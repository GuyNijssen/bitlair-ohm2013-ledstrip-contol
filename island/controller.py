#!/usr/bin/env python3
"""
Headless island controller for the Art-Net LED poles.

Plays an idle effect and, on command, the next cue from the config.
Always keeps streaming frames: the pole firmware resets to dim white
after 60 s without Art-Net.

  ./island/controller.py --config /etc/ledpoles/island.toml

Commands arrive as JSON over UDP on the control address (localhost):
  {"cmd": "status"}            -> current state
  {"cmd": "next"}              -> play the next cue
  {"cmd": "cue", "index": 2}   -> play cue 2 (0-based)
  {"cmd": "idle"}              -> back to the idle effect
"""

import argparse
import importlib
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.append(os.path.join(ROOT, 'lib'))
sys.path.append(os.path.join(ROOT, 'singleSleeve'))

from strip import Effect, Strip2D  # noqa: E402

log = logging.getLogger('island')

# These effects read the terminal with termios and crash without a TTY
# (see FORK_CHANGES.md). Refuse them until the headless-input fix lands.
HEADLESS_UNSAFE = {'fire2', 'eyes', 'mqtt_fire'}

# Crash budget for the idle effect before falling back to Glow.
IDLE_CRASH_LIMIT = 3
IDLE_CRASH_WINDOW = 60


class Switch(Exception):
  """Raised from send() to stop the running effect right away."""


class Glow(Effect):
  """Built-in fallback: steady dim warm light. Cannot fail."""

  def step(self, count):
    for y in range(self.strip2D.leny):
      for x in range(self.strip2D.lenx):
        self.strip2D.set(x, y, [60, 30, 8])


def parse_addr(entry):
  """'192.168.89.12' or '127.0.0.1:7000' -> (host, port)"""
  host, _, port = str(entry).partition(':')
  return (host, int(port) if port else 6454)


def load_effect(spec):
  """'plasma.Plasma' -> class from singleSleeve/plasma.py"""
  if spec == 'glow':
    return Glow
  module, _, cls = spec.partition('.')
  if module in HEADLESS_UNSAFE:
    raise ValueError(f'{spec}: needs a terminal, not usable headless yet')
  return getattr(importlib.import_module(module), cls)


