"""Hardware, configuration and systemd access.

All sysfs work is collected here; the GUI and the CLI are built on top of
this module.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import current_language, translate

PWM_MAX = 255
POINT_COUNT = 8

HWMON_ROOT = Path('/sys/class/hwmon')
CURVE_DRIVER = 'asus_custom_fan_curve'
FAN_DRIVER = 'asus'
# coretemp is Intel, k10temp/zenpower are AMD: plenty of ASUS models run Ryzen
TEMP_DRIVERS = ('coretemp', 'k10temp', 'zenpower')
# labels that stand for the package temperature, in order of preference
TEMP_LABELS = ('Package', 'Tctl', 'Tdie')
PROFILE_PATH = Path('/sys/firmware/acpi/platform_profile')

BATTERY_ROOT = Path('/sys/class/power_supply')
BATTERY_THRESHOLD_FILE = 'charge_control_end_threshold'

CONFIG_PATH = Path('/etc/wattson.json')
BINARY_PATH = Path('/usr/local/bin/wattson')
TIMER_UNIT = 'wattson.timer'
UNIT_PATH = Path('/etc/systemd/system') / TIMER_UNIT

DEFAULT_FLOOR_PCT = 10
# (temperature °C, speed %) — anything below the floor is raised to the floor
DEFAULT_POINTS = [(30, 0), (50, 0), (60, 2), (67, 38), (73, 47), (80, 61), (88, 80), (95, 100)]

# Charge threshold: below 20 % it makes no sense, 100 % means no limit.
DEFAULT_BATTERY_THRESHOLD = 80
MIN_BATTERY_THRESHOLD = 20

# What closing the lid does. The first three go to logind as they are; the
# last one is wattson's own: logind only locks the screen, and the service
# puts the machine to sleep once the charge is down to the threshold.
LID_SUSPEND = 'suspend'
LID_LOCK = 'lock'
LID_IGNORE = 'ignore'
LID_LOW_BATTERY = 'low-battery'
LID_BATTERY_ACTIONS = (LID_SUSPEND, LID_LOW_BATTERY, LID_LOCK, LID_IGNORE)
LID_AC_ACTIONS = (LID_SUSPEND, LID_LOCK, LID_IGNORE)
DEFAULT_LID_LOW_THRESHOLD = 15
MIN_LID_LOW_THRESHOLD = 5
MAX_LID_LOW_THRESHOLD = 50

# identifiers, not display text: they travel through JSON output
MODE_CUSTOM = 'custom'
MODE_AUTO = 'automatic'

# energy_* is watt-hours, charge_* is amp-hours; both come in micro units
UNIT_WATT_HOURS = 'Wh'
UNIT_AMP_HOURS = 'Ah'


class HardwareError(RuntimeError):
    """The needed driver is missing, or writing is not permitted."""


class PrivilegeError(RuntimeError):
    """A privileged operation could not be carried out."""

    def __init__(self, message: str, command: str = '') -> None:
        super().__init__(message)
        self.command = command


# --------------------------------------------------------------------------
# display names for values that stay identifiers on disk
# --------------------------------------------------------------------------

def mode_label(mode: str) -> str:
    """Display name of a fan mode identifier.

    :param mode: ``MODE_CUSTOM`` or ``MODE_AUTO``.
    """
    labels = {
        MODE_CUSTOM: translate('custom curve'),
        MODE_AUTO: translate('factory automatic'),
    }
    return labels.get(mode, mode)


def battery_status_label(status: str | None) -> str:
    """Display name of a kernel battery status.

    :param status: value of the sysfs ``status`` file, None if unreadable.
    """
    if status is None:
        return translate('unknown')
    labels = {
        'Charging': translate('charging'),
        'Discharging': translate('discharging'),
        'Not charging': translate('charging stopped'),
        'Full': translate('full'),
        'Unknown': translate('unknown'),
    }
    return labels.get(status, status)


def fan_label(name: str) -> str:
    """Display name of a kernel fan label.

    :param name: label reported by hwmon, e.g. ``cpu_fan``.
    """
    labels = {
        'cpu_fan': translate('CPU fan'),
        'gpu_fan': translate('GPU fan'),
    }
    return labels.get(name, name)


def unit_label(unit: str) -> str:
    """Display name of a capacity unit.

    :param unit: ``UNIT_WATT_HOURS`` or ``UNIT_AMP_HOURS``.
    """
    labels = {
        UNIT_WATT_HOURS: translate('Wh'),
        UNIT_AMP_HOURS: translate('Ah'),
    }
    return labels.get(unit, unit)


# --------------------------------------------------------------------------
# percent to raw PWM
# --------------------------------------------------------------------------

def pct_to_raw(pct: int) -> int:
    return max(0, min(PWM_MAX, int(pct * PWM_MAX / 100)))


def raw_to_pct(raw: int) -> int:
    return max(0, min(100, int(round(raw * 100 / PWM_MAX))))


# --------------------------------------------------------------------------
# finding hwmon
# --------------------------------------------------------------------------

def _hwmon_by_name(name: str) -> Path | None:
    for entry in sorted(HWMON_ROOT.glob('hwmon*')):
        try:
            if (entry / 'name').read_text().strip() == name:
                return entry
        except OSError:
            continue
    return None


def curve_dir() -> Path:
    path = _hwmon_by_name(CURVE_DRIVER)
    if path is None:
        raise HardwareError(translate(
            'driver {driver} not found — either this model is unsupported '
            'or the asus-nb-wmi module is not loaded',
            driver=CURVE_DRIVER,
        ))
    return path


def supported() -> bool:
    return _hwmon_by_name(CURVE_DRIVER) is not None


# --------------------------------------------------------------------------
# reading the current state
# --------------------------------------------------------------------------

def read_fans() -> list[tuple[str, int]]:
    """[(label, rpm), ...] for every fan."""
    directory = _hwmon_by_name(FAN_DRIVER)
    if directory is None:
        return []
    fans = []
    for idx in range(1, 5):
        source = directory / f'fan{idx}_input'
        if not source.exists():
            continue
        try:
            rpm = int(source.read_text().strip())
        except (OSError, ValueError):
            continue
        label_file = directory / f'fan{idx}_label'
        try:
            label = label_file.read_text().strip()
        except OSError:
            label = f'fan{idx}'
        fans.append((label, rpm))
    return fans


def read_cpu_temp() -> int | None:
    """Package temperature in °C, otherwise the hottest core.

    Both Intel and AMD are probed: the hwmon name and the package label
    differ between them.
    """
    directory = None
    for name in TEMP_DRIVERS:
        directory = _hwmon_by_name(name)
        if directory is not None:
            break
    if directory is None:
        return None
    hottest = None
    for source in sorted(directory.glob('temp*_input')):
        try:
            value = int(source.read_text().strip()) // 1000
        except (OSError, ValueError):
            continue
        label_file = Path(str(source)[: -len('_input')] + '_label')
        try:
            label = label_file.read_text().strip()
        except OSError:
            label = ''
        if label.startswith(TEMP_LABELS):
            return value
        hottest = value if hottest is None else max(hottest, value)
    return hottest


def platform_profile() -> str | None:
    try:
        return PROFILE_PATH.read_text().strip()
    except OSError:
        return None


def read_curve(fan: int = 1) -> list[tuple[int, int]]:
    """Curve currently held by the kernel: [(temperature, raw pwm), ...]."""
    directory = curve_dir()
    points = []
    for index in range(1, POINT_COUNT + 1):
        try:
            temp = int((directory / f'pwm{fan}_auto_point{index}_temp').read_text().strip())
            pwm = int((directory / f'pwm{fan}_auto_point{index}_pwm').read_text().strip())
        except (OSError, ValueError) as error:
            raise HardwareError(translate(
                'cannot read curve point {number}: {error}', number=index, error=error,
            )) from error
        points.append((temp, pwm))
    return points


def curve_active(fan: int = 1) -> bool:
    """True when the custom curve runs instead of the factory automatic."""
    try:
        return (curve_dir() / f'pwm{fan}_enable').read_text().strip() == '1'
    except OSError:
        return False


def current_mode() -> str:
    return MODE_CUSTOM if curve_active() else MODE_AUTO


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def _write(path: Path, value: int) -> None:
    try:
        path.write_text(f'{value}\n')
    except PermissionError as error:
        raise HardwareError(translate(
            'not allowed to write {path} — root is required', path=path,
        )) from error
    except OSError as error:
        raise HardwareError(translate(
            'error writing {path}: {error}', path=path, error=error,
        )) from error


def effective_points(floor_pct: int, points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Curve after the floor is applied, in raw PWM."""
    floor = pct_to_raw(floor_pct)
    return [(temp, max(floor, pct_to_raw(pwm))) for temp, pwm in points]


