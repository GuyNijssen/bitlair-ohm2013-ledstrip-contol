# Changes in this fork

Fork of [AlbertVos/bitlair-ohm2013-ledstrip-contol](https://github.com/AlbertVos/bitlair-ohm2013-ledstrip-contol).
Bug fixes and improvements are tracked separately so bug fixes can be
offered upstream on their own. Each bug fix lives on its own `fix/*`
branch based on upstream `master`, ready for a pull request.

## Bug fixes (to report upstream)

| Branch | File | Problem | Status |
|---|---|---|---|
| `fix/eyes-visibility` | `singleSleeve/eyes.py` | Leftover `on = [2, 2, 1]` made eyes max 2/255 brightness (look off); standalone `Strip2D(10, 10)` blanked pixels 100–149; 10 s wake-up delay; off-by-one CLI parsing of pairs/distance | Fixed, not yet sent upstream |
| `fix/simstrip-python-exe` | `tools/simstrip.py` | Child windows were started as `python3 simstrip.py`: on Windows `python3` is the Microsoft Store placeholder so no window opened, and the relative path only worked from `tools/`. Now uses `sys.executable` and the script's absolute path | Fixed, not yet sent upstream |

## Known bugs, not fixed yet

- `fire2.py`, `eyes.py`, `mqtt_fire.py` call `termios` on stdin, so they
  crash (`termios.error: Inappropriate ioctl for device`) when run
  without a terminal (systemd service). `effectsRandomSingle.py`
  includes `Fire2`, so a headless random rotation eventually dies.
- `UI.py` never shows a button for `mqtt_fire.py`: it looks for class
  `Mqtt_fire` but the class is `Mqtt_Fire`.

## Improvements (fork-specific)

_None yet._
