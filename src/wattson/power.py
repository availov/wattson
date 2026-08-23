"""Audit of what drains the battery.

Measured exactly: total draw from the battery (``power_now``), CPU package
power (RAPL, readable by root only), discrete GPU chip power
(``nvidia-smi``), process load and interrupt rate.

Estimated: the contribution of the screen. Such rows are marked as an
estimate — panels differ far too much for a single coefficient, and only a
meter on the power rail gives an exact figure.

Rows where everything is fine never reach the report: only what really
draws power or is configured away from saving shows up.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import core
from .i18n import translate

SAMPLE_SECONDS = 4
CLK_TCK = os.sysconf('SC_CLK_TCK')

DRM_ROOT = Path('/sys/class/drm')
PCI_ROOT = Path('/sys/bus/pci/devices')
BACKLIGHT_ROOT = Path('/sys/class/backlight')
RAPL_PACKAGE = Path('/sys/class/powercap/intel-rapl:0')
ASPM_POLICY = Path('/sys/module/pcie_aspm/parameters/policy')

# Thresholds: anything below them is left out to keep the report readable
MIN_PROCESS_PCT = 2.0       # % of a single core
MAX_PROCESS_ROWS = 8
WAKEUPS_WARN = 1500         # interrupts per second, all sources together
BRIGHTNESS_WARN = 70        # % of backlight
REFRESH_WARN = 61           # Hz on the built-in panel

# Rough coefficients for the screen, an order of magnitude for a 15–17" IPS;
# accuracy here is limited by nature, hence the rows are marked as estimates.
HZ_EXTRA_WATTS = 0.02       # per Hz above 60
BACKLIGHT_FULL_WATTS = 4.0  # backlight at 100 %

SEVERITY_HIGH = 3
SEVERITY_MEDIUM = 2
SEVERITY_LOW = 1


@dataclass
class Finding:
    """A single row of the report."""

    source: str
    state: str
    advice: str
    watts: float | None = None
    load_pct: float | None = None   # % of one core, when this is a process
    exact: bool = True              # False means an estimate, not a measurement
    severity: int = SEVERITY_MEDIUM

    def contribution(self) -> str:
        """The "contribution" column: watts, load or a dash."""
        parts = []
        if self.watts is not None:
            parts.append(translate('{watts:.1f} W', watts=self.watts) if self.exact
                         else translate('≈{watts:.1f} W', watts=self.watts))
        if self.load_pct is not None:
            parts.append(translate('{load:.0f} % of a core', load=self.load_pct))
        return '  '.join(parts) if parts else '—'

    def sort_key(self) -> tuple:
        return (-(self.watts or 0), -(self.load_pct or 0), -self.severity)

    def to_dict(self) -> dict:
        return {
            'source': self.source,
            'state': self.state,
            'advice': self.advice,
            'watts': self.watts,
            'load_pct': self.load_pct,
            'exact': self.exact,
            'severity': self.severity,
        }


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    total_watts: float | None = None     # from the battery, measured
    package_watts: float | None = None   # CPU package, RAPL, measured
    on_battery: bool = False
    hours_left: float | None = None

    def summary(self) -> str:
        if self.total_watts is None:
            if not self.on_battery:
                return translate('The laptop runs on the adapter — the total draw is '
                                 'not measured. Unplug it to see watts.')
            return translate('The battery does not report instant power — the total '
                             'draw is unavailable on this machine.')
        text = translate('From the battery right now: {watts:.1f} W', watts=self.total_watts)
        if self.hours_left is not None:
            text += translate(' — about {hours:.1f} h left', hours=self.hours_left)
        if self.package_watts is not None:
            text += translate('.  CPU package: {watts:.1f} W', watts=self.package_watts)
        return text

    def to_dict(self) -> dict:
        return {
            'total_watts': self.total_watts,
            'package_watts': self.package_watts,
            'on_battery': self.on_battery,
            'hours_left': self.hours_left,
            'notes': list(self.notes),
            'findings': [finding.to_dict() for finding in self.findings],
        }


def report_from_dict(data: dict) -> Report:
    """Back from JSON — this is how the GUI takes a report from a root run.

    The texts arrive already translated, because the privileged child is
    started with the language of the interface.

    :param data: payload produced by :meth:`Report.to_dict`.
    """
    report = Report(
        total_watts=data.get('total_watts'),
        package_watts=data.get('package_watts'),
        on_battery=bool(data.get('on_battery')),
        hours_left=data.get('hours_left'),
        notes=list(data.get('notes') or []),
    )
    for raw in data.get('findings') or []:
        report.findings.append(Finding(
            source=str(raw.get('source', '')),
            state=str(raw.get('state', '')),
            advice=str(raw.get('advice', '')),
            watts=raw.get('watts'),
            load_pct=raw.get('load_pct'),
            exact=bool(raw.get('exact', True)),
            severity=int(raw.get('severity', SEVERITY_MEDIUM)),
        ))
    return report


# --------------------------------------------------------------------------
# reading small things
# --------------------------------------------------------------------------

def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    value = _read(path)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _on_battery() -> bool:
    """Adapter unplugged? Look for any Mains supply with online=1."""
    try:
        entries = sorted(core.BATTERY_ROOT.iterdir())
    except OSError:
        return False
    for entry in entries:
        if _read(entry / 'type') == 'Mains' and _read(entry / 'online') == '1':
            return False
    return True


# --------------------------------------------------------------------------
# snapshots for measuring deltas
# --------------------------------------------------------------------------

def _process_name(entry: Path, comm: str) -> str:
    """comm in /proc is cut to 15 characters, so cmdline is tried as well."""
    try:
        argv = entry.joinpath('cmdline').read_bytes().split(b'\0')
    except OSError:
        return comm
    for token in argv:
        if not token or token.startswith(b'-'):
            continue
        name = os.path.basename(token.decode('utf-8', 'replace'))
        if name and not name.endswith(('.sh', '.so')):
            return name if len(name) >= len(comm) else comm
    return comm


def _process_snapshot() -> dict[str, tuple[int, str]]:
    """{pid: (CPU ticks, name)} for every process."""
    snapshot = {}
    try:
        entries = list(Path('/proc').iterdir())
    except OSError:
        return snapshot
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / 'stat').read_text()
            # comm inside the brackets may contain spaces — cut at the last one
            comm = stat[stat.index('(') + 1:stat.rindex(')')]
            rest = stat[stat.rindex(')') + 2:].split()
            ticks = int(rest[11]) + int(rest[12])   # utime + stime
        except (OSError, ValueError, IndexError):
            continue
        snapshot[entry.name] = (ticks, _process_name(entry, comm))
    return snapshot


def _interrupt_snapshot() -> dict[str, tuple[int, str]]:
    """{key: (sum over all CPUs, description)}."""
    snapshot = {}
    text = _read(Path('/proc/interrupts'))
    if text is None:
        return snapshot
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        key = parts[0].rstrip(':')
        counts, tail = [], []
        for token in parts[1:]:
            if token.isdigit() and not tail:
                counts.append(int(token))
            else:
                tail.append(token)
        if not counts:
            continue
        snapshot[key] = (sum(counts), ' '.join(tail) or key)
    return snapshot


def _rapl_snapshot() -> int | None:
    return _read_int(RAPL_PACKAGE / 'energy_uj')


def _rapl_watts(before: int | None, after: int | None, seconds: float) -> float | None:
    if before is None or after is None or seconds <= 0:
        return None
    delta = after - before
    if delta < 0:   # the counter wrapped around
        limit = _read_int(RAPL_PACKAGE / 'max_energy_range_uj')
        if limit is None:
            return None
        delta += limit
    return delta / 1e6 / seconds


# --------------------------------------------------------------------------
# graphics cards and displays
# --------------------------------------------------------------------------

def _gpu_cards() -> dict[str, tuple[str, str]]:
    """{card1: (pci address, vendor)} for every drm card."""
    cards = {}
    for card in sorted(DRM_ROOT.glob('card[0-9]')):
        device = (card / 'device').resolve()
        cards[card.name] = (device.name, _read(device / 'vendor') or '')
    return cards


def _connected_outputs() -> list[tuple[str, str, str]]:
    """[(connector, card, vendor), ...] for every connected output."""
    cards = _gpu_cards()
    outputs = []
    for connector in sorted(DRM_ROOT.glob('card[0-9]-*')):
        if _read(connector / 'status') != 'connected':
            continue
        card = connector.name.split('-', 1)[0]
        pci, vendor = cards.get(card, ('', ''))
        outputs.append((connector.name, card, vendor))
    return outputs


def _discrete_gpu() -> tuple[str, str] | None:
    """(pci address, runtime_status) of a discrete NVIDIA/AMD card."""
    for card, (pci, vendor) in _gpu_cards().items():
        if vendor in ('0x10de', '0x1002') and pci != '0000:00:02.0':
            status = _read(PCI_ROOT / pci / 'power' / 'runtime_status') or translate('unknown')
            return pci, status
    return None


def _nvidia_power() -> float | None:
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=power.draw', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return float(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def _current_modes() -> list[tuple[str, float, bool]]:
    """[(connector, Hz, built-in), ...] through GNOME Mutter."""
    try:
        from gi.repository import Gio
    except Exception:
        return []
    try:
        proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
            'org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
            'org.gnome.Mutter.DisplayConfig', None,
        )
        reply = proxy.call_sync('GetCurrentState', None, Gio.DBusCallFlags.NONE, 3000, None)
        _serial, monitors, _logical, _props = reply.unpack()
    except Exception:
        return []

    modes = []
    for description, mode_list, properties in monitors:
        connector = description[0]
        builtin = bool(properties.get('is-builtin'))
        for mode in mode_list:
            if mode[6].get('is-current'):
                modes.append((connector, float(mode[3]), builtin))
                break
    return modes


# --------------------------------------------------------------------------
# analysers: each one returns problem rows only
# --------------------------------------------------------------------------

def _check_discrete_gpu() -> list[Finding]:
    discrete = _discrete_gpu()
    if discrete is None:
        return []
    pci, status = discrete
    if status == 'suspended':
        return []   # asleep and drawing nothing — nothing to report

    watts = _nvidia_power()
    outputs = [name for name, _card, vendor in _connected_outputs()
               if vendor in ('0x10de', '0x1002')]
    if outputs:
        advice = translate(
            'An external screen hangs off the discrete card ({outputs}), which keeps '
            'it out of D3cold. On battery unplug it, or move it to a port wired to '
            'the integrated graphics (USB-C).',
            outputs=', '.join(outputs),
        )
    else:
        advice = translate(
            'No external screens on it, so an application is holding the card — look '
            'for whatever opened a CUDA/OpenGL context and close it.'
        )

    state = translate('{status}, awake', status=status)
    if watts:
        state += translate(', chip {watts:.1f} W', watts=watts)

    return [Finding(
        source=translate('Discrete graphics card'),
        state=state,
        advice=advice,
        watts=watts,
        exact=watts is not None,
        severity=SEVERITY_HIGH,
    )]


def _check_displays() -> list[Finding]:
    findings = []
    for connector, hz, builtin in _current_modes():
        if builtin and hz >= REFRESH_WARN:
            extra = (hz - 60) * HZ_EXTRA_WATTS
            findings.append(Finding(
                source=translate('Refresh rate of {connector}', connector=connector),
                state=translate('{hz:.0f} Hz', hz=hz),
                advice=translate(
                    'On battery switch to 60 Hz: Settings → Displays → Refresh Rate. '
                    'Resolution barely affects consumption, the refresh rate does.'
                ),
                watts=extra,
                exact=False,
                severity=SEVERITY_HIGH,
            ))

    for panel in sorted(BACKLIGHT_ROOT.iterdir()) if BACKLIGHT_ROOT.exists() else []:
        now = _read_int(panel / 'brightness')
        top = _read_int(panel / 'max_brightness')
        if now is None or not top:
            continue
        percent = now * 100 // top
        if percent < BRIGHTNESS_WARN:
            continue
        findings.append(Finding(
            source=translate('Backlight brightness'),
            state=translate('{percent} %', percent=percent),
            advice=translate(
                'The backlight is one of the biggest consumers. Dropping it by 30 % '
                'noticeably extends runtime.'
            ),
            watts=percent / 100 * BACKLIGHT_FULL_WATTS,
            exact=False,
            severity=SEVERITY_MEDIUM,
        ))
    return findings


def _check_processes(before: dict, after: dict, seconds: float,
                     package_watts: float | None) -> list[Finding]:
    loads = []
    for pid, (ticks, name) in after.items():
        old = before.get(pid)
        if old is None:
            continue
        delta = ticks - old[0]
        if delta <= 0:
            continue
        percent = delta / CLK_TCK / seconds * 100
        if percent >= MIN_PROCESS_PCT:
            loads.append((percent, pid, name))
    loads.sort(reverse=True)

    busy = sum(percent for percent, _pid, _name in loads)
    findings = []
    for percent, pid, name in loads[:MAX_PROCESS_ROWS]:
        watts = None
        if package_watts is not None and busy > 0:
            watts = package_watts * percent / busy
        # lifetime total: tells a stuck loop apart from a short burst
        hours = after[pid][0] / CLK_TCK / 3600
        if percent >= 25:
            advice = translate(
                'Keeps a core busy almost all the time — the largest contribution in '
                'the list. Look at what it is doing before fixing anything else.'
            )
            severity = SEVERITY_HIGH
        else:
            advice = translate(
                'Background load keeps the package out of deep sleep. Check whether '
                'the process needs to run at all.'
            )
            severity = SEVERITY_LOW
        findings.append(Finding(
            source=translate('{name} (pid {pid})', name=name, pid=pid),
            state=translate('{hours:.1f} h of CPU time so far', hours=hours),
            advice=advice,
            watts=watts,
            load_pct=percent,
            exact=False,
            severity=severity,
        ))
    return findings


def _check_wakeups(before: dict, after: dict, seconds: float) -> list[Finding]:
    rates = []
    for key, (count, label) in after.items():
        old = before.get(key)
        if old is None:
            continue
        rate = (count - old[0]) / seconds
        if rate >= 1:
            rates.append((rate, label))
    if not rates:
        return []
    total = sum(rate for rate, _label in rates)
    if total < WAKEUPS_WARN:
        return []
    rates.sort(reverse=True)
    top = ', '.join(translate('{label} {rate:.0f}/s', label=label, rate=rate)
                    for rate, label in rates[:3])
    return [Finding(
        source=translate('Wakeups (interrupts)'),
        state=translate('{total:.0f}/s — a lot', total=total),
        advice=translate(
            'Main sources: {top}. Frequent wakeups keep the package from reaching '
            'deep C-states. Remove the background processes at the top of the list.',
            top=top,
        ),
        severity=SEVERITY_MEDIUM,
    )]


def _check_cstates() -> list[Finding]:
    states = sorted((Path('/sys/devices/system/cpu/cpu0/cpuidle')).glob('state*'))
    names = [_read(state / 'name') or '' for state in states]
    if not names:
        return []
    if any(name.startswith(('C6', 'C8', 'C10')) for name in names):
        return []   # native deep states are there — all good
    return [Finding(
        source=translate('Deep C-states'),
        state=translate('only {names}', names=', '.join(name for name in names if name)),
        advice=translate(
            'intel_idle fell back to the ACPI tables instead of the native states, so '
            'the package never goes below C3 and the uncore stays powered. The cure is '
            'a firmware or kernel update; there is no quick fix.'
        ),
        severity=SEVERITY_MEDIUM,
    )]


def _check_settings() -> list[Finding]:
    findings = []

    profile = core.platform_profile()
    if profile == 'performance':
        findings.append(Finding(
            source=translate('Platform profile'),
            state=profile,
            advice=translate(
                'On battery switch to balanced or quiet — that lifts the power limits '
                'and the fan speed right away.'
            ),
            severity=SEVERITY_HIGH,
        ))

    epp = _read(Path('/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference'))
    if epp == 'performance':
        findings.append(Finding(
            source=translate('EPP (HWP policy)'),
            state=epp,
            advice=translate(
                'Switch it to balance_power: '
                'for c in /sys/devices/system/cpu/cpu*/cpufreq/'
                'energy_performance_preference; do echo balance_power | sudo tee $c; done'
            ),
            severity=SEVERITY_MEDIUM,
        ))

    aspm = _read(ASPM_POLICY)
    if aspm and '[powersave]' not in aspm and '[powersupersave]' not in aspm:
        active = re.search(r'\[(\w+)\]', aspm)
        findings.append(Finding(
            source=translate('PCIe ASPM'),
            state=active.group(1) if active else aspm,
            advice=translate(
                'The PCIe lanes are not saving power. Add pcie_aspm.policy=powersave '
                'to the kernel parameters.'
            ),
            severity=SEVERITY_LOW,
        ))

    stuck = []
    for device in sorted(PCI_ROOT.iterdir()) if PCI_ROOT.exists() else []:
        if _read(device / 'power' / 'control') == 'on':
            stuck.append(device.name)
    if stuck:
        findings.append(Finding(
            source=translate('PCI devices without runtime PM'),
            state=translate(
                '{count}: {names}',
                count=len(stuck),
                names=', '.join(stuck[:4]) + (' …' if len(stuck) > 4 else ''),
            ),
            advice=translate(
                'These devices never fall asleep. powertop turns the automation on: '
                'sudo apt install powertop && sudo powertop --auto-tune'
            ),
            severity=SEVERITY_MEDIUM,
        ))

    return findings


# --------------------------------------------------------------------------
# the full run
# --------------------------------------------------------------------------

def analyze(seconds: float = SAMPLE_SECONDS) -> Report:
    """Sample for ``seconds`` and analyse. Blocks — call it from a worker.

    :param seconds: length of the measurement window.
    """
    report = Report()
    report.on_battery = _on_battery()

    battery_before = core.read_battery()
    processes_before = _process_snapshot()
    interrupts_before = _interrupt_snapshot()
    rapl_before = _rapl_snapshot()
    started = time.monotonic()

    time.sleep(seconds)

    elapsed = time.monotonic() - started
    rapl_after = _rapl_snapshot()
    interrupts_after = _interrupt_snapshot()
    processes_after = _process_snapshot()
    battery_after = core.read_battery()

    report.package_watts = _rapl_watts(rapl_before, rapl_after, elapsed)
    if report.package_watts is None:
        report.notes.append(translate(
            'CPU package power was not measured: RAPL is readable by root only. '
            'Exact watts come from "sudo wattson power".'
        ))

    powers = [info.power for info in (battery_before, battery_after)
              if info is not None and info.power]
    if powers and report.on_battery:
        report.total_watts = sum(powers) / len(powers)
        if battery_after is not None and battery_after.now:
            report.hours_left = battery_after.now / report.total_watts
    elif not report.on_battery:
        report.notes.append(translate(
            'The laptop runs on the adapter: the battery does not report the total draw.'
        ))
    else:
        report.notes.append(translate(
            'The battery exposes neither power_now nor current_now — system watts '
            'cannot be measured, but everything else in the report is computed.'
        ))

    report.findings.extend(_check_discrete_gpu())
    report.findings.extend(_check_displays())
    report.findings.extend(_check_processes(processes_before, processes_after,
                                            elapsed, report.package_watts))
    report.findings.extend(_check_wakeups(interrupts_before, interrupts_after, elapsed))
    report.findings.extend(_check_cstates())
    report.findings.extend(_check_settings())

    report.findings.sort(key=Finding.sort_key)
    return report