def validate(floor_pct: int, points: list[tuple[int, int]]) -> None:
    if not 0 <= floor_pct <= 100:
        raise ValueError(translate('the floor must be between 0 and 100 %'))
    if len(points) != POINT_COUNT:
        raise ValueError(translate(
            'the curve must hold exactly {count} points', count=POINT_COUNT,
        ))
    previous_temp = -1
    previous_pwm = -1
    for number, (temp, pwm) in enumerate(points, start=1):
        if not 0 <= temp <= 110:
            raise ValueError(translate(
                'point {number}: temperature {temp} is outside 0–110 °C',
                number=number, temp=temp,
            ))
        if not 0 <= pwm <= 100:
            raise ValueError(translate(
                'point {number}: speed {pwm} is outside 0–100 %',
                number=number, pwm=pwm,
            ))
        if temp <= previous_temp:
            raise ValueError(translate(
                'point {number}: temperature {temp} °C is not above the previous '
                '{previous} °C — points must rise',
                number=number, temp=temp, previous=previous_temp,
            ))
        if pwm < previous_pwm:
            raise ValueError(translate(
                'point {number}: speed {pwm} % is below the previous {previous} % — '
                'the curve must not fall',
                number=number, pwm=pwm, previous=previous_pwm,
            ))
        previous_temp, previous_pwm = temp, pwm


