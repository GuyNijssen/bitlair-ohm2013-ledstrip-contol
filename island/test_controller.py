#!/usr/bin/env python3
"""
Packet-level test for the island controller, no poles needed.

Starts controller.py headless (stdin = /dev/null) pointed at a local UDP
port, then checks the Art-Net header, the brightness cap, the keep-alive
and the cue state machine over the control port.

  python3 island/test_controller.py
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SINK = ('127.0.0.1', 7011)
CONTROL = ('127.0.0.1', 6465)
CAP = 0.4

CONFIG = f"""
[island]
name = "test"
poles = ["{SINK[0]}:{SINK[1]}"]
brightness = {CAP}
control = "{CONTROL[0]}:{CONTROL[1]}"

[idle]
effect = "glow"

[[cue]]
name = "Rainbow"
effect = "rainbow.Rainbow"
duration = 2

[[cue]]
name = "Weird3"
effect = "weird3.Weird3"
duration = 2

[[cue]]
name = "Not headless"
effect = "fire2.Fire2"
"""

failures = []


def check(ok, what):
  print(('ok   ' if ok else 'FAIL ') + what)
  if not ok:
    failures.append(what)


def command(msg):
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  s.settimeout(2)
  s.sendto(json.dumps(msg).encode(), CONTROL)
  reply = json.loads(s.recv(65536))
  s.close()
  return reply


def frames(sock, seconds):
  out, end = [], time.time() + seconds
  while time.time() < end:
    try:
      out.append(sock.recv(2048))
    except socket.timeout:
      pass
  return out


def main():
  sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  sink.bind(SINK)
  sink.settimeout(0.2)

  with tempfile.NamedTemporaryFile('w', suffix='.toml', delete=False) as f:
    f.write(CONFIG)
  proc = subprocess.Popen(
    [sys.executable, os.path.join(HERE, 'controller.py'), '--config', f.name],
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
  try:
    time.sleep(1.5)
    while True:  # drop frames queued during startup
      try:
        sink.setblocking(False); sink.recv(2048)
      except BlockingIOError:
        sink.settimeout(0.2); break
    got = frames(sink, 1)
    check(len(got) > 20, f'streams while idle ({len(got)} frames/s)')
    check(all(p[:8] == b'Art-Net\x00' and p[8:10] == b'\x00\x50' for p in got), 'ArtDmx header')
    check(all(max(p[18:]) <= int(255 * CAP) for p in got), f'brightness cap {CAP}')

    st = command({'cmd': 'status'})
    check(st['mode'] == 'idle' and st['effect'] == 'glow', 'starts in idle')
    check(st['cues'] == ['Rainbow', 'Weird3'], 'fire2 cue refused (needs a terminal)')

    st = command({'cmd': 'next'})
    check(st['mode'] == 'cue' and st['cue']['name'] == 'Rainbow', 'next -> cue 0 (reply shows new state)')
    check(all(max(p[18:]) <= int(255 * CAP) for p in frames(sink, 0.5)), 'cap holds during cue')

    time.sleep(2)
    st = command({'cmd': 'status'})
    check(st['mode'] == 'idle' and st['next_cue'] == 'Weird3', 'timed cue returns to idle')

    command({'cmd': 'next'}); time.sleep(3)
    st = command({'cmd': 'status'})
    check(st['mode'] == 'idle', 'weird3 (ignores runtime) still ends on time')

    command({'cmd': 'cue', 'index': 0}); time.sleep(0.5)
    command({'cmd': 'idle'}); time.sleep(0.5)
    st = command({'cmd': 'status'})
    check(st['mode'] == 'idle', 'idle command interrupts a cue')
    check('error' in command({'cmd': 'cue', 'index': 9}), 'bad cue index rejected')
    check(st['errors'] == 0, 'no effect crashes')
    check(len(frames(sink, 1)) > 20, 'still streaming after switches')

    effects = command({'cmd': 'status'})['effects']
    check('plasma.Plasma' in effects and 'glow' in effects, f'effects listed ({len(effects)})')
    check(not any(e.startswith(('fire2.', 'eyes.', 'mqtt_fire.')) for e in effects),
          'terminal-only effects not listed')
    st = command({'cmd': 'play', 'effect': 'fire.Fire'})
    check(st['mode'] == 'play' and st['effect'] == 'fire.Fire', 'play any effect')
    check('error' in command({'cmd': 'play', 'effect': 'os.system'}), 'unknown effect rejected')
    command({'cmd': 'idle'})
    st = command({'cmd': 'set_idle', 'effect': 'stars.Stars1'})
    check(st['mode'] == 'idle' and st['effect'] == 'stars.Stars1', 'set_idle switches the running idle')
    check(max(max(p[18:]) for p in frames(sink, 1)) <= int(255 * CAP), 'cap holds for new effects')

    second = subprocess.run(
      [sys.executable, os.path.join(HERE, 'controller.py'), '--config', f.name],
      stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    check(second.returncode != 0 and 'another controller' in second.stderr,
          'second instance refuses to start')
  finally:
    proc.terminate()
    try:
      out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
      proc.kill()
      out, _ = proc.communicate()
      out += '\n(killed: no exit within 5 s of SIGTERM)'
  check(proc.returncode == 0, 'clean exit on SIGTERM')
  check('termios' not in out, 'no termios errors')
  if failures:
    print(out)
    sys.exit(1)
  print('all passed')


if __name__ == '__main__':
  main()
