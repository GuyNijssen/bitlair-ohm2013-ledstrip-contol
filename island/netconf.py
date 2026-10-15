#!/usr/bin/env python3
"""
Art-Net network of an island Pi: which link carries management and which
Art-Net, plus the DHCP server for the poles. Runs as root (it changes
NetworkManager profiles and runs dnsmasq) and is controlled by pi-agent over
a localhost UDP port, like the controller.

Modes
  shared  Ethernet for management and Art-Net (eth0 on DHCP). No DHCP server:
          the poles get their address from the network they are on.
  wifi    Wi-Fi for management; eth0 gets the Pi's Art-Net address and a
          DHCP server for the poles.
  vlan    eth0 for management (DHCP); a VLAN on eth0 gets the Pi's Art-Net
          address and a DHCP server for the poles.

Nothing changes on start: changes happen on set_config only. Each change is
checked first (a Wi-Fi with internet for the wifi mode, a cable on eth0 for
the Ethernet modes, no other DHCP server on the Art-Net link) and rolled back
if management loses its internet connection afterwards.

While management runs over Ethernet (shared, vlan), Wi-Fi can be connected
and disconnected; in the wifi mode it is locked, since it carries management.

Fallback hotspot: when the Pi has no management connection for 90 s (wifi
mode: not on a saved Wi-Fi; Ethernet modes: no DHCP address on eth0 and no
Wi-Fi), it opens a Wi-Fi access point named after the Pi (WPA2, password
"hotspot_password", default pipipipi) at 10.42.0.1. Every name looked up
there points at the Pi (captive portal), so a phone shows the Pi's page
(pi-agent, also on port 80 of that address). It goes back as soon as eth0
has an address again (Ethernet modes), and in every mode every 5 minutes
while no phone is connected: then the saved Wi-Fi networks get 45 s. Connecting a
Wi-Fi network from the page also ends it. "hotspot": false switches it off.

Config: /etc/ledpoles/network.json. Commands (JSON over UDP 127.0.0.1:6456):
  {"cmd": "status"}
  {"cmd": "set_config", "mode": "vlan", "vlan_id": 89, "subnet": "192.168.89.0/24",
   "pi_address": "192.168.89.1", "dhcp_start": "192.168.89.2", "dhcp_end": "192.168.89.50"}
  {"cmd": "add_reservation", "mac": "70:69:69:2d:30:31", "ip": "192.168.89.2", "name": "pole01"}
  {"cmd": "remove_reservation", "mac": "70:69:69:2d:30:31"}
  {"cmd": "wifi_rescan"}
  {"cmd": "wifi_connect", "profile": "netplan-wlan0-24IT"}       saved network
  {"cmd": "wifi_connect", "ssid": "Name", "password": "..."}    new network
  {"cmd": "wifi_disconnect"}
  {"cmd": "hotspot", "on": true, "minutes": 10}      start it now (to try it), or "on": false
  {"cmd": "hotspot_auto", "on": false}                no automatic hotspot
"""

import copy
import ipaddress
import json
import logging
import os
import random
import re
import socket
import struct
import subprocess
import threading
import time
import tomllib

CONFIG = os.environ.get('NETCONF_CONFIG', '/etc/ledpoles/network.json')
STATE = os.environ.get('STATE_DIRECTORY', '/var/lib/ledpoles')
DNSMASQ_CONF = os.path.join(STATE, 'dnsmasq-artnet.conf')
LEASES = os.path.join(STATE, 'dnsmasq.leases')
DHCP_UNIT = 'ledpoles-dhcp.service'
ISLAND_CONFIG = '/etc/ledpoles/island.toml'
CONTROL = ('127.0.0.1', 6456)
PROFILE = 'artnet'            # the NetworkManager profile this owns
# Management must reach this after a change. Override only to test the
# rollback (e.g. 192.0.2.1, which never answers).
CHECK_HOST = os.environ.get('NETCONF_CHECK_HOST', '1.1.1.1')
CHECK_SECONDS = 45

HOTSPOT_PROFILE = 'ett-hotspot'   # NetworkManager profile of the fallback hotspot
HOTSPOT_ADDRESS = '10.42.0.1'
HOTSPOT_GRACE = 90                # s without management before it starts
HOTSPOT_RETRY = 300               # wifi mode: s between tries of the saved networks
HOTSPOT_TRY = 45                  # s NetworkManager gets to join a saved network
# Every name a phone looks up on the hotspot points at the Pi: its "is there
# internet?" check lands on pi-agent, which sends it to the Pi's page.
CAPTIVE_CONF = '/etc/NetworkManager/dnsmasq-shared.d/ett-hotspot.conf'

MODES = {
  'shared': ('Ethernet for management and Art-Net',
             'eth0 on the existing network (DHCP). Art-Net goes over the same network; no DHCP server on the Pi.'),
  'wifi': ('Wi-Fi management, Ethernet for Art-Net',
           'Management over Wi-Fi. eth0 gets the Art-Net address and a DHCP server for the poles.'),
  'vlan': ('Ethernet management, VLAN for Art-Net',
           'eth0 stays on the existing network (DHCP). A tagged VLAN on eth0 gets the Art-Net address and a DHCP server.'),
}

