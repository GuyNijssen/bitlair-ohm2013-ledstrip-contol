# Changes in this fork

Fork of [AlbertVos/bitlair-ohm2013-ledstrip-contol](https://github.com/AlbertVos/bitlair-ohm2013-ledstrip-contol).
Bug fixes and improvements are tracked separately so bug fixes can be
offered upstream on their own. Each bug fix lives on its own `fix/*`
branch based on upstream `master`, ready for a pull request.

## Bug fixes (to report upstream)

| Branch | File | Problem | Status |
|---|---|---|---|
| `fix/eyes-visibility` | `singleSleeve/eyes.py` | Leftover `on = [2, 2, 1]` made eyes max 2/255 brightness (look off); standalone `Strip2D(10, 10)` blanked pixels 100–149; 10 s wake-up delay; off-by-one CLI parsing of pairs/distance | Fixed, not yet sent upstream |

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
| `feat/island-controller` | `island/controller.py`: headless island controller. TOML config (poles, brightness cap, idle effect, cue list), idle → cue → idle state machine, keeps streaming at all times, crash fallback to idle and then a built-in `glow`, preemptive effect switching, JSON-over-UDP control port on localhost. Refuses `fire2`/`eyes`/`mqtt_fire` until the headless-input fix. `island/test_controller.py` checks header, brightness cap, keep-alive and state machine without poles. `deploy/`: systemd unit `pimod-artnet.service`, pi-agent module manifest, dnsmasq config, `install.sh` (`--enable`, `--network`) and `uninstall.sh`. |
