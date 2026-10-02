#!/usr/bin/env python3
"""
Tests for netconf.py that touch no network: config validation and the
dnsmasq config it writes.

  python3 island/test_netconf.py
"""

import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['STATE_DIRECTORY'] = tempfile.mkdtemp()
import netconf  # noqa: E402

OFFICE = {'eth0': ['10.10.192.113/24'], 'wlan0': ['10.10.192.115/24']}
failures = []


def check(ok, what):
  print(('ok   ' if ok else 'FAIL ') + what)
  if not ok:
    failures.append(what)


def error(changes, mgmt=OFFICE):
  cfg = copy.deepcopy(netconf.DEFAULTS)
  cfg.update(changes)
  try:
    netconf.validate(cfg, mgmt)
    return None
  except ValueError as e:
    return str(e)


check(error({}) is None, 'defaults are valid')
check(error({'mode': 'vlan', 'vlan_id': 89}) is None, 'vlan mode valid')
check('mode' in error({'mode': 'bridge'}), 'unknown mode refused')
check('VLAN' in error({'vlan_id': 1}), 'VLAN 1 refused')
check('VLAN' in error({'vlan_id': '89'}), 'VLAN id as text refused')
check('private' in error({'subnet': '8.8.8.0/24', 'pi_address': '8.8.8.1', 'dhcp_start': '8.8.8.2', 'dhcp_end': '8.8.8.9'}), 'public subnet refused')
check('not a network' in error({'subnet': '192.168.89.1/24'}), 'host address as subnet refused')
check('/16 to /29' in error({'subnet': '10.0.0.0/8'}), '/8 refused')
check('Tailscale' in error({'subnet': '100.64.10.0/24', 'pi_address': '100.64.10.1', 'dhcp_start': '100.64.10.2', 'dhcp_end': '100.64.10.9'}), 'Tailscale range refused')
check('overlaps 10.10.192.0/24' in error({'subnet': '10.10.192.0/24', 'pi_address': '10.10.192.1', 'dhcp_start': '10.10.192.2', 'dhcp_end': '10.10.192.9'}), 'overlap with management LAN refused')
check('usable' in error({'pi_address': '192.168.90.1'}), 'Pi address outside subnet refused')
check('usable' in error({'pi_address': '192.168.89.255'}), 'broadcast as Pi address refused')
check('inside the DHCP range' in error({'pi_address': '192.168.89.10'}), 'Pi address inside range refused')
check('start is after end' in error({'dhcp_start': '192.168.89.50', 'dhcp_end': '192.168.89.2'}), 'reversed range refused')
check('lease' in error({'lease': '12 hours'}), 'bad lease refused')
check(error({'subnet': '172.20.5.0/24', 'pi_address': '172.20.5.1', 'dhcp_start': '172.20.5.100', 'dhcp_end': '172.20.5.150'}) is None, 'other range accepted')

res = lambda *r: {'reservations': [dict(zip(('mac', 'ip', 'name'), x)) for x in r]}
check(error(res(('70:69:69:2d:30:31', '192.168.89.2', 'pole01'))) is None, 'reservation accepted')
check('MAC' in error(res(('7069692d3031', '192.168.89.2', ''))), 'bad MAC refused')
check('usable pole address' in error(res(('70:69:69:2d:30:31', '192.168.90.2', ''))), 'reservation outside subnet refused')
check('usable pole address' in error(res(('70:69:69:2d:30:31', '192.168.89.1', ''))), 'reservation on the Pi address refused')
check('twice' in error(res(('70:69:69:2d:30:31', '192.168.89.2', ''), ('70:69:69:2d:30:32', '192.168.89.2', ''))), 'same IP twice refused')
check('twice' in error(res(('70:69:69:2d:30:31', '192.168.89.2', ''), ('70:69:69:2d:30:31', '192.168.89.3', ''))), 'same MAC twice refused')
check('name' in error(res(('70:69:69:2d:30:31', '192.168.89.2', 'pole 1'))), 'name with space refused')
check(error({'subnet': '172.20.5.0/24', 'pi_address': '172.20.5.1', 'dhcp_start': '172.20.5.2', 'dhcp_end': '172.20.5.9',
             **res(('70:69:69:2d:30:31', '192.168.89.2', ''))}) is not None, 'new subnet that orphans a reservation refused')
check(error({}, mgmt={'eth0': ['192.168.89.1/24']}) is None, 'own Art-Net address on eth0 (wifi mode) is not an overlap')

cfg = copy.deepcopy(netconf.DEFAULTS)
cfg.update(mode='vlan', vlan_id=89, **res(('70:69:69:2d:30:31', '192.168.89.2', 'pole01'), ('70:69:69:2d:30:32', '192.168.89.3', '')))
netconf.write_dnsmasq(cfg)
conf = open(netconf.DNSMASQ_CONF).read()
check('interface=eth0.89\n' in conf, 'dnsmasq: VLAN interface')
check('port=0\n' in conf, 'dnsmasq: DNS off')
check('dhcp-range=192.168.89.2,192.168.89.50,255.255.255.0,12h\n' in conf, 'dnsmasq: range')
check('dhcp-option=3\n' in conf and 'dhcp-option=6\n' in conf, 'dnsmasq: no gateway, no DNS for the poles')
check('dhcp-host=70:69:69:2d:30:31,192.168.89.2,pole01\n' in conf, 'dnsmasq: named reservation')
check('dhcp-host=70:69:69:2d:30:32,192.168.89.3\n' in conf, 'dnsmasq: unnamed reservation')
cfg['mode'] = 'wifi'
netconf.write_dnsmasq(cfg)
check('interface=eth0\n' in open(netconf.DNSMASQ_CONF).read(), 'dnsmasq: wifi mode serves eth0')

if failures:
  sys.exit(1)
print('all passed')