def curve_matches(floor_pct: int, points: list[tuple[int, int]]) -> bool:
    """Already applied? Keeps the timer from poking WMI every 30 seconds."""
    wanted = effective_points(floor_pct, points)
    try:
        for fan in (1, 2):
            if not curve_active(fan) or read_curve(fan) != wanted:
                return False
    except HardwareError:
        return False
    return True


def apply_curve(floor_pct: int, points: list[tuple[int, int]], force: bool = False) -> bool:
    """Write the curve and switch it on. True when something really changed."""
    validate(floor_pct, points)
    if not force and curve_matches(floor_pct, points):
        return False
    directory = curve_dir()
    wanted = effective_points(floor_pct, points)
    for fan in (1, 2):
        for index, (temp, pwm) in enumerate(wanted, start=1):
            _write(directory / f'pwm{fan}_auto_point{index}_temp', temp)
            _write(directory / f'pwm{fan}_auto_point{index}_pwm', pwm)
        _write(directory / f'pwm{fan}_enable', 1)
    return True


def reset_factory() -> None:
    """Restore the factory curve and automatic (enable=3 resets the points)."""
    directory = curve_dir()
    for fan in (1, 2):
        _write(directory / f'pwm{fan}_enable', 3)


# --------------------------------------------------------------------------
# battery: charge stop threshold
# --------------------------------------------------------------------------

