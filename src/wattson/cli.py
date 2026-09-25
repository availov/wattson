"""Command line interface. Without arguments it starts the GUI."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__, core, i18n
from .i18n import translate

PRIVILEGED_COMMANDS = {'apply', 'reset'}
LANGUAGE_OPTION = '--lang'


def _elevate(argv: list[str]) -> None:
    """Restart through pkexec, keeping the output stream.

    The language is passed on explicitly because pkexec scrubs the
    environment; an explicit ``--lang`` in ``argv`` comes later and wins.

    :param argv: arguments this run was called with.
    """
    if core.is_root():
        return
    command = ['pkexec', *core.self_command(),
               LANGUAGE_OPTION, i18n.current_language(), *argv]
    try:
        os.execvp('pkexec', command)
    except OSError as error:
        raise core.PrivilegeError(
            translate('could not start pkexec: {error}', error=error), ' '.join(command),
        ) from error


def _preselect_language(argv: list[str]) -> None:
    """Apply ``--lang`` before the parser is built, so --help follows it too.

    :param argv: arguments this run was called with.
    """
    for index, token in enumerate(argv):
        if token == LANGUAGE_OPTION and index + 1 < len(argv):
            i18n.set_language(argv[index + 1])
        elif token.startswith(LANGUAGE_OPTION + '='):
            i18n.set_language(token.split('=', 1)[1])


def _print_rows(rows: list[tuple[str, str]]) -> None:
    """Print ``label: value`` pairs with the values lined up.

    The width is computed instead of hard-coded: translated labels have
    lengths of their own.

    :param rows: pairs of label and already formatted value.
    """
    width = max((len(label) for label, _value in rows), default=0)
    for label, value in rows:
        print(f'{label + ":":<{width + 2}} {value}')


# --------------------------------------------------------------------------
# battery: shared by status and battery
# --------------------------------------------------------------------------

def _threshold_text(threshold: int | None) -> str:
    if threshold is None or threshold >= 100:
        return translate('none')
    return translate('{value} %', value=threshold)


def _battery_payload(info: core.BatteryInfo, config: core.Config) -> dict:
    return {
        'supported': True,
        'capacity': info.capacity,
        'status': info.status,
        'threshold': info.threshold,
        'voltage': info.voltage,
        'power': info.power,
        'now': info.now,
        'full': info.full,
        'full_design': info.design,
        'unit': info.unit,
        'health_pct': info.health_pct,
        'wear_pct': info.wear_pct,
        'config': {
            'battery_enabled': config.battery_enabled,
            'battery_threshold': config.battery_threshold,
        },
    }


def _battery_json() -> dict:
    info = core.read_battery()
    if info is None:
        return {'supported': False}
    return _battery_payload(info, core.load_config())


def _battery_rows() -> list[tuple[str, str]]:
    """One row for the overall status; empty when there is no battery."""
    info = core.read_battery()
    if info is None:
        return []
    capacity = '—' if info.capacity is None else translate('{value} %', value=info.capacity)
    return [(translate('Battery'), translate(
        '{capacity}, {status}, threshold {threshold}',
        capacity=capacity,
        status=info.status_label,
        threshold=_threshold_text(info.threshold),
    ))]


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    config = core.load_config()
    fans = core.read_fans()
    temp = core.read_cpu_temp()
    profile = core.platform_profile()

    kernel_points: list[tuple[int, int]] = []
    mode = core.MODE_AUTO
    try:
        kernel_points = core.read_curve(1)
        mode = core.current_mode()
    except core.HardwareError as error:
        if args.json:
            print(json.dumps(
                {'supported': False, 'error': str(error), 'battery': _battery_json()},
                ensure_ascii=False,
            ))
        else:
            print(translate('Hardware: unsupported ({error})', error=error))
            _print_rows(_battery_rows())
        return 1

    if args.json:
        payload = {
            'supported': True,
            'mode': mode,
            'profile': profile,
            'cpu_temp': temp,
            'fans': [{'label': label, 'rpm': rpm} for label, rpm in fans],
            'kernel_curve': [[temp_c, pwm] for temp_c, pwm in kernel_points],
            'battery': _battery_json(),
            'config': config.to_dict(),
            'autostart': core.autostart_enabled(),
            'autostart_installed': core.autostart_installed(),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    rows = [
        (translate('Mode'), core.mode_label(mode)),
        (translate('Profile'), profile or translate('unknown')),
        (translate('Temperature'),
         '—' if temp is None else translate('{value} °C', value=temp)),
    ]
    rows += [(core.fan_label(label), translate('{rpm} rpm', rpm=rpm)) for label, rpm in fans]
    rows += _battery_rows()

    if core.autostart_installed():
        rows.append((translate('Autostart'),
                     translate('on') if core.autostart_enabled() else translate('off')))
    else:
        rows.append((translate('Autostart'), translate('not installed (run install.sh)')))
    _print_rows(rows)

    suffix = '' if core.CONFIG_PATH.exists() else translate(' — no file, defaults in use')
    print()
    print(translate('Settings ({path}{suffix}):', path=core.CONFIG_PATH, suffix=suffix))
    _print_rows([
        ('  ' + translate('apply'),
         translate('yes') if config.enabled else translate('no')),
        ('  ' + translate('floor'),
         translate('{value} % (pwm {raw})',
                   value=config.floor_pct, raw=core.pct_to_raw(config.floor_pct))),
        ('  ' + translate('charge up to'),
         translate('{value} %', value=config.battery_threshold) if config.battery_enabled
         else translate('not limited')),
    ])

    print()
    print(translate('Curve currently in the kernel:'))
    for temp_c, pwm in kernel_points:
        print(f'  {temp_c:>3} °C   pwm {pwm:>3}   {core.raw_to_pct(pwm):>3} %')
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    config = core.load_config()

    if args.from_config:
        # the lid goes first: a failure further down must not keep a laptop
        # with its lid closed awake until the battery is flat
        if config.lid_enabled and config.lid_battery == core.LID_LOW_BATTERY:
            from . import lid

            if lid.sleep_if_low(config.lid_low_threshold) and not args.quiet:
                print(translate('the lid is closed and the charge is low — going to sleep'))

        # service mode: curve and charge threshold are restored independently,
        # a missing subsystem must not take the other one down
        restored = []
        if (config.enabled and core.supported()
                and core.apply_curve(config.floor_pct, config.points, force=args.force)):
            restored.append(translate('curve, floor {value} %', value=config.floor_pct))
        if (config.battery_enabled and core.battery_supported()
                and core.apply_battery(config.battery_threshold, force=args.force)):
            restored.append(translate('charge threshold {value} %',
                                      value=config.battery_threshold))
        if not args.quiet:
            print(translate('restored: {items}', items=', '.join(restored)) if restored
                  else translate('everything was already applied'))
        return 0

    floor = config.floor_pct if args.floor is None else args.floor
    points = core.parse_points(args.points) if args.points else config.points
    core.validate(floor, points)

    changed = core.apply_curve(floor, points, force=args.force)

    config.enabled = True
    config.floor_pct = floor
    config.points = points
    core.save_config(config)

    if not args.quiet:
        raw = core.pct_to_raw(floor)
        print(
            translate('floor {value} % (pwm {raw})', value=floor, raw=raw)
            + ('' if changed else translate(', the curve was already applied'))
        )
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    core.reset_factory()
    config = core.load_config()
    config.enabled = False
    core.save_config(config)
    if not args.quiet:
        print(translate('factory curve restored, automatic re-apply switched off'))
    return 0


def cmd_battery(args: argparse.Namespace) -> int:
    if args.action == 'show':
        info = core.read_battery()
        if info is None:
            if args.json:
                print(json.dumps({'supported': False}, ensure_ascii=False))
            else:
                print(translate(
                    'Battery: {file} not found — the charge threshold is unsupported',
                    file=core.BATTERY_THRESHOLD_FILE,
                ))
            return 1

        config = core.load_config()
        if args.json:
            print(json.dumps(_battery_payload(info, config), indent=2, ensure_ascii=False))
            return 0

        capacity = '—' if info.capacity is None else translate('{value} %',
                                                               value=info.capacity)
        rows = [
            (translate('Charge'), capacity),
            (translate('State'), info.status_label),
            (translate('Threshold'), _threshold_text(info.threshold)),
        ]
        if info.voltage is not None:
            rows.append((translate('Voltage'), translate('{value:.2f} V', value=info.voltage)))
        if info.power is not None:
            rows.append((translate('Power'), translate('{value:.1f} W', value=info.power)))
        if info.full is not None and info.design is not None:
            wear = info.wear_pct
            text = translate('{now:.1f} of {design:.1f} {unit}',
                             now=info.full, design=info.design, unit=info.unit_label)
            if wear is not None:
                text += translate('   (wear {value} %)', value=wear)
            rows.append((translate('Capacity'), text))
        rows.append((translate('In the config'),
                     translate('restore {value} %', value=config.battery_threshold)
                     if config.battery_enabled else translate('do not limit')))
        _print_rows(rows)
        return 0

    threshold = 100 if args.action == 'off' else args.value
    if threshold is None:
        raise ValueError(translate(
            'a value is required: battery set PERCENT ({minimum}–100)',
            minimum=core.MIN_BATTERY_THRESHOLD,
        ))
    core.validate_battery(threshold)

    changed = core.apply_battery(threshold)
    config = core.load_config()
    config.battery_enabled = threshold < 100
    config.battery_threshold = threshold
    core.save_config(config)

    if not args.quiet:
        if threshold >= 100:
            print(translate('limit lifted, charging up to 100 %'))
        else:
            print(translate('charge threshold {value} %', value=threshold)
                  + ('' if changed else translate(', it was already set')))
    return 0


def cmd_lid(args: argparse.Namespace) -> int:
    """Show, set or reset what closing the lid does.

    :param args: parsed ``lid`` subcommand.
    """
    from . import lid

    if args.action == 'show':
        state = lid.read_state()
        if state is None:
            if args.json:
                print(json.dumps({'supported': False}, ensure_ascii=False))
            else:
                print(translate('Lid: systemd-logind does not answer — the lid settings '
                                'are unavailable'))
            return 1

        config = core.load_config()
        if args.json:
            payload = {
                'supported': True,
                **state.to_dict(),
                'config': {
                    'lid_enabled': config.lid_enabled,
                    'lid_battery': config.lid_battery,
                    'lid_ac': config.lid_ac,
                    'lid_low_threshold': config.lid_low_threshold,
                },
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0

        _print_rows([
            (translate('Lid'), translate('closed') if state.closed else translate('open')),
            (translate('Power source'),
             translate('adapter') if state.on_external_power else translate('battery')),
            (translate('On battery'),
             lid.battery_label(state, config, core.autostart_enabled())),
            (translate('On the adapter'), lid.action_label(state.ac)),
            (translate('With an external monitor'), lid.action_label(state.docked_action)),
        ])
        if state.blocked:
            print()
            print(translate(
                'An application holds the lid switch right now (GNOME does so while an '
                'external monitor is connected), so closing the lid will not put the '
                'laptop to sleep.'
            ))
        return 0

    if args.action == 'off':
        lid.reset()
        config = core.load_config()
        config.lid_enabled = False
        core.save_config(config)
        if not args.quiet:
            print(translate('the lid is back to the system defaults'))
        return 0

    config = core.load_config()
    battery = args.battery or config.lid_battery
    ac = args.ac or config.lid_ac
    threshold = config.lid_low_threshold if args.low is None else args.low
    core.validate_lid_threshold(threshold)

    lid.apply(battery, ac)
    config.lid_enabled = True
    config.lid_battery = battery
    config.lid_ac = ac
    config.lid_low_threshold = threshold
    core.save_config(config)

    if not args.quiet:
        _print_rows([
            (translate('On battery'), lid.setting_label(battery, threshold)),
            (translate('On the adapter'), lid.action_label(ac)),
        ])
        if battery == core.LID_LOW_BATTERY and not core.autostart_enabled():
            print(translate('autostart is off, and without it the laptop never goes to '
                            'sleep at low charge: wattson autostart on'))
    return 0


def cmd_power(args: argparse.Namespace) -> int:
    from . import power

    report = power.analyze(args.seconds)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0

    print(report.summary())
    for note in report.notes:
        print(f'  · {note}')

    if not report.findings:
        print()
        print(translate('No noticeable consumers — everything is already set to save power.'))
        return 0

    source_title = translate('SOURCE')
    width = min(38, max([len(finding.source) for finding in report.findings]
                        + [len(source_title)]))
    print()
    print(f'{source_title:<{width}}  {translate("CONTRIBUTION"):<20}  {translate("STATE")}')
    print('─' * (width + 24 + 30))
    for finding in report.findings:
        print(f'{finding.source[:width]:<{width}}  {finding.contribution():<20}  {finding.state}')
        for line in _wrap(finding.advice, 92):
            print(f'    → {line}')
    return 0


def cmd_net(args: argparse.Namespace) -> int:
    from . import network

    monitor = network.Monitor()
    monitor.sample()            # baseline: rates need two readings
    time.sleep(args.seconds)
    snapshot = monitor.sample()

    if args.json:
        print(json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False))
        return 0

    rows = [(translate('All traffic'), translate(
        '↓ {down}   ↑ {up}',
        down=network.format_rate(snapshot.rx_rate),
        up=network.format_rate(snapshot.tx_rate),
    ))]
    for interface in snapshot.visible_interfaces():
        rows.append((interface.name, translate(
            '↓ {down}   ↑ {up}   ({received} / {sent} in total)',
            down=network.format_rate(interface.rx_rate),
            up=network.format_rate(interface.tx_rate),
            received=network.format_bytes(interface.rx_bytes),
            sent=network.format_bytes(interface.tx_bytes),
        )))
    _print_rows(rows)

    if not snapshot.applications:
        print()
        print(translate('ss from iproute2 is missing — traffic per application cannot '
                        'be counted.') if network.tool_missing()
              else translate('No application moved data during the measurement.'))
        return 0

    title = translate('APPLICATION')
    width = min(30, max([len(item.name) for item in snapshot.applications] + [len(title)]))
    print()
    print(f'{title:<{width}}  {translate("SPEED ↓"):>11}  {translate("SPEED ↑"):>11}'
          f'  {translate("MOVED ↓"):>11}  {translate("MOVED ↑"):>11}')
    for application in snapshot.applications[:network.MAX_APPLICATION_ROWS]:
        print(f'{application.name[:width]:<{width}}  '
              f'{network.format_rate(application.rx_rate):>11}  '
              f'{network.format_rate(application.tx_rate):>11}  '
              f'{network.format_bytes(application.rx_total):>11}  '
              f'{network.format_bytes(application.tx_total):>11}')

    if not core.is_root():
        print()
        print(translate('Only your own applications are listed — run with sudo for all.'))
    return 0


def _wrap(text: str, width: int) -> list[str]:
    lines, current = [], ''
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f'{current} {word}'.strip()
    if current:
        lines.append(current)
    return lines


def cmd_autostart(args: argparse.Namespace) -> int:
    if args.action == 'status':
        if not core.autostart_installed():
            print(translate('not installed'))
            return 1
        print(translate('on') if core.autostart_enabled() else translate('off'))
        return 0

    core.set_autostart(args.action == 'on')
    if not args.quiet:
        print(translate('autostart is on') if args.action == 'on'
              else translate('autostart is off'))
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    if args.action == 'init':
        if core.CONFIG_PATH.exists():
            print(translate('{path} already exists', path=core.CONFIG_PATH))
            return 0
        core.save_config(core.Config())
        print(translate('{path} created', path=core.CONFIG_PATH))
        return 0

    print(json.dumps(core.load_config().to_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    from . import gui

    return gui.run()


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='wattson',
        description=translate(
            'Laptop power and cooling: fan curve (ASUS only), battery charge stop '
            'threshold and an audit of where the watts go.'
        ),
    )
    parser.add_argument('--version', action='version', version=f'wattson {__version__}')
    parser.add_argument(LANGUAGE_OPTION, metavar='CODE',
                        help=translate(
                            'interface language ({languages}); WATTSON_LANG and the '
                            'locale of the session are used when omitted',
                            languages=', '.join(i18n.available_languages()),
                        ))

    # shared flag: -q is accepted after the name of a subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('-q', '--quiet', action='store_true',
                        help=translate('do not print the result'))

    sub = parser.add_subparsers(dest='command')

    sub.add_parser('gui', help=translate('graphical interface (the default)'))

    status = sub.add_parser('status', help=translate(
        'current state: speed, mode, settings'))
    status.add_argument('--json', action='store_true',
                        help=translate('machine readable output'))

    apply_cmd = sub.add_parser('apply', parents=[common],
                               help=translate('apply the curve (needs root)'))
    apply_cmd.add_argument('--floor', type=int, metavar='PERCENT',
                           help=translate('minimum fan speed, 0–100'))
    apply_cmd.add_argument('--points', metavar='T:P,...',
                           help=translate(
                               '8 curve points, for instance '
                               '30:0,50:0,60:2,67:38,73:47,80:61,88:80,95:100'))
    apply_cmd.add_argument('--from-config', action='store_true',
                           help=translate('take everything from {path} (used by the service)',
                                          path=core.CONFIG_PATH))
    apply_cmd.add_argument('--force', action='store_true',
                           help=translate('write even when the curve already matches'))

    sub.add_parser('reset', parents=[common],
                   help=translate('restore the factory curve (needs root)'))

    battery = sub.add_parser('battery', parents=[common],
                             help=translate('battery charge stop threshold'))
    battery.add_argument('action', nargs='?', choices=['show', 'set', 'off'],
                         default='show',
                         help=translate(
                             'show — state, set — set the threshold, '
                             'off — lift the limit (set and off need root)'))
    battery.add_argument('value', nargs='?', type=int, metavar='PERCENT',
                         help=translate('threshold for set, {minimum}–100',
                                        minimum=core.MIN_BATTERY_THRESHOLD))
    battery.add_argument('--json', action='store_true',
                         help=translate('machine readable output for show'))

    lid_cmd = sub.add_parser('lid', parents=[common],
                             help=translate('what closing the lid does'))
    lid_cmd.add_argument('action', nargs='?', choices=['show', 'set', 'off'],
                         default='show',
                         help=translate(
                             'show — state, set — change it, off — back to the system '
                             'defaults (set and off need root)'))
    lid_cmd.add_argument('--battery', choices=core.LID_BATTERY_ACTIONS,
                         help=translate(
                             'on battery; {mode} sleeps only once the charge is down '
                             'to --low', mode=core.LID_LOW_BATTERY))
    lid_cmd.add_argument('--ac', choices=core.LID_AC_ACTIONS,
                         help=translate('on the adapter'))
    lid_cmd.add_argument('--low', type=int, metavar='PERCENT',
                         help=translate('charge to sleep at, {minimum}–{maximum}',
                                        minimum=core.MIN_LID_LOW_THRESHOLD,
                                        maximum=core.MAX_LID_LOW_THRESHOLD))
    lid_cmd.add_argument('--json', action='store_true',
                         help=translate('machine readable output for show'))

    power_cmd = sub.add_parser('power', parents=[common],
                               help=translate('audit of the power consumers'))
    power_cmd.add_argument('--seconds', type=float, default=4.0, metavar='SECONDS',
                           help=translate('length of the measurement, 4 by default'))
    power_cmd.add_argument('--json', action='store_true',
                           help=translate('machine readable output'))

    net = sub.add_parser('net', parents=[common],
                         help=translate('network speed and traffic per application'))
    net.add_argument('--seconds', type=float, default=2.0, metavar='SECONDS',
                     help=translate('length of the measurement, 2 by default'))
    net.add_argument('--json', action='store_true',
                     help=translate('machine readable output'))

    autostart = sub.add_parser('autostart', parents=[common],
                               help=translate('control the autostart'))
    autostart.add_argument('action', choices=['on', 'off', 'status'])

    config = sub.add_parser('config', parents=[common],
                            help=translate('show or create the configuration'))
    config.add_argument('action', nargs='?', choices=['show', 'init'], default='show')

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _preselect_language(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    # the pre-scan above only exists so that --help is already translated;
    # the language picked in the GUI counts as the preference of this user
    i18n.set_language(args.lang or core.load_language()
                      or i18n.language_from_environment())

    command = args.command or 'gui'

    needs_root = command in PRIVILEGED_COMMANDS or (
        command == 'autostart' and args.action in ('on', 'off')
    ) or (command == 'config' and args.action == 'init') or (
        command in ('battery', 'lid') and args.action in ('set', 'off')
    )

    if needs_root and not core.is_root():
        _elevate(argv)  # does not return

    handlers = {
        'gui': cmd_gui,
        'status': cmd_status,
        'apply': cmd_apply,
        'reset': cmd_reset,
        'battery': cmd_battery,
        'lid': cmd_lid,
        'power': cmd_power,
        'net': cmd_net,
        'autostart': cmd_autostart,
        'config': cmd_config,
    }

    try:
        return handlers[command](args)
    except (core.HardwareError, core.PrivilegeError, ValueError) as error:
        print(translate('error: {error}', error=error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
