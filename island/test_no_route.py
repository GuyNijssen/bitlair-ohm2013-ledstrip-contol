#!/usr/bin/env python3
"""
No route to the poles (Wi-Fi and the eth0 link both down): the controller
must skip frames, not crash and restart the effect in a tight loop, and
pick up again when the network is back.

Replaces the Art-Net socket with one that fails like a missing route.

  python3 island/test_no_route.py
"""

import errno
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import controller  # noqa: E402

failures = []


def check(ok, what):
  print(('ok   ' if ok else 'FAIL ') + what)
  if not ok:
    failures.append(what)


class NoRoute:
  def sendto(self, data, addr):
    raise OSError(errno.ENETUNREACH, os.strerror(errno.ENETUNREACH))

  def close(self):
    pass


cfg = {'island': {'name': 'no-route-test', 'poles': ['127.0.0.1:7015'], 'control': '127.0.0.1:6468'},
       'idle': {'effect': 'glow'}}
ctl = controller.Controller(cfg)
artnet = ctl.strip2D.strip.artnet
real_sock = artnet.sock

picks = 0
pick = ctl.pick
def counting_pick():
  global picks
  picks += 1
  return pick()
ctl.pick = counting_pick

artnet.sock = NoRoute()
t = threading.Thread(target=ctl.play, daemon=True)
t.start()
time.sleep(2)
check(picks == 1, f'effect not restarted while sending fails (started {picks}x in 2 s)')
check(ctl.errors == 0, 'no effect crashes counted')
check(ctl.send_error == os.strerror(errno.ENETUNREACH), f"status shows why: {ctl.send_error!r}")
check(ctl.frames == 0, 'no frames counted as sent')

artnet.sock = real_sock
time.sleep(1)
check(ctl.send_error is None, 'error cleared when the network is back')
check(ctl.frames > 20, f'frames flow again ({ctl.frames} in 1 s)')
check(picks == 1, 'still the same effect run')

ctl.stop()
t.join(timeout=5)
check(not t.is_alive(), 'stops cleanly')
if failures:
  sys.exit(1)
print('all passed')