DEFAULTS = {
  'mode': 'shared',
  'eth': 'eth0',
  'wifi': 'wlan0',
  'vlan_id': 89,
  'subnet': '192.168.89.0/24',
  'pi_address': '192.168.89.1',
  'dhcp_start': '192.168.89.2',
  'dhcp_end': '192.168.89.50',
  'lease': '12h',
  'reservations': [],
  'hotspot': True,
  'hotspot_password': 'pipipipi',
}

log = logging.getLogger('netconf')


class Refused(Exception):
  """A check failed before anything was changed."""


# --- config -------------------------------------------------------------------------

def load():
  try:
    with open(CONFIG) as f:
      return {**DEFAULTS, **json.load(f)}
  except FileNotFoundError:
    return copy.deepcopy(DEFAULTS)


def save(cfg):
  tmp = CONFIG + '.tmp'
  with open(tmp, 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
  os.replace(tmp, CONFIG)


MAC_RE = re.compile(r'^([0-9a-f]{2}:){5}[0-9a-f]{2}$')
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,31}$')


def validate(cfg, mgmt=None):
  """Raise ValueError with a readable reason if cfg is not usable. mgmt:
  {iface: [addresses]} of the management links (read live when None)."""
  if cfg['mode'] not in MODES:
    raise ValueError(f"mode must be one of: {', '.join(MODES)}")
  if not isinstance(cfg['vlan_id'], int) or not 2 <= cfg['vlan_id'] <= 4094:
    raise ValueError('VLAN id must be a whole number from 2 to 4094')
  try:
    net = ipaddress.ip_network(cfg['subnet'], strict=True)
  except ValueError:
    raise ValueError(f"subnet {cfg['subnet']!r} is not a network like 192.168.89.0/24")
  if net.version == 4 and net.overlaps(ipaddress.ip_network('100.64.0.0/10')):
    raise ValueError('subnet overlaps the Tailscale range 100.64.0.0/10')
  if net.version != 4 or not net.is_private or not 16 <= net.prefixlen <= 29:
    raise ValueError('subnet must be a private IPv4 network from /16 to /29')
  if net.overlaps(ipaddress.ip_network(f'{HOTSPOT_ADDRESS}/24', strict=False)):
    raise ValueError(f'subnet overlaps the fallback hotspot network {HOTSPOT_ADDRESS}/24')
  if not isinstance(cfg.get('hotspot', True), bool):
    raise ValueError('hotspot must be true or false')
  if not 8 <= len(str(cfg.get('hotspot_password', ''))) <= 63:
    raise ValueError('hotspot password: 8 to 63 characters')

  def host(key):
    try:
      a = ipaddress.ip_address(cfg[key])
    except ValueError:
      raise ValueError(f'{key} {cfg[key]!r} is not an IPv4 address')
    if a not in net or a in (net.network_address, net.broadcast_address):
      raise ValueError(f'{key} {a} is not a usable address in {net}')
    return a

  pi, start, end = host('pi_address'), host('dhcp_start'), host('dhcp_end')
  if start > end:
    raise ValueError('DHCP range: start is after end')
  if start <= pi <= end:
    raise ValueError(f'the Pi address {pi} lies inside the DHCP range')
  if not re.fullmatch(r'\d{1,4}[mh]|infinite', str(cfg['lease'])):
    raise ValueError('lease must be like 30m, 12h or infinite')

  # The Art-Net subnet must not overlap a management network.
  if mgmt is None:
    mgmt = {iface: addresses(iface) for iface in (cfg['eth'], cfg['wifi'])}
  for iface, addrs in mgmt.items():
    for a in addrs:
      mgmt = ipaddress.ip_interface(a).network
      if mgmt.overlaps(net) and str(ipaddress.ip_interface(a).ip) != cfg['pi_address']:
        raise ValueError(f'subnet {net} overlaps {mgmt} on {iface}')

  macs, ips = set(), set()
  for r in cfg['reservations']:
    if not MAC_RE.match(r['mac']):
      raise ValueError(f"MAC {r['mac']!r}: use the form 70:69:69:2d:30:31")
    ip = ipaddress.ip_address(r['ip'])
    if ip not in net or ip == pi or ip in (net.network_address, net.broadcast_address):
      raise ValueError(f"reservation {r['mac']}: {ip} is not a usable pole address in {net}")
    if r.get('name') and not NAME_RE.match(r['name']):
      raise ValueError(f"name {r['name']!r}: letters, digits and - only, up to 32")
    if r['mac'] in macs or r['ip'] in ips:
      raise ValueError(f"MAC {r['mac']} or address {r['ip']} is reserved twice")
    macs.add(r['mac']); ips.add(r['ip'])


