#!/bin/sh
# Install the island controller and the Art-Net network module as pi-agent
# modules. Run as root on the Pi from a checkout of this repo:
#
#   sudo deploy/install.sh              # code, config, services
#   sudo deploy/install.sh --enable     # also start the controller at boot
#
# The network module (pimod-artnet-net) is started and enabled, but changes
# nothing until a mode is applied from the pi-agent page. Network config:
# /etc/ledpoles/network.json (written by the module).
set -eu

SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/ledpoles
ENABLE=0
for arg in "$@"; do
  case "$arg" in
    --enable) ENABLE=1 ;;
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

# The DHCP server only needs the dnsmasq binary (dnsmasq-base); the full
# dnsmasq package would also start a DNS server on every interface.
command -v dnsmasq >/dev/null || apt-get install -y dnsmasq-base

install -m 644 "$SRC/deploy/pimod-artnet.service" "$SRC/deploy/pimod-artnet-net.service" \
  "$SRC/deploy/ledpoles-dhcp.service" /etc/systemd/system/
if [ -d /etc/pi-agent/modules.d ]; then
  install -m 644 "$SRC/deploy/pi-agent-module.toml" /etc/pi-agent/modules.d/artnet.toml
  install -m 644 "$SRC/deploy/pi-agent-module-net.toml" /etc/pi-agent/modules.d/artnet-net.toml
else
  echo "note: pi-agent not installed, module manifests skipped"
fi
systemctl daemon-reload
if systemctl is-active --quiet pimod-artnet.service; then
  systemctl restart pimod-artnet.service
fi
[ "$ENABLE" = 1 ] && systemctl enable pimod-artnet.service
systemctl enable pimod-artnet-net.service
systemctl restart pimod-artnet-net.service

echo "installed. config: /etc/ledpoles/island.toml  services: pimod-artnet, pimod-artnet-net"
