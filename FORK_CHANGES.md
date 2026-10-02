# Changes in this fork

Fork of [AlbertVos/bitlair-ohm2013-ledstrip-contol](https://github.com/AlbertVos/bitlair-ohm2013-ledstrip-contol).
Bug fixes and improvements are tracked separately so bug fixes can be
offered upstream on their own. Each bug fix lives on its own `fix/*`
branch based on upstream `master`, ready for a pull request.

## Bug fixes (to report upstream)

| Branch | File | Problem | Status |
|---|---|---|---|
| `fix/eyes-visibility` | `singleSleeve/eyes.py` | Leftover `on = [2, 2, 1]` made eyes max 2/255 brightness (look off); standalone `Strip2D(10, 10)` blanked pixels 100–149; 10 s wake-up delay; off-by-one CLI parsing of pairs/distance | PR [#7](https://github.com/AlbertVos/bitlair-ohm2013-ledstrip-contol/pull/7) open |
| `fix/simstrip-python-exe` | `tools/simstrip.py` | Child windows were started as `python3 simstrip.py`: on Windows `python3` is the Microsoft Store placeholder so no window opened, and the relative path only worked from `tools/`. Now uses `sys.executable` and the script's absolute path | PR [#6](https://github.com/AlbertVos/bitlair-ohm2013-ledstrip-contol/pull/6) open |

## Known bugs, not fixed yet

- `fire2.py`, `eyes.py`, `mqtt_fire.py` call `termios` on stdin, so they
  crash (`termios.error: Inappropriate ioctl for device`) when run
  without a terminal (systemd service). `effectsRandomSingle.py`
  includes `Fire2`, so a headless random rotation eventually dies.
- `UI.py` never shows a button for `mqtt_fire.py`: it looks for class
  `Mqtt_fire` but the class is `Mqtt_Fire`.
- `weird3.py` ignores the `runtime` argument and only checks `quit` once
  per ~15 s pass, so it cannot be stopped on time. The island controller
  works around it by aborting from `send()`.
- `lib/strip.py` `Artnet.clear()` does `data = self.dataHeader` and then
  `data += ...` on a `bytearray`, which grows the shared class header in
  place. Every frame sent after a `clear()` has a broken header. Harmless
  today because `clear()` only runs at exit.

## Improvements (fork-specific)

| Branch | What |
|---|---|
| `feat/island-controller` | `island/controller.py`: headless island controller. TOML config (poles, brightness cap, idle effect, cue list), idle → cue → idle state machine, keeps streaming at all times, crash fallback to idle and then a built-in `glow`, preemptive effect switching, JSON-over-UDP control port on localhost. Refuses `fire2`/`eyes`/`mqtt_fire` until the headless-input fix. `island/test_controller.py` checks header, brightness cap, keep-alive and state machine without poles. `deploy/`: systemd unit `pimod-artnet.service`, pi-agent module manifest, `install.sh` (`--enable`) and `uninstall.sh`. |
| `feat/island-controller` | `island/netconf.py` (pi-agent module `pimod-artnet-net`, root): three network modes for the island Pi (Ethernet for management and Art-Net; Wi-Fi management with Art-Net on eth0; Ethernet management with Art-Net on a VLAN), a DHCP server for the poles (dnsmasq, Art-Net interface only, `ledpoles-dhcp.service`) with MAC→IP reservations and an editable address range (default 192.168.89.0/24, Pi .1, DHCP .2–.50). Refuses to start a DHCP server where another one answers, refuses Wi-Fi mode without working Wi-Fi internet, and rolls back if management loses internet after a change. `island/test_netconf.py`: validation and dnsmasq config tests. Replaces `install.sh --network` and `deploy/dnsmasq-poles.conf`. |
| `feat/island-controller` | Wi-Fi connect/disconnect (saved or new network, password masked in errors) while management runs over Ethernet, and a checked way back from Wi-Fi to Ethernet management (needs a link on eth0; rolls back if eth0 has no internet). `poles = "auto"` in island.toml: the controller sends to the broadcast address of the Art-Net network (wifi/vlan) or of eth0's network (shared), rechecked every 5 s, so it follows a mode change. `island/test_auto_poles.py`. |
| `feat/island-controller` | Robustness: the controller skips frames it cannot send (no route, link down) instead of crashing the effect, which restarted it ~400x per second; it logs once and reports `send_error`. The DHCP server follows the Art-Net address appearing and disappearing (dnsmasq `bind-dynamic`, unit `Restart=always`) and the network module checks every 5 s that it runs, so a pole switch switched on after the Pi, or a pulled cable, no longer leaves the poles without DHCP. `island/test_no_route.py`. |
