"""Network speed and traffic per application.

Interface counters are read from ``/sys/class/net`` — exact and free. The
per-application figures come from the TCP sockets listed by ``ss``: the
kernel counts ``bytes_sent`` and ``bytes_received`` for every socket, so
adding up the deltas of all sockets of a program gives what that program
moved.

Two limits come with that source. UDP sockets carry no byte counters in
the kernel, so QUIC (HTTP/3) traffic cannot be attributed to anybody. And
a socket only names its process for processes of the current user unless
the reader is root, which is why ``sudo wattson net`` sees more than the
GUI does.

Totals are counted from the moment the monitor starts: what a socket
carried before that is not ours to claim.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import translate

NET_ROOT = Path('/sys/class/net')
LOOPBACK = 'lo'
# -t TCP, -i socket info, -n numeric, -H no header, -O one line per socket,
# -p process, -e socket inode
SS_COMMAND = ('ss', '-tinHOpe')
SS_TIMEOUT = 5

MAX_APPLICATION_ROWS = 10
UNITS = ('B', 'kB', 'MB', 'GB', 'TB')

_PROCESS = re.compile(r'users:\(\("([^"]+)"')
_missing_tool = False


def format_bytes(value: float) -> str:
    """Human readable size in the decimal units traffic is measured in.

    :param value: number of bytes.
    """
    scaled = float(value)
    for unit in UNITS:
        if scaled < 1000 or unit == UNITS[-1]:
            return f'{scaled:.0f} {unit}' if unit == 'B' else f'{scaled:.1f} {unit}'
        scaled /= 1000


def format_rate(value: float) -> str:
    """Human readable speed.

    :param value: bytes per second.
    """
    return translate('{size}/s', size=format_bytes(value))


@dataclass
class Interface:
    """One network interface with its counters and current speed."""

    name: str
    rx_bytes: int = 0        # since the interface came up
    tx_bytes: int = 0
    physical: bool = False   # real hardware, not a tunnel or a bridge
    up: bool = False
    rx_rate: float = 0.0     # bytes per second
    tx_rate: float = 0.0

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'rx_bytes': self.rx_bytes,
            'tx_bytes': self.tx_bytes,
            'physical': self.physical,
            'up': self.up,
            'rx_rate': self.rx_rate,
            'tx_rate': self.tx_rate,
        }


@dataclass
class Application:
    """Traffic of one program, all of its processes added together."""

    name: str
    rx_rate: float = 0.0
    tx_rate: float = 0.0
    rx_total: int = 0        # since the monitor started
    tx_total: int = 0

    def rate(self) -> float:
        return self.rx_rate + self.tx_rate

    def total(self) -> int:
        return self.rx_total + self.tx_total

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'rx_rate': self.rx_rate,
            'tx_rate': self.tx_rate,
            'rx_total': self.rx_total,
            'tx_total': self.tx_total,
        }


@dataclass
class Snapshot:
    """State of the network at one moment, with the rates of the last interval."""

    interfaces: list[Interface] = field(default_factory=list)
    applications: list[Application] = field(default_factory=list)
    seconds: float = 0.0     # interval the rates were measured over, 0 on the first sample
    watching: float = 0.0    # how long the monitor has been running
    sockets: int = 0         # TCP sockets a program could be named for

    def visible_interfaces(self) -> list[Interface]:
        """Interfaces worth showing: up, or moving data right now."""
        return [item for item in self.interfaces
                if item.up or item.rx_rate or item.tx_rate]

    @property
    def rx_rate(self) -> float:
        """Download speed over the real interfaces, tunnels not counted twice."""
        return sum(item.rx_rate for item in self.interfaces if item.physical and item.up)

    @property
    def tx_rate(self) -> float:
        return sum(item.tx_rate for item in self.interfaces if item.physical and item.up)

    @property
    def rx_bytes(self) -> int:
        return sum(item.rx_bytes for item in self.interfaces if item.physical and item.up)

    @property
    def tx_bytes(self) -> int:
        return sum(item.tx_bytes for item in self.interfaces if item.physical and item.up)

    def to_dict(self) -> dict:
        return {
            'seconds': self.seconds,
            'watching': self.watching,
            'sockets': self.sockets,
            'rx_rate': self.rx_rate,
            'tx_rate': self.tx_rate,
            'rx_bytes': self.rx_bytes,
            'tx_bytes': self.tx_bytes,
            'interfaces': [item.to_dict() for item in self.interfaces],
            'applications': [item.to_dict() for item in self.applications],
        }


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _read_str(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ''


def read_interfaces() -> list[Interface]:
    """Every interface except the loopback, with its counters."""
    interfaces = []
    try:
        entries = sorted(NET_ROOT.iterdir())
    except OSError:
        return interfaces
    for entry in entries:
        if entry.name == LOOPBACK:
            continue
        received = _read_int(entry / 'statistics' / 'rx_bytes')
        sent = _read_int(entry / 'statistics' / 'tx_bytes')
        if received is None or sent is None:
            continue
        interfaces.append(Interface(
            name=entry.name,
            rx_bytes=received,
            tx_bytes=sent,
            # a tunnel or a bridge has no device behind it
            physical=(entry / 'device').exists(),
            up=_read_str(entry / 'operstate') in ('up', 'unknown'),
        ))
    return interfaces


def _socket_traffic() -> dict[str, tuple[str, int, int]]:
    """{socket inode: (program, bytes sent, bytes received)} for TCP sockets.

    Sockets without a named process are skipped: they belong to another
    user and there is nothing to attribute them to.
    """
    global _missing_tool
    try:
        result = subprocess.run(SS_COMMAND, capture_output=True, text=True,
                                timeout=SS_TIMEOUT, check=False)
    except FileNotFoundError:
        _missing_tool = True
        return {}
    except (OSError, subprocess.SubprocessError):
        return {}
    _missing_tool = False

    sockets = {}
    for line in result.stdout.splitlines():
        found = _PROCESS.search(line)
        if found is None:
            continue
        inode, sent, received = '', 0, 0
        # the process field may hold spaces, the numeric fields never do
        for token in line.split():
            if token.startswith('ino:'):
                inode = token[len('ino:'):]
            elif token.startswith('bytes_sent:'):
                sent = int(token[len('bytes_sent:'):] or 0)
            elif token.startswith('bytes_received:'):
                received = int(token[len('bytes_received:'):] or 0)
        if inode:
            sockets[inode] = (found.group(1), sent, received)
    return sockets


def tool_missing() -> bool:
    """True when ``ss`` could not be started at the last sample.

    Without it interface speed still works and only the per-application
    part is empty, which is worth saying out loud instead of showing a
    table that stays blank for no visible reason.
    """
    return _missing_tool


class Monitor:
    """Keeps counters between samples: rates and totals need two readings.

    One instance lives as long as the interface does, so the totals it
    reports are what has moved since the program was started.
    """

    def __init__(self) -> None:
        self._interfaces: dict[str, tuple[int, int]] = {}
        self._sockets: dict[str, tuple[int, int]] = {}
        self._totals: dict[str, list[int]] = {}
        self._moment: float | None = None
        self._started: float | None = None

    def sample(self) -> Snapshot:
        """One reading. Speeds appear from the second call on."""
        now = time.monotonic()
        first = self._moment is None
        seconds = 0.0 if first else max(0.0, now - self._moment)

        interfaces = read_interfaces()
        for interface in interfaces:
            previous = self._interfaces.get(interface.name)
            if previous is not None and seconds > 0:
                interface.rx_rate = max(0, interface.rx_bytes - previous[0]) / seconds
                interface.tx_rate = max(0, interface.tx_bytes - previous[1]) / seconds
            self._interfaces[interface.name] = (interface.rx_bytes, interface.tx_bytes)

        sockets = _socket_traffic()
        moved: dict[str, list[int]] = {}
        for inode, (name, sent, received) in sockets.items():
            previous = self._sockets.get(inode)
            if first:
                # what an already open socket carried before is not ours to count
                continue
            if previous is None:
                # opened after we started watching, so all of it happened on our watch
                delta_rx, delta_tx = received, sent
            else:
                delta_rx = max(0, received - previous[1])
                delta_tx = max(0, sent - previous[0])
            if not delta_rx and not delta_tx:
                continue
            for store in (self._totals.setdefault(name, [0, 0]),
                          moved.setdefault(name, [0, 0])):
                store[0] += delta_rx
                store[1] += delta_tx

        self._sockets = {inode: (sent, received)
                         for inode, (_name, sent, received) in sockets.items()}
        self._moment = now
        if self._started is None:
            self._started = now

        applications = []
        for name, (rx_total, tx_total) in self._totals.items():
            rx_step, tx_step = moved.get(name, (0, 0))
            applications.append(Application(
                name=name,
                rx_rate=rx_step / seconds if seconds > 0 else 0.0,
                tx_rate=tx_step / seconds if seconds > 0 else 0.0,
                rx_total=rx_total,
                tx_total=tx_total,
            ))
        applications.sort(key=lambda item: (-item.rate(), -item.total(), item.name))

        return Snapshot(interfaces=interfaces, applications=applications,
                        seconds=seconds, watching=now - self._started,
                        sockets=len(sockets))