# --- system helpers -----------------------------------------------------------------

def run(*cmd, check=True, timeout=30, secret=None):
  """Run a command; on failure raise with its output. `secret` (a password
  on the command line) is masked in the error."""
  try:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
  except subprocess.TimeoutExpired:
    raise RuntimeError(f'{cmd[0]} {cmd[1] if len(cmd) > 1 else ""}: no answer within {timeout} s')
  if check and r.returncode != 0:
    msg = f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()}"
    raise RuntimeError(msg.replace(secret, '***') if secret else msg)
  return r.stdout


def addresses(iface):
  out = run('ip', '-4', '-o', 'addr', 'show', 'dev', iface, check=False)
  return re.findall(r'inet (\S+)', out)


def nm_profiles():
  """[(name, type, interface)] of all NetworkManager profiles."""
  rows = []
  for line in run('nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show').splitlines():
    name, _, ctype = line.rpartition(':')
    iface = run('nmcli', '-g', 'connection.interface-name', 'con', 'show', name, check=False).strip()
    rows.append((name.replace('\\:', ':'), ctype, iface))
  return rows


def eth_profiles(eth):
  return [n for n, t, i in nm_profiles() if t == '802-3-ethernet' and i == eth and n != PROFILE]


def wifi_info(wifi):
  """The Wi-Fi network wlan0 is on; the fallback hotspot does not count."""
  conn = run('nmcli', '-g', 'GENERAL.CONNECTION', 'dev', 'show', wifi, check=False).strip()
  if conn == HOTSPOT_PROFILE:
    return {'connection': None, 'ssid': None, 'ipv4': None}
  ssid = run('nmcli', '-g', '802-11-wireless.ssid', 'con', 'show', conn, check=False).strip() if conn else ''
  return {'connection': conn or None, 'ssid': ssid or None, 'ipv4': (addresses(wifi) or [None])[0]}


# --- fallback hotspot ---------------------------------------------------------------

def hotspot_active(wifi):
  return run('nmcli', '-g', 'GENERAL.CONNECTION', 'dev', 'show', wifi, check=False).strip() == HOTSPOT_PROFILE


def hotspot_clients(wifi):
  return run('iw', 'dev', wifi, 'station', 'dump', check=False).count('Station ')


def hotspot_ssid():
  return socket.gethostname()[:32]


def hotspot_up(cfg):
  wifi, psk = cfg['wifi'], cfg['hotspot_password']
  captive = f'address=/#/{HOTSPOT_ADDRESS}\n'
  try:
    current = open(CAPTIVE_CONF).read()
  except OSError:
    current = None
  if current != captive:
    os.makedirs(os.path.dirname(CAPTIVE_CONF), exist_ok=True)
    with open(CAPTIVE_CONF, 'w') as f:
      f.write(captive)
  settings = ['connection.autoconnect', 'no', '802-11-wireless.ssid', hotspot_ssid(),
              '802-11-wireless.mode', 'ap', '802-11-wireless.band', 'bg',
              'ipv4.method', 'shared', 'ipv4.addresses', f'{HOTSPOT_ADDRESS}/24', 'ipv6.method', 'disabled',
              'wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', psk, 'wifi-sec.proto', 'rsn',
              'wifi-sec.pairwise', 'ccmp', 'wifi-sec.group', 'ccmp']
  if any(p['name'] == HOTSPOT_PROFILE for p in wifi_profiles(include_hotspot=True)):
    run('nmcli', 'con', 'mod', HOTSPOT_PROFILE, *settings, secret=psk)
  else:
    run('nmcli', 'con', 'add', 'type', 'wifi', 'ifname', wifi, 'con-name', HOTSPOT_PROFILE, *settings, secret=psk)
  run('nmcli', 'con', 'up', HOTSPOT_PROFILE, 'ifname', wifi, timeout=60)


def hotspot_down():
  run('nmcli', 'con', 'down', HOTSPOT_PROFILE, check=False, timeout=30)


def management_ok(cfg):
  """A management connection: a saved Wi-Fi with an address, or (Ethernet
  modes) an address on eth0 from its network."""
  w = wifi_info(cfg['wifi'])
  if w['connection'] and w['ipv4']:
    return True
  return cfg['mode'] != 'wifi' and bool(addresses(cfg['eth']))


def reaches_internet(iface):
  return subprocess.run(['ping', '-c', '1', '-W', '2', '-I', iface, CHECK_HOST],
                        capture_output=True).returncode == 0


def carrier(iface):
  """True when the interface has a link (cable plugged in, other end up)."""
  try:
    with open(f'/sys/class/net/{iface}/carrier') as f:
      return f.read().strip() == '1'
  except OSError:
    return False


def split_terse(line):
  """Split an `nmcli -t` line on unescaped colons."""
  return [p.replace('\\:', ':').replace('\\\\', '\\') for p in re.split(r'(?<!\\):', line)]