def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _read_str(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _battery_entries() -> list[Path]:
    """Every battery in the system (usually a single BAT0)."""
    try:
        entries = sorted(BATTERY_ROOT.iterdir())
    except OSError:
        return []
    return [entry for entry in entries if _read_str(entry / 'type') == 'Battery']


def _battery_path() -> Path | None:
    """Battery that knows the charge threshold. None means no control."""
    for entry in _battery_entries():
        if (entry / BATTERY_THRESHOLD_FILE).exists():
            return entry
    return None


def _readable_battery_path() -> Path | None:
    """Any battery: charge, capacity and wear do not need the threshold.

    The split matters for portability: on a laptop without charge control
    the readings still show up and only the controls are switched off.
    """
    with_control = _battery_path()
    if with_control is not None:
        return with_control
    entries = _battery_entries()
    return entries[0] if entries else None


def battery_dir() -> Path:
    path = _battery_path()
    if path is None:
        raise HardwareError(translate(
            'no battery with {file} — the platform driver of this model does '
            'not expose the charge threshold',
            file=BATTERY_THRESHOLD_FILE,
        ))
    return path


def battery_supported() -> bool:
    return _battery_path() is not None


@dataclass
class BatteryInfo:
    """Snapshot of the battery. Capacities in Wh (or Ah), voltage in V."""

    threshold: int | None = None     # charge stop threshold, %
    capacity: int | None = None      # current charge, %
    status: str | None = None        # Charging / Discharging / Not charging / Full
    voltage: float | None = None
    power: float | None = None       # absolute charge or discharge power, W
    now: float | None = None
    full: float | None = None        # capacity at full charge today
    design: float | None = None      # capacity by the data sheet
    unit: str = UNIT_WATT_HOURS

    @property
    def health_pct(self) -> int | None:
        """How much of the factory capacity is left, %."""
        if not self.full or not self.design:
            return None
        return max(0, min(100, int(round(self.full / self.design * 100))))

    @property
    def wear_pct(self) -> int | None:
        health = self.health_pct
        return None if health is None else 100 - health

    @property
    def status_label(self) -> str:
        """Battery status in the language of the interface."""
        return battery_status_label(self.status)

    @property
    def unit_label(self) -> str:
        """Capacity unit in the language of the interface."""
        return unit_label(self.unit)


def read_battery() -> BatteryInfo | None:
    """Current battery state, None when there is no battery at all.

    Works where the charge threshold is unsupported too: ``threshold`` stays
    None and the remaining fields are read as usual.
    """
    directory = _readable_battery_path()
    if directory is None:
        return None

    info = BatteryInfo()
    info.threshold = _read_int(directory / BATTERY_THRESHOLD_FILE)
    info.capacity = _read_int(directory / 'capacity')
    info.status = _read_str(directory / 'status')

    micro_volt = _read_int(directory / 'voltage_now')
    if micro_volt is not None:
        info.voltage = micro_volt / 1e6

    # energy_* is watt-hours, charge_* is amp-hours; both in micro units
    for prefix, unit in (('energy', UNIT_WATT_HOURS), ('charge', UNIT_AMP_HOURS)):
        full = _read_int(directory / f'{prefix}_full')
        if full is None:
            continue
        design = _read_int(directory / f'{prefix}_full_design')
        now = _read_int(directory / f'{prefix}_now')
        info.unit = unit
        info.full = full / 1e6
        info.design = None if design is None else design / 1e6
        info.now = None if now is None else now / 1e6
        break

    micro_watt = _read_int(directory / 'power_now')
    if micro_watt is None:
        micro_amp = _read_int(directory / 'current_now')
        if micro_amp is not None and micro_volt is not None:
            micro_watt = int(micro_amp * micro_volt / 1e6)
    if micro_watt is not None:
        info.power = abs(micro_watt) / 1e6

    return info


def read_battery_threshold() -> int | None:
    directory = _battery_path()
    if directory is None:
        return None
    return _read_int(directory / BATTERY_THRESHOLD_FILE)


def validate_battery(threshold: int) -> None:
    if not MIN_BATTERY_THRESHOLD <= threshold <= 100:
        raise ValueError(translate(
            'the charge threshold must be between {minimum} and 100 %',
            minimum=MIN_BATTERY_THRESHOLD,
        ))


def apply_battery(threshold: int, force: bool = False) -> bool:
    """Write the charge stop threshold. True when the value really changed."""
    validate_battery(threshold)
    directory = battery_dir()
    if not force and _read_int(directory / BATTERY_THRESHOLD_FILE) == threshold:
        return False
    _write(directory / BATTERY_THRESHOLD_FILE, threshold)
    return True


def reset_battery() -> bool:
    """Lift the limit — charge all the way to 100 %."""
    return apply_battery(100)


# --------------------------------------------------------------------------
# lid: the settings only, logind itself is handled in lid.py
# --------------------------------------------------------------------------

def validate_lid_threshold(threshold: int) -> None:
    """Check the charge at which a laptop with its lid closed goes to sleep.

    :param threshold: charge in %.
    """
    if not MIN_LID_LOW_THRESHOLD <= threshold <= MAX_LID_LOW_THRESHOLD:
        raise ValueError(translate(
            'the charge to sleep at must be between {minimum} and {maximum} %',
            minimum=MIN_LID_LOW_THRESHOLD, maximum=MAX_LID_LOW_THRESHOLD,
        ))


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

@dataclass
class Config:
    enabled: bool = True
    floor_pct: int = DEFAULT_FLOOR_PCT
    points: list[tuple[int, int]] = field(default_factory=lambda: list(DEFAULT_POINTS))
    # the charge threshold is off by default: silently changing how an
    # already installed machine charges is not acceptable
    battery_enabled: bool = False
    battery_threshold: int = DEFAULT_BATTERY_THRESHOLD
    # the lid stays with the system defaults until it is set in wattson
    lid_enabled: bool = False
    lid_battery: str = LID_SUSPEND
    lid_ac: str = LID_SUSPEND
    lid_low_threshold: int = DEFAULT_LID_LOW_THRESHOLD

    def to_dict(self) -> dict:
        return {
            'enabled': self.enabled,
            'floor_pct': self.floor_pct,
            'points': [[temp, pwm] for temp, pwm in self.points],
            'battery_enabled': self.battery_enabled,
            'battery_threshold': self.battery_threshold,
            'lid_enabled': self.lid_enabled,
            'lid_battery': self.lid_battery,
            'lid_ac': self.lid_ac,
            'lid_low_threshold': self.lid_low_threshold,
        }


def load_config() -> Config:
    """Configuration from /etc; defaults when it is missing or damaged."""
    try:
        raw = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return Config()
    config = Config()
    if isinstance(raw.get('enabled'), bool):
        config.enabled = raw['enabled']
    try:
        config.floor_pct = max(0, min(100, int(raw['floor_pct'])))
    except (KeyError, TypeError, ValueError):
        pass
    points = raw.get('points')
    if isinstance(points, list) and len(points) == POINT_COUNT:
        try:
            parsed = [(int(temp), int(pwm)) for temp, pwm in points]
            validate(config.floor_pct, parsed)
            config.points = parsed
        except (TypeError, ValueError):
            pass
    if isinstance(raw.get('battery_enabled'), bool):
        config.battery_enabled = raw['battery_enabled']
    try:
        threshold = int(raw['battery_threshold'])
        validate_battery(threshold)
        config.battery_threshold = threshold
    except (KeyError, TypeError, ValueError):
        pass
    if isinstance(raw.get('lid_enabled'), bool):
        config.lid_enabled = raw['lid_enabled']
    if raw.get('lid_battery') in LID_BATTERY_ACTIONS:
        config.lid_battery = raw['lid_battery']
    if raw.get('lid_ac') in LID_AC_ACTIONS:
        config.lid_ac = raw['lid_ac']
    try:
        threshold = int(raw['lid_low_threshold'])
        validate_lid_threshold(threshold)
        config.lid_low_threshold = threshold
    except (KeyError, TypeError, ValueError):
        pass
    return config


def save_config(config: Config) -> None:
    temporary = CONFIG_PATH.with_suffix('.json.tmp')
    payload = json.dumps(config.to_dict(), indent=2, ensure_ascii=False) + '\n'
    try:
        temporary.write_text(payload)
        os.chmod(temporary, 0o644)
        os.replace(temporary, CONFIG_PATH)
    except PermissionError as error:
        raise HardwareError(translate(
            'not allowed to write {path} — root is required', path=CONFIG_PATH,
        )) from error
    except OSError as error:
        raise HardwareError(translate(
            'could not save {path}: {error}', path=CONFIG_PATH, error=error,
        )) from error


# --------------------------------------------------------------------------
# per-user preferences: /etc belongs to root, the GUI runs as the user
# --------------------------------------------------------------------------

def user_config_path() -> Path:
    """File with the preferences of the user running the interface."""
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return Path(base) / 'wattson.json'


def load_language() -> str | None:
    """Interface language chosen by this user, None when never chosen."""
    try:
        raw = json.loads(user_config_path().read_text())
    except (OSError, ValueError):
        return None
    language = raw.get('language') if isinstance(raw, dict) else None
    return language if isinstance(language, str) and language else None


def save_language(code: str) -> None:
    """Remember the interface language for this user.

    A failing write is ignored on purpose: not being able to store the
    preference must not stop the interface from switching language.

    :param code: language code to store, e.g. ``de``.
    """
    path = user_config_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw['language'] = code
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + '\n')
    except OSError:
        pass


