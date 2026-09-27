#!/bin/sh
# Install the island controller as a pi-agent module. Run as root on the Pi
# from a checkout of this repo:
#
#   sudo deploy/install.sh              # code, config, service (not started)
#   sudo deploy/install.sh --enable     # also start at boot
#   sudo deploy/install.sh --network    # also eth0 = 192.168.89.1/24 + dnsmasq
#
# --network takes eth0 off whatever network it is on now. Only use it when
# the Pi is reachable over WiFi or Tailscale.
set -eu

SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/ledpoles
ENABLE=0
NETWORK=0
for arg in "$@"; do
  case "$arg" in
    --enable) ENABLE=1 ;;
    --network) NETWORK=1 ;;
    *) echo "unknown option: $arg" >&2; exit 1 ;;
  esac
done

id ledpoles >/dev/null 2>&1 || useradd --system --home-dir "$DEST" --shell /usr/sbin/nologin ledpoles

# Code: replace in full, no stray files left behind.
rm -rf "$DEST.new"
mkdir -p "$DEST.new"
(cd "$SRC" && tar --exclude=.git -cf - .) | tar -xf - -C "$DEST.new"
rm -rf "$DEST"
mv "$DEST.new" "$DEST"
chown -R root:root "$DEST"

# Config: never overwrite an edited one.
install -d -m 755 /etc/ledpoles
[ -f /etc/ledpoles/island.toml ] || install -m 644 "$SRC/island/island.example.toml" /etc/ledpoles/island.toml

install -m 644 "$SRC/deploy/pimod-artnet.service" /etc/systemd/system/pimod-artnet.service
if [ -d /etc/pi-agent/modules.d ]; then
  install -m 644 "$SRC/deploy/pi-agent-module.toml" /etc/pi-agent/modules.d/artnet.toml
else
  echo "note: pi-agent not installed, module manifest skipped"
fi
systemctl daemon-reload
if systemctl is-active --quiet pimod-artnet.service; then
  systemctl restart pimod-artnet.service
fi
[ "$ENABLE" = 1 ] && systemctl enable pimod-artnet.service

if [ "$NETWORK" = 1 ]; then
  apt-get install -y dnsmasq
  install -m 644 "$SRC/deploy/dnsmasq-poles.conf" /etc/dnsmasq.d/poles.conf
  # Drop other profiles bound to eth0, then a static, no-gateway profile.
  nmcli -t -f NAME,DEVICE con show | awk -F: '$2=="eth0"{print $1}' | while read -r c; do
    [ "$c" = poles ] || nmcli con mod "$c" connection.autoconnect no
  done
  nmcli con show poles >/dev/null 2>&1 || nmcli con add type ethernet ifname eth0 con-name poles
  nmcli con mod poles ipv4.method manual ipv4.addresses 192.168.89.1/24 \
    ipv4.never-default yes ipv6.method disabled connection.autoconnect yes
  nmcli con up poles
  systemctl enable --now dnsmasq
  systemctl restart dnsmasq
fi

echo "installed. config: /etc/ledpoles/island.toml  service: pimod-artnet.service"
