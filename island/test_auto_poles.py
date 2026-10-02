#!/usr/bin/env python3
"""
poles = "auto": the controller follows the network module's config.

Runs the controller with a temporary network.json that names the loopback
interface, then switches that config to "vlan" with a 127.x subnet, and
checks that frames keep arriving and the target follows within ~6 s.
Needs Linux (reads `ip`), no poles.

  python3 island/test_auto_poles.py
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 7013
CONTROL = ('127.0.0.1', 6467)
failures = []


def check(ok, what):
  print(('ok   ' if ok else 'FAIL ') + what)
  if not ok:
    failures.append(what)


def status():
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  s.settimeout(2)
  s.sendto(b'{"cmd": "status"}', CONTROL)
  reply = json.loads(s.recv(65536))
  s.close()
  return reply


def frames(sock, seconds):
  n, end = 0, time.time() + seconds
  while time.time() < end:
    try:
      sock.recv(2048)
      n += 1
    except socket.timeout:
      pass
  return n


def main():
  tmp = tempfile.mkdtemp()
  net = os.path.join(tmp, 'network.json')
  with open(net, 'w') as f:
    json.dump({'mode': 'shared', 'eth': 'lo'}, f)
  island = os.path.join(tmp, 'island.toml')
  with open(island, 'w') as f:
    f.write(f'[island]\nname = "auto-test"\npoles = "auto"\nport = {PORT}\n'
            f'control = "{CONTROL[0]}:{CONTROL[1]}"\n[idle]\neffect = "glow"\n')

  sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  sink.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  sink.bind(('0.0.0.0', PORT))
  sink.settimeout(0.2)

  proc = subprocess.Popen([sys.executable, os.path.join(HERE, 'controller.py'), '--config', island],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          env={**os.environ, 'NETCONF_CONFIG': net})
  try:
    time.sleep(1.5)
    st = status()
    check(st['poles'] == [f'127.255.255.255:{PORT}'], f"shared: broadcast of lo's network ({st['poles']})")
    check(st['poles_auto'] == {'network_mode': 'shared'}, 'status says auto, shared')
    check(frames(sink, 1) > 20, 'frames arrive (shared)')

    with open(net, 'w') as f:
      json.dump({'mode': 'vlan', 'eth': 'lo', 'subnet': '127.1.0.0/16'}, f)
    deadline, st = time.time() + 8, None
    while time.time() < deadline:
      st = status()
      if st['poles'] == [f'127.1.255.255:{PORT}']:
        break
      time.sleep(0.5)
    check(st['poles'] == [f'127.1.255.255:{PORT}'], f"vlan: follows to the Art-Net broadcast ({st['poles']})")
    check(st['poles_auto'] == {'network_mode': 'vlan'}, 'status says vlan')
    check(frames(sink, 1) > 20, 'frames keep arriving after the switch')
    check(st['errors'] == 0, 'no effect crashes')
  finally:
    proc.terminate()
    out, _ = proc.communicate(timeout=10)
  check('now sending to 127.1.255.255' in out, 'switch is logged')
  if failures:
    print(out)
    sys.exit(1)
  print('all passed')


if __name__ == '__main__':
  main()