def wifi_profiles(include_hotspot=False):
  out = []
  for line in run('nmcli', '-t', '-f', 'NAME,TYPE,AUTOCONNECT,ACTIVE', 'con', 'show', check=False).splitlines():
    name, ctype, auto, active = (split_terse(line) + [''] * 4)[:4]
    if ctype == '802-11-wireless' and (include_hotspot or name != HOTSPOT_PROFILE):
      ssid = run('nmcli', '-g', '802-11-wireless.ssid', 'con', 'show', name, check=False).strip()
      out.append({'name': name, 'ssid': ssid, 'autoconnect': auto == 'yes', 'active': active == 'yes'})
  return out


def wifi_visible(wifi):
  """Networks from the last scan (no new scan: that takes seconds)."""
  seen = {}
  out = run('nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY', 'dev', 'wifi', 'list', 'ifname', wifi, '--rescan', 'no', check=False)
  for line in out.splitlines():
    ssid, signal, security = (split_terse(line) + [''] * 3)[:3]
    if ssid and (ssid not in seen or int(signal or 0) > seen[ssid]['signal']):
      seen[ssid] = {'ssid': ssid, 'signal': int(signal or 0), 'security': security or 'open'}
  return sorted(seen.values(), key=lambda n: -n['signal'])


def dhcp_servers(iface, ignore=(), timeout=3.0):
  """Send one DHCPDISCOVER on iface and return the servers that offer."""
  mac = bytes.fromhex(open(f'/sys/class/net/{iface}/address').read().strip().replace(':', ''))
  xid = random.getrandbits(32)
  pkt = struct.pack('!BBBBIHH4s4s4s4s16s64s128s4s', 1, 1, 6, 0, xid, 0, 0x8000,
                    bytes(4), bytes(4), bytes(4), bytes(4), mac + bytes(10), b'', b'', b'\x63\x82\x53\x63')
  pkt += bytes([53, 1, 1, 55, 3, 1, 3, 6, 255])
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  try:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
    s.bind(('', 68))
    s.settimeout(0.5)
    s.sendto(pkt, ('255.255.255.255', 67))
    servers, end = set(), time.time() + timeout
    while time.time() < end:
      try:
        data, addr = s.recvfrom(4096)
      except socket.timeout:
        continue
      if len(data) < 240 or data[0] != 2 or struct.unpack('!I', data[4:8])[0] != xid:
        continue
      server, i = addr[0], 240
      while i + 1 < len(data) and data[i] != 255:
        if data[i] == 0:
          i += 1
          continue
        if data[i] == 54 and data[i + 1] == 4:
          server = socket.inet_ntoa(data[i + 2:i + 6])
        i += 2 + data[i + 1]
      servers.add(server)
    return sorted(servers - set(ignore))
  finally:
    s.close()


def artnet_iface(cfg):
  return f"{cfg['eth']}.{cfg['vlan_id']}" if cfg['mode'] == 'vlan' else cfg['eth']


# --- DHCP server ---------------------------------------------------------------------

def write_dnsmasq(cfg):
  net = ipaddress.ip_network(cfg['subnet'])
  lines = [
    '# Written by netconf.py; changes here are overwritten.',
    'port=0',                       # DHCP only, no DNS
    f'interface={artnet_iface(cfg)}',
    'bind-dynamic',                 # follows the address appearing/disappearing (link down, late switch)
    f"dhcp-range={cfg['dhcp_start']},{cfg['dhcp_end']},{net.netmask},{cfg['lease']}",
    'dhcp-option=3',                # no gateway for the poles
    'dhcp-option=6',                # no DNS
    f'dhcp-leasefile={LEASES}',
    'log-dhcp',
  ]
  for r in cfg['reservations']:
    lines.append(f"dhcp-host={r['mac']},{r['ip']}" + (f",{r['name']}" if r.get('name') else ''))
  tmp = DNSMASQ_CONF + '.tmp'
  with open(tmp, 'w') as f:
    f.write('\n'.join(lines) + '\n')
  os.replace(tmp, DNSMASQ_CONF)


def dhcp_on(cfg):
  write_dnsmasq(cfg)
  run('systemctl', 'restart', DHCP_UNIT)


def dhcp_off():
  run('systemctl', 'stop', DHCP_UNIT, check=False)


def leases():
  out = []
  try:
    with open(LEASES) as f:
      for line in f:
        p = line.split()
        if len(p) >= 4:
          out.append({'expires': int(p[0]), 'mac': p[1], 'ip': p[2], 'name': None if p[3] == '*' else p[3]})
  except FileNotFoundError:
    pass
  return out


# --- applying a mode ----------------------------------------------------------------

def delete_profile():
  if any(n == PROFILE for n, _, _ in nm_profiles()):
    run('nmcli', 'con', 'delete', PROFILE)