class Controller:

  def __init__(self, cfg):
    island = cfg.get('island', {})
    self.name = island.get('name', 'island')
    self.poles = [parse_addr(p) for p in island.get('poles', ['192.168.89.255'])]
    self.brightness = min(1.0, max(0.0, float(island.get('brightness', 0.4))))
    self.control = parse_addr(island.get('control', '127.0.0.1:6455'))

    self.strip2D = Strip2D(7, 21, addr=list(self.poles))
    artnet = self.strip2D.strip.artnet
    artnet.fade = self.brightness  # applied to every frame in Artnet.send

    # Count frames so the status shows the keep-alive is working, and abort
    # the running effect from inside send(): some effects only check quit
    # every few seconds (weird3: ~15 s), but all of them send every frame.
    self.frames = 0
    self.last_frame = 0.0
    self.abort = False
    send = artnet.send
    def counted_send(current_strip):
      if self.abort:
        raise Switch()
      send(current_strip)
      self.frames += 1
      self.last_frame = time.time()
    artnet.send = counted_send

    self.idle_spec = cfg.get('idle', {}).get('effect', 'glow')
    try:
      self.idle_cls = load_effect(self.idle_spec)
    except Exception as e:
      log.error('idle effect %s unusable (%s), using glow', self.idle_spec, e)
      self.idle_spec, self.idle_cls = 'glow', Glow

    self.cues = []
    for i, cue in enumerate(cfg.get('cue', [])):
      try:
        cls = load_effect(cue['effect'])
      except Exception as e:
        log.error('cue %d (%s) skipped: %s', i, cue.get('name', cue.get('effect')), e)
        continue
      self.cues.append({
        'name': cue.get('name', cue['effect']),
        'effect': cue['effect'],
        'cls': cls,
        'duration': float(cue.get('duration', 0)),  # 0 = until next command
      })

    self.lock = threading.Lock()
    self.pending = None      # ('idle',) or ('cue', index)
    self.current = None      # running Effect instance
    self.deadline = None     # end time of a timed cue
    self.mode = 'starting'
    self.cue_index = None
    self.next_cue = 0
    self.since = time.time()
    self.errors = 0
    self.last_error = None
    self.idle_crashes = []
    self.running = True

  # --- commands -----------------------------------------------------------

  def request(self, target):
    with self.lock:
      self.pending = target
      if self.current:
        self.current.quit = True

  def handle(self, msg):
    cmd = msg.get('cmd')
    if cmd == 'next':
      if not self.cues:
        return {'error': 'no cues configured'}
      self.request(('cue', self.next_cue))
    elif cmd == 'cue':
      index = msg.get('index')
      if not isinstance(index, int) or not 0 <= index < len(self.cues):
        return {'error': f'index must be 0..{len(self.cues) - 1}'}
      self.request(('cue', index))
    elif cmd == 'idle':
      self.request(('idle',))
    elif cmd != 'status':
      return {'error': f'unknown command {cmd!r}'}
    return self.status()

  def status(self):
    cue = self.cues[self.cue_index] if self.cue_index is not None else None
    return {
      'island': self.name,
      'mode': self.mode,
      'effect': cue['effect'] if cue else self.idle_spec,
      'cue': {'index': self.cue_index, 'name': cue['name']} if cue else None,
      'next_cue': self.cues[self.next_cue]['name'] if self.cues else None,
      'cues': [c['name'] for c in self.cues],
      'since': self.since,
      'brightness': self.brightness,
      'poles': [f'{h}:{p}' for h, p in self.poles],
      'frames': self.frames,
      'last_frame_age': round(time.time() - self.last_frame, 2) if self.last_frame else None,
      'errors': self.errors,
      'last_error': self.last_error,
    }

  def serve_control(self, sock):
    sock.settimeout(0.5)
    while self.running:
      try:
        data, peer = sock.recvfrom(4096)
      except socket.timeout:
        continue
      try:
        reply = self.handle(json.loads(data))
      except Exception as e:
        reply = {'error': str(e)}
      sock.sendto(json.dumps(reply).encode(), peer)

  # --- playback -----------------------------------------------------------

  def pick(self):
    with self.lock:
      target, self.pending = self.pending or ('idle',), None
      self.abort = False
      if target[0] == 'cue':
        index = target[1]
        cue = self.cues[index]
        self.next_cue = (index + 1) % len(self.cues)
        self.mode, self.cue_index = 'cue', index
        self.current = cue['cls'](self.strip2D)
        self.deadline = time.time() + cue['duration'] if cue['duration'] else None
      else:
        self.mode, self.cue_index = 'idle', None
        self.current = self.idle_cls(self.strip2D)
        self.deadline = None
      self.since = time.time()
      return self.current

  def supervise(self):
    """Enforce cue durations and pending switches. Some effects ignore the
    runtime argument, and Effect.run resets quit when it starts, so a
    command arriving at that moment would otherwise be lost."""
    while self.running:
      with self.lock:
        if self.current and (self.pending or (self.deadline and time.time() >= self.deadline)):
          self.current.quit = True
          self.abort = True
      time.sleep(0.1)

  def play(self):
    while self.running:
      effect = self.pick()
      log.info('playing %s (%s)', type(effect).__name__, self.mode)
      try:
        effect.run()
      except Switch:
        pass
      except Exception as e:
        self.errors += 1
        self.last_error = f'{type(effect).__name__}: {e}'
        log.exception('effect crashed, back to idle')
        if self.mode == 'idle':
          now = time.time()
          self.idle_crashes = [t for t in self.idle_crashes if now - t < IDLE_CRASH_WINDOW] + [now]
          if len(self.idle_crashes) >= IDLE_CRASH_LIMIT:
            log.error('idle effect keeps crashing, switching to glow')
            self.idle_spec, self.idle_cls = 'glow', Glow

  def stop(self, *_):
    self.running = False
    with self.lock:
      self.abort = True
      if self.current:
        self.current.quit = True


def main():
  ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
  ap.add_argument('--config', default='/etc/ledpoles/island.toml')
  args = ap.parse_args()
  logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

  with open(args.config, 'rb') as f:
    cfg = tomllib.load(f)

  # Claim the control port before touching Art-Net. It doubles as the
  # single-instance lock: port 6454 is opened with SO_REUSEADDR, so a
  # second controller would otherwise start and interleave frames.
  control = parse_addr(cfg.get('island', {}).get('control', '127.0.0.1:6455'))
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  try:
    sock.bind(control)
  except OSError as e:
    sys.exit(f'control port {control[0]}:{control[1]} busy ({e.strerror}): '
             'is another controller running? (systemctl status pimod-artnet)')

  ctl = Controller(cfg)
  # Replace the lib's SIGINT handler (it SIGKILLs itself) with a clean stop.
  signal.signal(signal.SIGINT, ctl.stop)
  signal.signal(signal.SIGTERM, ctl.stop)

  threading.Thread(target=ctl.serve_control, args=(sock,), daemon=True).start()
  threading.Thread(target=ctl.supervise, daemon=True).start()
  log.info('island %s: %d poles, brightness %.2f, %d cues, control %s:%d',
           ctl.name, len(ctl.poles), ctl.brightness, len(ctl.cues), *ctl.control)
  ctl.play()

  ctl.strip2D.strip.artnet.clear()
  log.info('stopped; poles reset to dim white after ~68 s without frames')


if __name__ == '__main__':
  main()
