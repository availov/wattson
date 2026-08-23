# Wattson

Fan curve, battery charge limit and a power-drain audit for Linux laptops.
One GTK4 window plus a command line.

## Install

```
curl -fsSL https://raw.githubusercontent.com/availov/wattson/main/install.sh | sudo bash
```

Or from a clone:

```
git clone https://github.com/availov/wattson.git
cd wattson
sudo ./install.sh
```

Needs `python3` and `python3-gi` with GTK 4 — both are already there on
Ubuntu 24.04. Nothing is pulled from PyPI. Re-run `sudo ./install.sh` after
changing the sources; it rebuilds the binary every time.

## Use

Run `wattson`, or find Wattson in the application menu. Three tabs:

| Tab | What you get | Works on |
|---|---|---|
| **Fans** | minimum fan speed and all 8 curve points, live temperature and rpm | ASUS only (`asus_custom_fan_curve` driver) |
| **Battery** | stop charging at 80 %, plus charge, wear, watts, volts | any laptop with `charge_control_end_threshold` |
| **Power draw** | what drains the battery right now, and what to do about it | any machine |

A tab whose hardware is missing is greyed out with an explanation, the rest
keeps working. To check the charge limit on your laptop:

```
ls /sys/class/power_supply/BAT*/charge_control_end_threshold
```

The **Autostart** switch at the bottom of the window re-applies your
settings after a reboot, after resume and after a power-profile switch —
the kernel drops them at every one of those.

## Command line

```
wattson                   the window
wattson status            mode, temperature, rpm, battery, curve
wattson power             what eats the battery (with sudo: exact CPU watts)
wattson battery set 80    stop charging at 80 %
wattson battery off       charge up to 100 % again
wattson apply --floor 10  never let the fan drop below 10 %
wattson reset             back to the factory curve
wattson autostart on|off|status
wattson config show|init
```

`status`, `battery` and `power` also take `--json` for scripts. Anything
that needs root raises its privileges through pkexec on its own. Settings
live in `/etc/wattson.json`.

## Uninstall

```
curl -fsSL https://raw.githubusercontent.com/availov/wattson/main/uninstall.sh | sudo bash
```

It removes everything and puts back the factory fan curve and charging to
100 %.

## Good to know

- The fan floor is there to stop the pulsing: the factory curve keeps the
  fan off until about 60 °C and then spins it up hard, so at idle it starts
  and stops all the time.
- 80 % is the point of the charge limit: a lithium cell ages fastest while
  sitting full, even without charge cycles.
- Once the threshold is reached the battery state turns into
  `Not charging` — that is normal behaviour, not a fault.
- The power tab lists problems only, so an empty table means there is
  nothing to fix.
- Coming from `asus-fan-control`: the installer migrates the settings and
  removes the old version by itself.