def eth_on_dhcp(eth, profiles):
  """eth0 back on its own (DHCP) profile; left alone if that already runs,
  so management does not drop for nothing."""
  for p in profiles:
    run('nmcli', 'con', 'mod', p, 'connection.autoconnect', 'yes')
  active = run('nmcli', '-g', 'GENERAL.CONNECTION', 'dev', 'show', eth, check=False).strip()
  if profiles and active not in profiles:
    run('nmcli', 'con', 'up', profiles[0], timeout=60)


def apply(cfg, probe=True):
  """Bring the network into cfg's mode. Raises Refused if a check fails
  before anything changed."""
  eth, wifi, mode = cfg['eth'], cfg['wifi'], cfg['mode']
  net = ipaddress.ip_network(cfg['subnet'])
  address = f"{cfg['pi_address']}/{net.prefixlen}"
  dhcp_profiles = eth_profiles(eth)

  if mode == 'wifi':
    w = wifi_info(wifi)
    if not w['ipv4'] or not reaches_internet(wifi):
      raise Refused(f'Wi-Fi ({wifi}) has no working internet connection; connect it first')
    if probe:
      others = dhcp_servers(eth, ignore=[cfg['pi_address']])
      if others:
        raise Refused(f"{eth} is on a network with a DHCP server ({', '.join(others)}); "
                      'connect it to the pole switch first, or the Pi would hand out addresses there')
  elif probe and not carrier(eth):
    # Management would move to (or stay on) eth0: it needs a cable.
    raise Refused(f'{eth} has no network link; plug it into a network first')

  dhcp_off()
  delete_profile()

  if mode == 'shared':
    eth_on_dhcp(eth, dhcp_profiles)
    return

  if mode == 'wifi':
    for p in dhcp_profiles:
      run('nmcli', 'con', 'mod', p, 'connection.autoconnect', 'no')
    run('nmcli', 'con', 'add', 'type', 'ethernet', 'con-name', PROFILE, 'ifname', eth,
        'ipv4.method', 'manual', 'ipv4.addresses', address, 'ipv4.never-default', 'yes',
        'ipv6.method', 'disabled', 'connection.autoconnect', 'yes')
  else:  # vlan
    eth_on_dhcp(eth, dhcp_profiles)
    run('nmcli', 'con', 'add', 'type', 'vlan', 'con-name', PROFILE, 'dev', eth, 'id', str(cfg['vlan_id']),
        'ifname', artnet_iface(cfg), 'ipv4.method', 'manual', 'ipv4.addresses', address,
        'ipv4.never-default', 'yes', 'ipv6.method', 'disabled', 'connection.autoconnect', 'yes')
  run('nmcli', 'con', 'up', PROFILE, timeout=60)

  if mode == 'vlan' and probe:
    # Only possible once the VLAN exists, so the old setup is already gone:
    # a plain error, which makes the caller roll back.
    others = dhcp_servers(artnet_iface(cfg), ignore=[cfg['pi_address']])
    if others:
      delete_profile()
      raise RuntimeError(f"VLAN {cfg['vlan_id']} already has a DHCP server ({', '.join(others)})")
  dhcp_on(cfg)


def management_iface(cfg):
  return cfg['wifi'] if cfg['mode'] == 'wifi' else cfg['eth']


def wait_for_management(cfg):
  end = time.time() + CHECK_SECONDS
  while time.time() < end:
    if reaches_internet(management_iface(cfg)):
      return True
    time.sleep(3)
  return False


# --- daemon -------------------------------------------------------------------------