def parse_points(text: str) -> list[tuple[int, int]]:
    """Parse a string such as ``30:0,50:0,60:2,...``."""
    points = []
    for chunk in text.split(','):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            temp, pwm = chunk.split(':')
            points.append((int(temp), int(pwm)))
        except ValueError as error:
            raise ValueError(translate(
                "cannot parse point '{chunk}', expected temperature:percent",
                chunk=chunk,
            )) from error
    return points


def format_points(points: list[tuple[int, int]]) -> str:
    return ','.join(f'{temp}:{pwm}' for temp, pwm in points)


# --------------------------------------------------------------------------
# autostart through systemd
# --------------------------------------------------------------------------

def autostart_installed() -> bool:
    return UNIT_PATH.exists()


def autostart_enabled() -> bool:
    result = subprocess.run(
        ['systemctl', 'is-enabled', TIMER_UNIT],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() == 'enabled'


def set_autostart(enabled: bool) -> None:
    if not autostart_installed():
        raise HardwareError(translate(
            'unit {unit} is not installed — run install.sh first', unit=TIMER_UNIT,
        ))
    action = ['enable', '--now'] if enabled else ['disable', '--now']
    result = subprocess.run(
        ['systemctl', *action, TIMER_UNIT],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise HardwareError(
            (result.stderr or result.stdout).strip()
            or translate('systemctl reported an error')
        )


# --------------------------------------------------------------------------
# gaining privileges
# --------------------------------------------------------------------------

def self_command() -> list[str]:
    """How to start ourselves: installed binary, zipapp or sources."""
    if BINARY_PATH.exists():
        return [str(BINARY_PATH)]
    argv0 = Path(sys.argv[0]).resolve()
    if argv0.is_file() and os.access(argv0, os.X_OK):
        return [str(argv0)]
    return [sys.executable, str(Path(__file__).resolve().parent)]


def is_root() -> bool:
    return os.geteuid() == 0


def run_privileged(args: list[str]) -> str:
    """Run a subcommand as root and return its stdout. Used by the GUI.

    The language is handed over explicitly: pkexec scrubs the environment,
    so the child would otherwise answer in the locale of root.
    """
    command = self_command() + ['--lang', current_language()] + list(args)
    if not is_root():
        command = ['pkexec', *command]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode == 126:
        raise PrivilegeError(translate('authorisation cancelled'), ' '.join(command))
    if result.returncode == 127:
        raise PrivilegeError(translate('could not start pkexec'), ' '.join(command))
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip() or translate(
            'exit code {code}', code=result.returncode,
        )
        raise PrivilegeError(message, ' '.join(command))
    return (result.stdout or '').strip()
