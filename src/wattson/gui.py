"""Graphical interface built on GTK4.

Three tabs: the fan curve, the battery charge stop threshold and an audit
of the power consumers.

The window runs as an ordinary user: every sysfs write goes to a separate
process through pkexec, which is what lets the GUI live under Wayland.
"""

from __future__ import annotations

import json
import threading
from typing import Callable

import gi

gi.require_version('Gtk', '4.0')

from gi.repository import GLib, Gtk  # noqa: E402

from . import __version__, core, i18n, network, power  # noqa: E402
from .i18n import translate  # noqa: E402

REFRESH_SECONDS = 2
AUTOSTART_EVERY = 5  # refresh the state of the service once every N ticks
ERROR_COLOR = '#c01c28'
HINT_COLOR = '#77767b'
WARN_WEAR_PCT = 15  # from which wear on it is painted red
FIRST_ANALYSIS_DELAY_MS = 700  # first measurement shortly after the window opens


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application,
                         title=translate('Wattson — fans, battery, power draw'))
        self.set_default_size(600, 820)

        self._suppress = False          # keeps programmatic updates out of the handlers
        self._tick = 0
        self._temp_spins: list[Gtk.SpinButton] = []
        self._pwm_spins: list[Gtk.SpinButton] = []
        self._effective_labels: list[Gtk.Label] = []
        self._fan_labels: dict[str, Gtk.Label] = {}
        self._applied_threshold: int | None = None
        self._analysis_running = False
        self._network_running = False
        self._closing = False           # set while the window is being replaced
        self._supported = core.supported()
        self._battery_supported = core.battery_supported()
        # the monitor belongs to the application, so its totals survive a
        # language switch, which builds the window anew
        self._network = application.network

        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)
        notebook.append_page(self._build_fan_tab(), Gtk.Label(label=translate('Fans')))
        notebook.append_page(self._build_battery_tab(), Gtk.Label(label=translate('Battery')))
        notebook.append_page(self._build_power_tab(), Gtk.Label(label=translate('Power draw')))
        notebook.append_page(self._build_network_tab(), Gtk.Label(label=translate('Network')))
        notebook.set_action_widget(self._build_language(), Gtk.PackType.END)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.append(notebook)
        root.append(self._build_footer())
        self.set_child(root)

        self._load_initial()
        self._refresh_live()
        self._refresh_battery()
        self._refresh_network()
        self._refresh_autostart()
        self._set_sensitive(True)

        problems = []
        if not self._supported:
            problems.append(translate('the asus_custom_fan_curve driver is missing, '
                                      'fan control is unavailable'))
        if not self._battery_supported:
            problems.append(translate('{file} is missing, the charge threshold is '
                                      'unavailable', file=core.BATTERY_THRESHOLD_FILE))
        if problems:
            self._set_status(
                translate('Unavailable: {problems}.', problems='; '.join(problems)),
                error=True,
            )

        self._timer_id = GLib.timeout_add_seconds(REFRESH_SECONDS, self._on_timer)
        self._first_analysis_id = GLib.timeout_add(FIRST_ANALYSIS_DELAY_MS,
                                                   self._on_first_analysis)

    # ------------------------------------------------------------------
    # tabs
    # ------------------------------------------------------------------

    def _build_fan_tab(self) -> Gtk.Widget:
        scroller = self._scroller()
        content = self._page()
        content.append(self._build_fan_live())
        content.append(self._build_floor(scroller))
        content.append(self._build_curve(scroller))
        content.append(self._build_fan_actions())
        scroller.set_child(content)
        return scroller

    def _build_battery_tab(self) -> Gtk.Widget:
        scroller = self._scroller()
        content = self._page()
        content.append(self._build_battery_live())
        content.append(self._build_threshold(scroller))
        content.append(self._build_battery_actions())
        scroller.set_child(content)
        return scroller

    # ------------------------------------------------------------------
    # layout: fans
    # ------------------------------------------------------------------

    def _build_fan_live(self) -> Gtk.Widget:
        grid = self._grid()

        self._temp_value = self._value_label()
        self._profile_value = self._value_label()
        self._mode_value = self._value_label()

        row = 0
        grid.attach(self._key_label(translate('CPU temperature')), 0, row, 1, 1)
        grid.attach(self._temp_value, 1, row, 1, 1)

        for label, _rpm in core.read_fans():
            row += 1
            value = self._value_label()
            self._fan_labels[label] = value
            grid.attach(self._key_label(core.fan_label(label)), 0, row, 1, 1)
            grid.attach(value, 1, row, 1, 1)

        row += 1
        grid.attach(self._key_label(translate('Power profile')), 0, row, 1, 1)
        grid.attach(self._profile_value, 1, row, 1, 1)

        row += 1
        grid.attach(self._key_label(translate('Mode')), 0, row, 1, 1)
        grid.attach(self._mode_value, 1, row, 1, 1)

        return self._frame(translate('Right now'), grid)

    def _build_floor(self, scroller: Gtk.ScrolledWindow) -> Gtk.Widget:
        box = self._section()

        self._floor_value = Gtk.Label(xalign=0)

        self._floor_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self._floor_scale.set_draw_value(False)
        self._floor_scale.set_hexpand(True)
        for mark in (0, 10, 20, 30, 50, 75, 100):
            self._floor_scale.add_mark(mark, Gtk.PositionType.BOTTOM, str(mark))
        self._floor_scale.connect('value-changed', self._on_floor_changed)
        self._disable_wheel(self._floor_scale, scroller)

        box.append(self._floor_value)
        box.append(self._floor_scale)
        box.append(self._hint(translate(
            'The fan never goes below this value and can no longer stop completely — '
            'it is the stopping that produces the pulsing.'
        )))
        return self._frame(translate('Minimum fan speed'), box)

    def _build_curve(self, scroller: Gtk.ScrolledWindow) -> Gtk.Widget:
        grid = self._grid()

        titles = (translate('Temperature'), translate('Speed'),
                  translate('With the floor'))
        for column, title in enumerate(titles):
            header = Gtk.Label(xalign=0)
            header.set_markup(f"<span foreground='{HINT_COLOR}'>{title}</span>")
            grid.attach(header, column, 0, 1, 1)

        for index in range(core.POINT_COUNT):
            temp_spin = Gtk.SpinButton.new_with_range(0, 110, 1)
            temp_spin.set_numeric(True)
            temp_spin.connect('value-changed', self._on_point_changed)
            self._disable_wheel(temp_spin, scroller)

            pwm_spin = Gtk.SpinButton.new_with_range(0, 100, 1)
            pwm_spin.set_numeric(True)
            pwm_spin.connect('value-changed', self._on_point_changed)
            self._disable_wheel(pwm_spin, scroller)

            effective = self._value_label()

            grid.attach(temp_spin, 0, index + 1, 1, 1)
            grid.attach(pwm_spin, 1, index + 1, 1, 1)
            grid.attach(effective, 2, index + 1, 1, 1)

            self._temp_spins.append(temp_spin)
            self._pwm_spins.append(pwm_spin)
            self._effective_labels.append(effective)

        return self._frame(translate('Curve: °C → % of speed'), grid)

    def _build_fan_actions(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        self._apply_button = Gtk.Button(label=translate('Apply'))
        self._apply_button.add_css_class('suggested-action')
        self._apply_button.connect('clicked', self._on_apply)

        self._reset_button = Gtk.Button(label=translate('Factory'))
        self._reset_button.connect('clicked', self._on_reset)

        box.append(self._apply_button)
        box.append(self._reset_button)
        return box

    # ------------------------------------------------------------------
    # layout: battery
    # ------------------------------------------------------------------

    def _build_battery_live(self) -> Gtk.Widget:
        grid = self._grid()

        self._charge_value = self._value_label()
        self._charge_status_value = self._value_label()
        self._threshold_value = self._value_label()
        self._power_value = self._value_label()
        self._voltage_value = self._value_label()
        self._health_value = self._value_label()

        rows = (
            (translate('Charge'), self._charge_value),
            (translate('State'), self._charge_status_value),
            (translate('Threshold now'), self._threshold_value),
            (translate('Power'), self._power_value),
            (translate('Voltage'), self._voltage_value),
            (translate('Capacity'), self._health_value),
        )
        for row, (title, widget) in enumerate(rows):
            grid.attach(self._key_label(title), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)

        return self._frame(translate('Right now'), grid)

    def _build_threshold(self, scroller: Gtk.ScrolledWindow) -> Gtk.Widget:
        box = self._section()

        self._threshold_preview = Gtk.Label(xalign=0)

        self._battery_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, core.MIN_BATTERY_THRESHOLD, 100, 1
        )
        self._battery_scale.set_draw_value(False)
        self._battery_scale.set_hexpand(True)
        for mark in (20, 60, 80, 90, 100):
            if mark >= core.MIN_BATTERY_THRESHOLD:
                self._battery_scale.add_mark(mark, Gtk.PositionType.BOTTOM, str(mark))
        self._battery_scale.connect('value-changed', self._on_threshold_changed)
        self._disable_wheel(self._battery_scale, scroller)

        box.append(self._threshold_preview)
        box.append(self._battery_scale)
        box.append(self._hint(translate(
            'A lithium-ion battery ages faster the higher the voltage on the cell is, '
            'and sitting at 100 % it loses capacity even without charge cycles. '
            '80 % is a sensible compromise, 60 % suits a machine that hardly ever '
            'leaves the adapter. The difference between 100 and 95 % is next to nothing.'
        )))
        box.append(self._hint(translate(
            'Once the threshold is reached charging stops and the state becomes '
            '"charging stopped" — that is normal behaviour, not a fault.'
        )))
        return self._frame(translate('Charge stop threshold'), box)

    def _build_battery_actions(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        self._battery_apply = Gtk.Button(label=translate('Apply'))
        self._battery_apply.add_css_class('suggested-action')
        self._battery_apply.connect('clicked', self._on_battery_apply)

        self._battery_off = Gtk.Button(label=translate('Lift the limit'))
        self._battery_off.connect('clicked', self._on_battery_off)

        box.append(self._battery_apply)
        box.append(self._battery_off)
        return box

    # ------------------------------------------------------------------
    # layout: power draw
    # ------------------------------------------------------------------

    def _build_power_summary(self) -> Gtk.Widget:
        box = self._section()

        self._power_summary = Gtk.Label(xalign=0, wrap=True)
        self._power_summary.set_markup(
            f'<b>{GLib.markup_escape_text(translate("No measurement taken yet"))}</b>'
        )

        self._power_notes = Gtk.Label(xalign=0, wrap=True)
        self._power_notes.set_visible(False)

        box.append(self._power_summary)
        box.append(self._power_notes)
        return self._frame(translate('Summary'), box)

    def _build_power_actions(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        self._power_button = Gtk.Button(label=translate('Check'))
        self._power_button.add_css_class('suggested-action')
        self._power_button.connect('clicked', self._on_analyze)
        self._power_button.set_tooltip_text(translate(
            'The measurement takes {seconds} seconds', seconds=power.SAMPLE_SECONDS,
        ))

        self._power_root_button = Gtk.Button(label=translate('Exact measurement (root)'))
        self._power_root_button.connect('clicked', self._on_analyze_root)
        self._power_root_button.set_tooltip_text(translate(
            'The same thing, but reading RAPL — it adds the exact CPU power and the '
            'split of watts across processes'
        ))

        box.append(self._power_button)
        box.append(self._power_root_button)
        return box

    def _build_power_table(self) -> Gtk.Widget:
        self._power_table = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._power_table.set_margin_top(10)
        self._power_table.set_margin_bottom(10)
        self._power_table.set_margin_start(12)
        self._power_table.set_margin_end(12)
        self._power_table.append(self._hint(translate(
            'Press "Check". Only what really draws power or is configured away from '
            'saving lands in the table — healthy things are not shown.'
        )))
        return self._frame(translate('Consumers'), self._power_table)

    def _build_power_tab(self) -> Gtk.Widget:
        scroller = self._scroller()
        content = self._page()
        content.append(self._build_power_summary())
        content.append(self._build_power_actions())
        content.append(self._build_power_table())
        scroller.set_child(content)
        return scroller

    def _render_power(self, report: power.Report) -> None:
        self._power_summary.set_markup(
            f'<b>{GLib.markup_escape_text(report.summary())}</b>'
        )
        if report.notes:
            self._power_notes.set_markup(
                f"<span foreground='{HINT_COLOR}'>"
                + GLib.markup_escape_text('\n'.join(report.notes)) + '</span>'
            )
            self._power_notes.set_visible(True)
        else:
            self._power_notes.set_visible(False)

        self._clear(self._power_table)

        if not report.findings:
            self._power_table.append(self._hint(translate(
                'No noticeable consumers — everything is already set to save power.'
            )))
            return

        grid = self._table_grid((translate('Source'), translate('Contribution'),
                                 translate('State')))
        row = 1
        for finding in report.findings:
            name = Gtk.Label(xalign=0, wrap=True, max_width_chars=30)
            escaped = GLib.markup_escape_text(finding.source)
            if finding.severity >= power.SEVERITY_HIGH:
                name.set_markup(f'<b>{escaped}</b>')
            else:
                name.set_markup(escaped)

            value = Gtk.Label(xalign=0)
            value.set_markup(f'<tt>{GLib.markup_escape_text(finding.contribution())}</tt>')

            state = Gtk.Label(xalign=0, wrap=True, max_width_chars=34)
            state.set_markup(f'<tt>{GLib.markup_escape_text(finding.state)}</tt>')

            grid.attach(name, 0, row, 1, 1)
            grid.attach(value, 1, row, 1, 1)
            grid.attach(state, 2, row, 1, 1)

            advice = Gtk.Label(xalign=0, wrap=True, max_width_chars=88)
            advice.set_margin_start(12)
            advice.set_margin_bottom(8)
            advice.set_markup(
                f"<span foreground='{HINT_COLOR}'>→ "
                + GLib.markup_escape_text(finding.advice) + '</span>'
            )
            grid.attach(advice, 0, row + 1, 3, 1)
            row += 2

        self._power_table.append(grid)

    # ------------------------------------------------------------------
    # layout: network
    # ------------------------------------------------------------------

    def _build_network_tab(self) -> Gtk.Widget:
        scroller = self._scroller()
        content = self._page()

        self._network_live = self._section()
        content.append(self._frame(translate('Right now'), self._network_live))

        self._network_apps = self._section()
        content.append(self._frame(translate('Applications'), self._network_apps))

        content.append(self._hint(translate(
            'Per-application traffic is counted from TCP sockets: QUIC (HTTP/3) has '
            'no counters in the kernel and stays invisible, and programs of other '
            'users need root — "sudo wattson net" shows them all.'
        )))
        scroller.set_child(content)
        return scroller

    def _render_network(self, snapshot: network.Snapshot) -> None:
        self._clear(self._network_live)
        grid = self._table_grid((translate('Interface'), translate('Speed ↓'),
                                 translate('Speed ↑'), translate('Total ↓'),
                                 translate('Total ↑')))
        self._table_row(grid, 1, (
            translate('All traffic'),
            network.format_rate(snapshot.rx_rate),
            network.format_rate(snapshot.tx_rate),
            network.format_bytes(snapshot.rx_bytes),
            network.format_bytes(snapshot.tx_bytes),
        ), bold=True)
        for row, interface in enumerate(snapshot.visible_interfaces(), start=2):
            self._table_row(grid, row, (
                interface.name,
                network.format_rate(interface.rx_rate),
                network.format_rate(interface.tx_rate),
                network.format_bytes(interface.rx_bytes),
                network.format_bytes(interface.tx_bytes),
            ))
        self._network_live.append(grid)

        self._clear(self._network_apps)
        if not snapshot.applications:
            self._network_apps.append(self._hint(
                translate('ss from iproute2 is missing — traffic per application '
                          'cannot be counted') if network.tool_missing()
                else translate('Nothing from your applications yet')))
            return
        grid = self._table_grid((translate('Application'), translate('Speed ↓'),
                                 translate('Speed ↑'), translate('Since start ↓'),
                                 translate('Since start ↑')))
        for row, application in enumerate(
                snapshot.applications[:network.MAX_APPLICATION_ROWS], start=1):
            self._table_row(grid, row, (
                application.name,
                network.format_rate(application.rx_rate),
                network.format_rate(application.tx_rate),
                network.format_bytes(application.rx_total),
                network.format_bytes(application.tx_total),
            ))
        self._network_apps.append(grid)

    # ------------------------------------------------------------------
    # layout: the shared bottom of the window
    # ------------------------------------------------------------------

    def _build_footer(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_bottom(14)
        box.set_margin_start(16)
        box.set_margin_end(16)

        autostart_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        autostart_box.set_halign(Gtk.Align.END)
        self._autostart_switch = Gtk.Switch()
        self._autostart_switch.set_valign(Gtk.Align.CENTER)
        self._autostart_switch.connect('notify::active', self._on_autostart_toggled)
        autostart_box.append(Gtk.Label(label=translate('Autostart')))
        autostart_box.append(self._autostart_switch)

        self._status_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self._status_label.set_text('')

        box.append(autostart_box)
        box.append(self._status_label)
        return box

    def _build_language(self) -> Gtk.Widget:
        """Language switcher: English plus every catalog shipped with the program.

        It lives in the corner of the tab row and shows two-letter codes to
        take as little room as possible; the tooltip spells the language out.
        """
        self._languages = i18n.available_languages()
        model = Gtk.StringList()
        for code in self._languages:
            model.append(code.split('_')[0][:2].upper())

        current = i18n.current_language()
        self._language_drop = Gtk.DropDown(model=model)
        self._language_drop.set_valign(Gtk.Align.CENTER)
        self._language_drop.set_margin_end(6)
        self._language_drop.add_css_class('flat')
        self._language_drop.set_selected(
            self._languages.index(current) if current in self._languages else 0
        )
        # connected after the initial value, so setting it changes nothing
        self._language_drop.connect('notify::selected', self._on_language_changed)
        if len(self._languages) < 2:
            self._language_drop.set_sensitive(False)
            self._language_drop.set_tooltip_text(translate('Only one language is installed'))
        else:
            self._language_drop.set_tooltip_text(i18n.language_name(current))
        return self._language_drop

    # ------------------------------------------------------------------
    # small layout helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _scroller() -> Gtk.ScrolledWindow:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        return scroller

    @staticmethod
    def _page() -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        return box

    @staticmethod
    def _section() -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        return box

    @staticmethod
    def _grid() -> Gtk.Grid:
        grid = Gtk.Grid()
        grid.set_row_spacing(6)
        grid.set_column_spacing(14)
        grid.set_margin_top(10)
        grid.set_margin_bottom(10)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        return grid

    @staticmethod
    def _frame(title: str, child: Gtk.Widget) -> Gtk.Frame:
        frame = Gtk.Frame(label=f' {title} ')
        frame.set_child(child)
        return frame

    @staticmethod
    def _key_label(text: str) -> Gtk.Label:
        label = Gtk.Label(xalign=0, label=text)
        return label

    @staticmethod
    def _value_label() -> Gtk.Label:
        label = Gtk.Label(xalign=0)
        label.set_use_markup(True)
        label.set_markup('<tt>—</tt>')
        return label

    @staticmethod
    def _clear(box: Gtk.Box) -> None:
        """Throw away the contents of a container that is redrawn on every tick."""
        child = box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            box.remove(child)
            child = following

    @staticmethod
    def _table_grid(titles: tuple[str, ...]) -> Gtk.Grid:
        """Grid with a row of dimmed column titles.

        :param titles: column titles, left to right.
        """
        grid = Gtk.Grid()
        grid.set_row_spacing(4)
        grid.set_column_spacing(16)
        for column, title in enumerate(titles):
            header = Gtk.Label(xalign=0)
            header.set_markup(f"<span foreground='{HINT_COLOR}'>"
                              + GLib.markup_escape_text(title) + '</span>')
            grid.attach(header, column, 0, 1, 1)
        return grid

    @staticmethod
    def _table_row(grid: Gtk.Grid, row: int, values: tuple[str, ...],
                   bold: bool = False) -> None:
        """One row of a table: a name and monospaced values after it.

        :param grid: grid built by :meth:`_table_grid`.
        :param row: row number, 1 is the first one under the titles.
        :param values: the name followed by the values of the row.
        :param bold: highlight the name, used for a summary row.
        """
        for column, value in enumerate(values):
            label = Gtk.Label(xalign=0)
            escaped = GLib.markup_escape_text(value)
            if column:
                label.set_markup(f'<tt>{escaped}</tt>')
            else:
                label.set_markup(f'<b>{escaped}</b>' if bold else escaped)
            grid.attach(label, column, row, 1, 1)

    @staticmethod
    def _hint(text: str) -> Gtk.Label:
        label = Gtk.Label(xalign=0, wrap=True)
        label.set_markup(
            f"<span foreground='{HINT_COLOR}'>{GLib.markup_escape_text(text)}</span>"
        )
        return label

    def _disable_wheel(self, widget: Gtk.Widget, scroller: Gtk.ScrolledWindow) -> None:
        """The mouse wheel over the widget must not change its value.

        The controller is attached in the CAPTURE phase: it runs before the
        own handlers of GtkRange and GtkSpinButton, which sit in the BUBBLE
        phase, so the event never reaches the control. Instead the tab is
        scrolled, otherwise the wheel over a control would look stuck.

        :param widget: control that should ignore the wheel.
        :param scroller: tab to scroll instead.
        """
        controller = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect('scroll', self._on_wheel_over_control, scroller)
        widget.add_controller(controller)

    def _on_wheel_over_control(self, _controller: Gtk.EventControllerScroll,
                               _dx: float, dy: float,
                               scroller: Gtk.ScrolledWindow) -> bool:
        if dy:
            adjustment = scroller.get_vadjustment()
            if adjustment is not None:
                step = adjustment.get_step_increment() or 50
                lowest = adjustment.get_lower()
                highest = max(lowest, adjustment.get_upper() - adjustment.get_page_size())
                target = adjustment.get_value() + dy * step
                adjustment.set_value(min(max(target, lowest), highest))
        return True  # the event goes no further

    def _set_sensitive(self, sensitive: bool) -> None:
        """Each tab is enabled only when its hardware is there."""
        fan_ready = sensitive and self._supported
        for widget in (self._floor_scale, self._apply_button, self._reset_button):
            widget.set_sensitive(fan_ready)
        for spin in self._temp_spins + self._pwm_spins:
            spin.set_sensitive(fan_ready)

        battery_ready = sensitive and self._battery_supported
        for widget in (self._battery_scale, self._battery_apply, self._battery_off):
            widget.set_sensitive(battery_ready)

        # the audit does not depend on ASUS hardware, but is locked while measuring
        self._set_power_busy(self._analysis_running or not sensitive)

        self._autostart_switch.set_sensitive(
            sensitive and core.autostart_installed()
            and (self._supported or self._battery_supported)
        )

    def _set_power_busy(self, busy: bool) -> None:
        self._power_button.set_sensitive(not busy)
        self._power_root_button.set_sensitive(not busy)

    def _set_status(self, text: str, error: bool = False) -> None:
        if error:
            escaped = GLib.markup_escape_text(text)
            self._status_label.set_markup(f"<span foreground='{ERROR_COLOR}'>{escaped}</span>")
        else:
            self._status_label.set_text(text)

    # ------------------------------------------------------------------
    # data
    # ------------------------------------------------------------------

    def _load_initial(self) -> None:
        """Values for the editors: from the config, or from the kernel."""
        config = core.load_config()
        if not core.CONFIG_PATH.exists() and self._supported:
            try:
                kernel = core.read_curve(1)
            except core.HardwareError:
                kernel = []
            if kernel:
                config.points = [(temp, core.raw_to_pct(pwm)) for temp, pwm in kernel]
                config.floor_pct = min(core.raw_to_pct(pwm) for _temp, pwm in kernel)

        self._suppress = True
        self._floor_scale.set_value(config.floor_pct)
        for spin_temp, spin_pwm, (temp, pwm) in zip(self._temp_spins, self._pwm_spins,
                                                    config.points):
            spin_temp.set_value(temp)
            spin_pwm.set_value(pwm)
        self._battery_scale.set_value(self._initial_threshold(config))
        self._suppress = False

        self._update_effective()
        self._update_threshold_preview()

    @staticmethod
    def _initial_threshold(config: core.Config) -> int:
        """What the slider shows: saved, active, or the 80 % default.

        :param config: configuration just loaded from disk.
        """
        if config.battery_enabled:
            return config.battery_threshold
        current = core.read_battery_threshold()
        if current is not None and current < 100:
            return current
        return core.DEFAULT_BATTERY_THRESHOLD

    def _collect(self) -> tuple[int, list[tuple[int, int]]]:
        floor = int(self._floor_scale.get_value())
        points = [
            (temp.get_value_as_int(), pwm.get_value_as_int())
            for temp, pwm in zip(self._temp_spins, self._pwm_spins)
        ]
        return floor, points

    def _update_effective(self) -> None:
        floor, points = self._collect()
        raw_floor = core.pct_to_raw(floor)
        self._floor_value.set_markup(
            f'<b>{floor} %</b>  <tt>'
            + GLib.markup_escape_text(
                translate('(pwm {raw} of {maximum})', raw=raw_floor, maximum=core.PWM_MAX))
            + '</tt>'
        )
        for label, (_temp, pwm) in zip(self._effective_labels, points):
            raw = max(raw_floor, core.pct_to_raw(pwm))
            marker = '  ← ' + translate('floor') if raw > core.pct_to_raw(pwm) else ''
            label.set_markup(f'<tt>{core.raw_to_pct(raw):>3} %  (pwm {raw:>3})</tt>{marker}')

    def _update_threshold_preview(self) -> None:
        wanted = int(self._battery_scale.get_value())
        if wanted >= 100:
            text = '<b>100 %</b>  ' + GLib.markup_escape_text(translate('no limit'))
        else:
            text = (f'<b>{wanted} %</b>  ' + GLib.markup_escape_text(
                translate('charging stops at this level')))
        if self._applied_threshold is not None and self._applied_threshold != wanted:
            text += (f"  <span foreground='{HINT_COLOR}'>"
                     + GLib.markup_escape_text(translate(
                         '(the system currently holds {value} %, not applied)',
                         value=self._applied_threshold))
                     + '</span>')
        self._threshold_preview.set_markup(text)

    def _set_threshold_value(self, value: int) -> None:
        self._suppress = True
        self._battery_scale.set_value(value)
        self._suppress = False
        self._update_threshold_preview()

    def _refresh_live(self) -> None:
        temp = core.read_cpu_temp()
        self._temp_value.set_markup(f'<tt>{temp if temp is not None else "—"} °C</tt>')

        for label, rpm in core.read_fans():
            widget = self._fan_labels.get(label)
            if widget is not None:
                widget.set_markup(f'<tt>{rpm} rpm</tt>')

        profile = core.platform_profile()
        self._profile_value.set_markup(f'<tt>{profile or "—"}</tt>')

        if self._supported:
            self._mode_value.set_markup(f'<tt>{core.mode_label(core.current_mode())}</tt>')
        else:
            self._mode_value.set_markup(f'<tt>{translate("driver not found")}</tt>')

    def _refresh_battery(self) -> None:
        info = core.read_battery()
        if info is None:
            self._charge_status_value.set_markup(
                f'<tt>{translate("no battery found")}</tt>')
            return

        self._applied_threshold = info.threshold

        capacity = '—' if info.capacity is None else f'{info.capacity} %'
        self._charge_value.set_markup(f'<tt>{capacity}</tt>')
        self._charge_status_value.set_markup(f'<tt>{info.status_label}</tt>')

        if info.threshold is None:
            # the battery is readable, but the kernel offers no control over it
            self._threshold_value.set_markup(
                f'<tt>{translate("control unsupported")}</tt>')
        elif info.threshold >= 100:
            self._threshold_value.set_markup(
                f'<tt>{translate("none, charging up to 100 %")}</tt>')
        else:
            self._threshold_value.set_markup(f'<tt>{info.threshold} %</tt>')

        power_text = '—' if info.power is None else translate('{value:.1f} W',
                                                              value=info.power)
        self._power_value.set_markup(f'<tt>{power_text}</tt>')

        voltage = '—' if info.voltage is None else translate('{value:.2f} V',
                                                             value=info.voltage)
        self._voltage_value.set_markup(f'<tt>{voltage}</tt>')

        if info.full is None or info.design is None:
            self._health_value.set_markup('<tt>—</tt>')
        else:
            wear = info.wear_pct
            text = '<tt>' + GLib.markup_escape_text(translate(
                '{now:.1f} of {design:.1f} {unit}',
                now=info.full, design=info.design, unit=info.unit_label)) + '</tt>'
            if wear is not None:
                color = ERROR_COLOR if wear >= WARN_WEAR_PCT else HINT_COLOR
                text += (f"  <span foreground='{color}'>"
                         + GLib.markup_escape_text(translate('wear {value} %', value=wear))
                         + '</span>')
            self._health_value.set_markup(text)

        self._update_threshold_preview()

    def _refresh_autostart(self) -> None:
        installed = core.autostart_installed()
        self._suppress = True
        self._autostart_switch.set_active(installed and core.autostart_enabled())
        self._suppress = False
        self._autostart_switch.set_sensitive(
            installed and (self._supported or self._battery_supported)
        )
        if not installed:
            self._autostart_switch.set_tooltip_text(translate(
                'The service is not installed — run install.sh from the project directory'
            ))
        else:
            self._autostart_switch.set_tooltip_text(translate(
                'Restores the curve and the charge threshold after a reboot, after '
                'resume and after a power profile switch'
            ))

    def _on_timer(self) -> bool:
        self._tick += 1
        self._refresh_live()
        self._refresh_battery()
        self._refresh_network()
        if self._tick % AUTOSTART_EVERY == 0:
            self._refresh_autostart()
        return True

    def _refresh_network(self) -> None:
        """Sample in a thread: ``ss`` takes tens of milliseconds, too much here.

        Sampling goes on while other tabs are open, otherwise the totals
        would have holes in them.
        """
        if self._network_running:
            return
        self._network_running = True

        def worker() -> None:
            try:
                snapshot = self._network.sample()
            except Exception:  # noqa: BLE001 — one bad reading must not stop the timer
                snapshot = None
            GLib.idle_add(self._network_done, snapshot)

        threading.Thread(target=worker, daemon=True).start()

    def _network_done(self, snapshot: network.Snapshot | None) -> bool:
        self._network_running = False
        if snapshot is not None and not self._closing:
            self._render_network(snapshot)
        return False

    # ------------------------------------------------------------------
    # handlers
    # ------------------------------------------------------------------

    def _on_floor_changed(self, _scale: Gtk.Scale) -> None:
        if self._suppress:
            return
        self._update_effective()

    def _on_point_changed(self, _spin: Gtk.SpinButton) -> None:
        if self._suppress:
            return
        self._update_effective()

    def _on_threshold_changed(self, _scale: Gtk.Scale) -> None:
        if self._suppress:
            return
        self._update_threshold_preview()

    def _on_apply(self, _button: Gtk.Button) -> None:
        floor, points = self._collect()
        try:
            core.validate(floor, points)
        except ValueError as error:
            self._set_status(translate('Not applied: {error}', error=error), error=True)
            return
        self._run_privileged(
            ['apply', '--floor', str(floor), '--points', core.format_points(points),
             '--force', '-q'],
            translate('Applied: floor {value} %, the curve is written to the kernel and '
                      'saved in {path}', value=floor, path=core.CONFIG_PATH),
        )

    def _on_reset(self, _button: Gtk.Button) -> None:
        self._run_privileged(
            ['reset', '-q'],
            translate('Factory curve restored, automatic re-apply switched off'),
        )

    def _on_battery_apply(self, _button: Gtk.Button) -> None:
        wanted = int(self._battery_scale.get_value())
        if wanted >= 100:
            self._on_battery_off(_button)
            return
        try:
            core.validate_battery(wanted)
        except ValueError as error:
            self._set_status(translate('Not applied: {error}', error=error), error=True)
            return
        self._run_privileged(
            ['battery', 'set', str(wanted), '-q'],
            translate('Charge threshold {value} %: the battery will not charge above '
                      'that level, the value is saved in {path}',
                      value=wanted, path=core.CONFIG_PATH),
        )

    def _on_battery_off(self, _button: Gtk.Button) -> None:
        self._run_privileged(
            ['battery', 'off', '-q'],
            translate('Limit lifted — charging up to 100 %'),
            on_success=lambda: self._set_threshold_value(100),
        )

    def _on_first_analysis(self) -> bool:
        """One automatic measurement after opening, so the tab is not empty."""
        self._first_analysis_id = None
        self._run_analysis()
        return False

    def _on_language_changed(self, drop: Gtk.DropDown, _param) -> None:
        index = drop.get_selected()
        if self._closing or not 0 <= index < len(self._languages):
            return
        code = self._languages[index]
        if code == i18n.current_language():
            return
        self._closing = True
        # the window is rebuilt from a handler of its own widget: let the
        # signal finish before the widget goes away
        GLib.idle_add(self._switch_language, code)

    def _switch_language(self, code: str) -> bool:
        """Reopen the window in another language.

        Labels are built once, so switching means building the window again;
        the editors reload from the config exactly as on a normal start.

        :param code: language chosen in the switcher.
        """
        core.save_language(code)
        i18n.set_language(code)
        GLib.source_remove(self._timer_id)
        if self._first_analysis_id is not None:
            GLib.source_remove(self._first_analysis_id)
        window = MainWindow(self.get_application())
        window.present()
        self.destroy()
        return False

    def _on_analyze(self, _button: Gtk.Button) -> None:
        self._run_analysis()

    def _on_analyze_root(self, _button: Gtk.Button) -> None:
        self._run_privileged(
            ['power', '--json', '--seconds', str(power.SAMPLE_SECONDS)],
            translate('Exact measurement ready: CPU power was read from RAPL'),
            on_output=self._apply_power_json,
        )

    def _apply_power_json(self, output: str) -> None:
        try:
            self._render_power(power.report_from_dict(json.loads(output)))
        except (ValueError, TypeError, KeyError) as error:
            self._set_status(translate('Cannot parse the report: {error}', error=error),
                             error=True)

    def _on_autostart_toggled(self, switch: Gtk.Switch, _param) -> None:
        if self._suppress:
            return
        wanted = switch.get_active()
        self._run_privileged(
            ['autostart', 'on' if wanted else 'off', '-q'],
            translate('Autostart is on') if wanted else translate('Autostart is off'),
        )

    # ------------------------------------------------------------------
    # long operations run in a thread, so the window keeps responding
    # ------------------------------------------------------------------

    def _run_analysis(self) -> None:
        """Measure without privileges: RAPL stays unread, the rest works."""
        if self._analysis_running:
            return
        self._analysis_running = True
        self._set_power_busy(True)
        self._set_status(translate('Measuring the power draw, {seconds} s…',
                                   seconds=power.SAMPLE_SECONDS))

        def worker() -> None:
            try:
                report = power.analyze()
            except Exception as error:  # noqa: BLE001 — anything is shown to the user
                GLib.idle_add(self._analysis_done, error, None)
            else:
                GLib.idle_add(self._analysis_done, None, report)

        threading.Thread(target=worker, daemon=True).start()

    def _analysis_done(self, error: Exception | None,
                       report: power.Report | None) -> bool:
        if self._closing:   # the window was replaced while the worker ran
            return False
        self._analysis_running = False
        self._set_power_busy(False)
        if error is not None or report is None:
            self._set_status(translate('Measurement failed: {error}', error=error),
                             error=True)
            return False
        self._render_power(report)
        found = len(report.findings)
        self._set_status(
            translate('Measurement ready: consumers found — {count}', count=found) if found
            else translate('Measurement ready: no noticeable consumers')
        )
        return False

    def _run_privileged(self, args: list[str], success: str,
                        on_success: Callable[[], None] | None = None,
                        on_output: Callable[[str], None] | None = None) -> None:
        self._set_sensitive(False)
        self._set_status(translate('Waiting for confirmation…'))

        def worker() -> None:
            try:
                output = core.run_privileged(args)
            except Exception as error:  # noqa: BLE001 — anything is shown to the user
                GLib.idle_add(self._finish, error, success, on_success, on_output, '')
            else:
                GLib.idle_add(self._finish, None, success, on_success, on_output, output)

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, error: Exception | None, success: str,
                on_success: Callable[[], None] | None = None,
                on_output: Callable[[str], None] | None = None,
                output: str = '') -> bool:
        if self._closing:   # the window was replaced while the worker ran
            return False
        self._set_sensitive(True)
        if error is None:
            self._set_status(success)
            if on_success is not None:
                on_success()
            # last, so that a parsing failure overwrites the success message
            if on_output is not None:
                on_output(output)
        elif isinstance(error, core.PrivilegeError):
            text = translate('Not done: {error}', error=error)
            if error.command:
                text += '\n' + translate('Command: {command}', command=error.command)
            self._set_status(text, error=True)
        else:
            self._set_status(translate('Error: {error}', error=error), error=True)
        self._refresh_live()
        self._refresh_battery()
        self._refresh_autostart()
        return False


class WattsonApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id='com.yura.Wattson')
        # kept here and not in the window: the totals must outlive a rebuild
        self.network = network.Monitor()

    def do_activate(self) -> None:  # noqa: N802 — the name is dictated by GTK
        window = self.props.active_window
        if window is None:
            window = MainWindow(self)
        window.present()


def run() -> int:
    return WattsonApplication().run([])


if __name__ == '__main__':
    raise SystemExit(run())