class NetConf:

  def __init__(self):
    self.cfg = load()
    self.lock = threading.Lock()
    self.applying = None       # mode being applied
    self.last = None           # result of the last change
    self.wifi_busy = None      # Wi-Fi action running in the background
    self.wifi_last = None      # its result
    self.hotspot_since = None  # when the fallback hotspot started (None: off)
    self.hotspot_until = 0     # started by hand: keep it on until then
    self.hotspot_why = None
    self.bad_since = None      # management lost since
    self.next_try = 0          # wifi mode: next try of the saved networks

  def _apply_in_background(self, new):
    old = copy.deepcopy(self.cfg)
    try:
      apply(new)
      if not wait_for_management(new):
        raise RuntimeError(f"management ({management_iface(new)}) cannot reach {CHECK_HOST}")
      self.cfg = new
      save(new)
      self.last = {'ok': True, 'mode': new['mode'], 'at': time.time()}
      log.info('applied mode %s', new['mode'])
    except Refused as e:
      self.last = {'ok': False, 'mode': new['mode'], 'at': time.time(), 'error': str(e), 'rolled_back': False}
      log.warning('refused: %s', e)
    except Exception as e:
      log.exception('apply failed, rolling back to %s', old['mode'])
      rollback_error = None
      try:
        apply(old, probe=False)
      except Exception as e2:
        rollback_error = str(e2)
        log.exception('rollback failed')
      self.last = {'ok': False, 'mode': new['mode'], 'at': time.time(), 'error': str(e),
                   'rolled_back': rollback_error is None, 'rollback_error': rollback_error}
    finally:
      self.applying = None

  def set_config(self, msg):
    with self.lock:
      if self.applying:
        return {'error': f'still applying {self.applying}'}
      new = copy.deepcopy(self.cfg)
      for key in ('mode', 'vlan_id', 'subnet', 'pi_address', 'dhcp_start', 'dhcp_end', 'lease'):
        if key in msg:
          new[key] = msg[key]
      validate(new)  # also checks every reservation still fits a new subnet
      self.applying = new['mode']
    threading.Thread(target=self._apply_in_background, args=(new,), daemon=True).start()
    # A short reply: the full status calls nmcli several times, which is slow
    # while NetworkManager is busy with this change. The page polls status.
    return {'ok': True, 'applying': new['mode']}

  def change_reservations(self, change):
    with self.lock:
      if self.applying:
        return {'error': f'still applying {self.applying}'}
      new = copy.deepcopy(self.cfg)
      change(new['reservations'])
      new['reservations'].sort(key=lambda r: ipaddress.ip_address(r['ip']))
      validate(new)
      self.cfg = new
      save(new)
      if new['mode'] != 'shared':
        dhcp_on(new)   # rewrite and restart; leases are kept
    return {'ok': True, 'reservations': new['reservations']}

  def add_reservation(self, msg):
    entry = {'mac': str(msg.get('mac', '')).strip().lower().replace('-', ':'),
             'ip': str(msg.get('ip', '')).strip(),
             'name': str(msg.get('name') or '').strip()}
    def change(res):
      res[:] = [r for r in res if r['mac'] != entry['mac']]  # replacing one is fine
      res.append(entry)
    return self.change_reservations(change)

  def remove_reservation(self, msg):
    mac = str(msg.get('mac', '')).strip().lower()
    if not any(r['mac'] == mac for r in self.cfg['reservations']):
      return {'error': f'no reservation for {mac}'}
    return self.change_reservations(lambda res: res.__setitem__(slice(None), [r for r in res if r['mac'] != mac]))

  # --- Wi-Fi while management runs over Ethernet ---------------------------------

  def _wifi_job(self, label, fn):
    try:
      fn()
      self.wifi_last = {'ok': True, 'action': label, 'at': time.time()}
      log.info('wifi: %s', label)
    except Exception as e:
      self.wifi_last = {'ok': False, 'action': label, 'at': time.time(), 'error': str(e)}
      log.warning('wifi: %s failed: %s', label, e)
    finally:
      self.wifi_busy = None

  def _wifi_action(self, label, fn):
    with self.lock:
      if self.applying:
        return {'error': f'still applying {self.applying}'}
      # In the wifi mode Wi-Fi carries management, except while the fallback
      # hotspot runs: then joining a network is exactly what is needed.
      if self.cfg['mode'] == 'wifi' and not hotspot_active(self.cfg['wifi']):
        return {'error': 'Wi-Fi carries management in this mode; switch management back to Ethernet first'}
      if self.wifi_busy:
        return {'error': f'Wi-Fi is busy: {self.wifi_busy}'}
      self.wifi_busy = label
    threading.Thread(target=self._wifi_job, args=(label, fn), daemon=True).start()
    return {'ok': True, 'wifi_busy': label}

  def _leave_hotspot(self):
    """Before joining a Wi-Fi network: the radio does one or the other."""
    if hotspot_active(self.cfg['wifi']):
      log.info('hotspot: off, joining a Wi-Fi network')
      hotspot_down()
      self.hotspot_since, self.hotspot_until = None, 0

  def wifi_connect(self, msg):
    wifi = self.cfg['wifi']
    if msg.get('profile'):
      name = str(msg['profile'])
      if name not in [p['name'] for p in wifi_profiles()]:
        return {'error': f'no saved Wi-Fi network {name!r}'}
      def fn():
        self._leave_hotspot()
        run('nmcli', 'con', 'mod', name, 'connection.autoconnect', 'yes')
        run('nmcli', 'con', 'up', name, 'ifname', wifi, timeout=90)
      return self._wifi_action(f'connect to {name}', fn)
    ssid, password = str(msg.get('ssid') or ''), str(msg.get('password') or '')
    if not 1 <= len(ssid.encode()) <= 32:
      return {'error': 'network name: 1 to 32 characters'}
    if password and not 8 <= len(password) <= 63:
      return {'error': 'password: 8 to 63 characters (or empty for an open network)'}
    def fn():
      self._leave_hotspot()
      run('nmcli', 'dev', 'wifi', 'rescan', 'ifname', wifi, check=False)
      time.sleep(4)   # the scan from AP mode is stale or empty
      cmd = ['nmcli', 'dev', 'wifi', 'connect', ssid, 'ifname', wifi]
      if password:
        cmd += ['password', password]
      run(*cmd, timeout=90, secret=password or None)
    return self._wifi_action(f'connect to "{ssid}"', fn)

  def wifi_disconnect(self, msg):
    wifi = self.cfg['wifi']
    def fn():
      # Stay off after a reboot too, until connected again from here.
      for p in wifi_profiles():
        if p['active']:
          run('nmcli', 'con', 'mod', p['name'], 'connection.autoconnect', 'no')
      run('nmcli', 'dev', 'disconnect', wifi, check=False)
    return self._wifi_action('disconnect', fn)

  # --- fallback hotspot -----------------------------------------------------------

  def _hotspot_start(self, why):
    hotspot_up(self.cfg)
    self.hotspot_since, self.hotspot_why = time.time(), why
    log.info('hotspot: on (%s): "%s" at %s', why, hotspot_ssid(), HOTSPOT_ADDRESS)

  def _hotspot_stop(self, why):
    hotspot_down()
    log.info('hotspot: off (%s)', why)
    self.hotspot_since, self.hotspot_until, self.hotspot_why = None, 0, None

  def hotspot(self, msg):
    """Start it by hand (to try it with a phone) or stop it. Started by hand
    it stays on for `minutes` (and while a phone is still connected); in the
    wifi mode the Pi is off its Wi-Fi network, and off the tailnet, meanwhile."""
    with self.lock:
      if self.applying or self.wifi_busy:
        return {'error': f'busy: {self.applying or self.wifi_busy}'}
      if msg.get('on', True):
        try:
          minutes = int(msg.get('minutes', 10))
        except (TypeError, ValueError):
          minutes = -1
        if not 1 <= minutes <= 60:
          return {'error': 'minutes: 1 to 60'}
        self.wifi_busy = 'starting the hotspot'
      elif not hotspot_active(self.cfg['wifi']):
        return {'error': 'the hotspot is not on'}
      else:
        self.wifi_busy = 'stopping the hotspot'
    def fn():
      if msg.get('on', True):
        self.hotspot_until = time.time() + minutes * 60
        if not hotspot_active(self.cfg['wifi']):
          self._hotspot_start(f'started by hand for {minutes} min')
      else:
        self._hotspot_stop('stopped by hand')
    threading.Thread(target=self._wifi_job, args=(self.wifi_busy, fn), daemon=True).start()
    return {'ok': True, 'wifi_busy': self.wifi_busy}

  def hotspot_auto(self, msg):
    with self.lock:
      new = copy.deepcopy(self.cfg)
      new['hotspot'] = bool(msg.get('on', True))
      validate(new)
      self.cfg = new
      save(new)
    return {'ok': True, 'hotspot': new['hotspot']}

  def keep_reachable(self):
    """Every 5 s one reachable_step(); errors are logged once."""
    complained = False
    while True:
      try:
        self.reachable_step()
        complained = False
      except Exception as e:
        if not complained:
          log.error('hotspot check failed: %s', e)
          complained = True
      time.sleep(5)

  def reachable_step(self):
    """No management connection for HOTSPOT_GRACE s: the fallback hotspot
    goes on, so a phone can still reach the Pi. While it is on: off again
    when eth0 has an address (Ethernet modes), and in every mode the saved
    Wi-Fi networks get HOTSPOT_TRY s every HOTSPOT_RETRY s, but not while a
    phone is connected. Started by hand it stays until hotspot_until (and while a
    phone is connected)."""
    cfg = self.cfg
    if self.applying or self.wifi_busy:
      self.bad_since = None
      return
    now = time.time()
    if hotspot_active(cfg['wifi']):
      if self.hotspot_since is None:            # e.g. still on after a restart of this service
        self.hotspot_since, self.hotspot_why = now, 'found on'
      by_hand = now < self.hotspot_until
      if self.hotspot_until and not by_hand and not hotspot_clients(cfg['wifi']):
        self._hotspot_stop('time is up')
      elif not by_hand and cfg['mode'] != 'wifi' and addresses(cfg['eth']):
        self._hotspot_stop(f"{cfg['eth']} has an address again")
      # In every mode: an Ethernet mode can still run its management over
      # Wi-Fi (no cable on eth0), and waiting for eth0 alone never ends then.
      elif not by_hand and now >= self.next_try and not hotspot_clients(cfg['wifi']):
        self._hotspot_stop('trying the saved Wi-Fi networks')
        end = time.time() + HOTSPOT_TRY
        while time.time() < end and not management_ok(cfg):
          time.sleep(3)
        if management_ok(cfg):
          log.info('hotspot: back on Wi-Fi "%s"', wifi_info(cfg['wifi'])['ssid'])
        else:
          self._hotspot_start('still no saved Wi-Fi network')
        self.next_try = time.time() + HOTSPOT_RETRY
      self.bad_since = None
    elif management_ok(cfg):
      self.bad_since = None
    elif cfg.get('hotspot', True):
      self.bad_since = self.bad_since or now
      if now - self.bad_since >= HOTSPOT_GRACE:
        self._hotspot_start(f'no management connection for {HOTSPOT_GRACE} s')
        self.next_try = time.time() + HOTSPOT_RETRY
        self.bad_since = None

  def wifi_rescan(self, msg):
    run('nmcli', 'dev', 'wifi', 'rescan', 'ifname', self.cfg['wifi'], check=False)  # results show up in a few seconds
    return {'ok': True}

  def status(self):
    cfg = self.cfg
    ai = artnet_iface(cfg)
    warnings = []
    net = ipaddress.ip_network(cfg['subnet'])
    if cfg['mode'] != 'shared':
      try:
        with open(ISLAND_CONFIG, 'rb') as f:
          poles = tomllib.load(f).get('island', {}).get('poles', [])
        if poles == 'auto':
          poles = []   # follows the network by itself
        off = [p for p in poles if ipaddress.ip_address(str(p).split(':')[0]) not in net]
        if off:
          warnings.append(f"the controller sends to {', '.join(off)}, which is not on the Art-Net network {net}")
      except (OSError, ValueError, tomllib.TOMLDecodeError):
        pass
    elif cfg['reservations']:
      warnings.append('reservations are only used in the wifi and vlan modes')
    dhcp_active = run('systemctl', 'is-active', DHCP_UNIT, check=False).strip() == 'active'
    return {
      'kind': 'artnet-network',
      'mode': cfg['mode'],
      'modes': [{'id': k, 'label': v[0], 'description': v[1]} for k, v in MODES.items()],
      'applying': self.applying,
      'last_change': self.last,
      'config': {k: cfg[k] for k in ('vlan_id', 'subnet', 'pi_address', 'dhcp_start', 'dhcp_end', 'lease', 'reservations')},
      'defaults': {k: DEFAULTS[k] for k in ('vlan_id', 'subnet', 'pi_address', 'dhcp_start', 'dhcp_end', 'lease')},
      'interfaces': {
        'management': management_iface(cfg),
        'artnet': ai if cfg['mode'] != 'shared' else cfg['eth'],
        'eth': {'name': cfg['eth'], 'ipv4': addresses(cfg['eth']), 'link': carrier(cfg['eth'])},
        'wifi': {'name': cfg['wifi'], **wifi_info(cfg['wifi']),
                 'profiles': wifi_profiles(), 'visible': wifi_visible(cfg['wifi'])},
        'artnet_ipv4': addresses(ai) if cfg['mode'] != 'shared' else [],
      },
      'dhcp': {'active': dhcp_active, 'leases': leases() if dhcp_active or cfg['mode'] != 'shared' else []},
      'wifi_busy': self.wifi_busy,
      'wifi_last': self.wifi_last,
      'hotspot': {
        'auto': cfg.get('hotspot', True),
        'active': hotspot_active(cfg['wifi']),
        'ssid': hotspot_ssid(),
        'address': HOTSPOT_ADDRESS,
        'since': self.hotspot_since,
        'why': self.hotspot_why,
        'until': self.hotspot_until or None,
        'clients': hotspot_clients(cfg['wifi']) if self.hotspot_since else 0,
      },
      'warnings': warnings,
    }

  def handle(self, msg):
    cmd = msg.get('cmd')
    try:
      if cmd == 'status':
        return self.status()
      if cmd == 'set_config':
        return self.set_config(msg)
      if cmd == 'add_reservation':
        return self.add_reservation(msg)
      if cmd == 'remove_reservation':
        return self.remove_reservation(msg)
      if cmd in ('wifi_connect', 'wifi_disconnect', 'wifi_rescan', 'hotspot', 'hotspot_auto'):
        return getattr(self, cmd)(msg)
      return {'error': f'unknown command {cmd!r}'}
    except ValueError as e:
      return {'error': str(e)}

  def keep_dhcp_running(self):
    """Every few seconds: the DHCP server runs whenever the mode needs one,
    also when the Art-Net link came up after boot (pole switch switched on
    later) or dnsmasq stopped. dnsmasq itself (bind-dynamic) follows the
    address appearing and disappearing. The network is left as it is."""
    complained = False
    while True:
      try:
        if not self.applying and self.cfg['mode'] != 'shared' and \
            run('systemctl', 'is-active', DHCP_UNIT, check=False).strip() != 'active':
          log.info('DHCP server not running, starting it')
          dhcp_on(self.cfg)
          complained = False
      except Exception as e:
        if not complained:
          log.error('DHCP server not started: %s', e)
          complained = True
      time.sleep(5)


def main():
  logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
  os.makedirs(STATE, exist_ok=True)
  nc = NetConf()
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  sock.bind(CONTROL)
  threading.Thread(target=nc.keep_dhcp_running, daemon=True).start()
  threading.Thread(target=nc.keep_reachable, daemon=True).start()
  log.info('netconf: mode %s, control %s:%d', nc.cfg['mode'], *CONTROL)
  while True:
    data, peer = sock.recvfrom(4096)
    try:
      reply = nc.handle(json.loads(data))
    except Exception as e:
      log.exception('command failed')
      reply = {'error': str(e)}
    sock.sendto(json.dumps(reply).encode(), peer)


if __name__ == '__main__':
  main()
