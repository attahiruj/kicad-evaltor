"""Following signals across parts, not just along one net.

A netlist answers "which pins share this net". Many design questions are one
step wider: does the connector's D+ reach the bridge's D+ *through* the 22 ohm
series resistor? Is this resistor between a regulator's output and its feedback
pin? This module answers those by treating chosen parts as conductors.

Which parts conduct, and between which of their pins, is the caller's decision,
made with a *bridge*: a function that looks at one component and its connected
pins and returns the groups of pins that are joined inside it. A series
resistor joins its two pins; a flow-through ESD array joins each channel's in
and out pins and nothing else; an IC joins nothing. The ready-made bridges below
cover the common cases and combine with :func:`any_of`.

Traversal never crosses into a net that ``stop_at`` rejects. The default stops
at power rails, because one pull-up into ``+3V3`` would otherwise connect every
signal on the board to every other.
"""

from __future__ import annotations

import fnmatch
import re
from collections import deque
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field

from kicad_evaltor.netlist import FileNet, FileNetComponent, FileNetlist, FileNetNode
from kicad_evaltor.values import parse_quantity

Bridge = Callable[[FileNetComponent, Sequence[FileNetNode]], Iterable[Collection[str]]]
"""Groups of pin numbers that conduct inside one component; none when it blocks."""

NetFilter = Callable[[FileNet], bool]

_REF_PREFIX = re.compile(r"^[A-Za-z_#]+")


def ref_prefix(ref: str) -> str:
    """The letters of a reference designator: ``R`` for ``R12``, ``FB`` for ``FB3``."""
    match = _REF_PREFIX.match(ref)
    return match.group(0) if match else ""


def _component(netlist: FileNetlist, ref: str) -> FileNetComponent:
    return netlist.components.get(ref) or FileNetComponent(ref=ref)


def series_parts(*prefixes: str, max_ohms: float | None = None) -> Bridge:
    """Two-pin parts whose reference starts with one of ``prefixes`` conduct.

    The prefix is the whole letter part of the reference, so ``"R"`` takes
    ``R7`` but not ``RV1`` or ``RN2``. A part with more or fewer than two
    connected pins never conducts, which keeps a mislabelled network or
    potentiometer from bridging signals it does not join.

    ``max_ohms`` bounds the bridge by value: a part conducts only when its
    Value reads, by :func:`~kicad_evaltor.values.parse_quantity` as a
    resistance, at or below the bound. That keeps a 22 ohm series resistor
    in line while a 10 k pull-up no longer joins its signal to the rail, and
    ``max_ohms=0`` takes only 0 ohm links. A Value that does not read as a
    resistance (``2.2uH``, ``BLM18PG221``, ``DNP``, an empty one) does *not*
    conduct under a bound: an unknown value is never assumed small. Parts
    rated by something other than resistance, such as ferrite beads and
    inductors, are given their own unbounded bridge and joined with
    :func:`any_of`: ``any_of(series_parts("R", max_ohms=1000), series_parts("FB", "L"))``.
    """
    wanted = {p.upper() for p in prefixes}
    if max_ohms is not None and (
        isinstance(max_ohms, bool) or not isinstance(max_ohms, (int, float)) or max_ohms < 0
    ):
        raise ValueError("max_ohms must be a nonnegative number or None")

    def bridge(component: FileNetComponent, pins: Sequence[FileNetNode]) -> list[set[str]]:
        if ref_prefix(component.ref).upper() not in wanted or len(pins) != 2:
            return []
        if max_ohms is not None:
            ohms = parse_quantity(component.value, dimension="resistance").value
            if ohms is None or ohms > max_ohms:
                return []
        return [{pins[0].pin, pins[1].pin}]

    return bridge


two_terminal_passives: Bridge = series_parts("R", "L", "FB")
"""The default bridge: resistors, inductors and ferrite beads in series."""


def whole_part(*selectors: str) -> Bridge:
    """Every connected pin of the selected parts is joined to every other.

    A selector is a reference (``R5``) or a ``lib_id`` glob
    (``Device:R*``, ``Jumper:SolderJumper_2_Bridged``). Meant for parts that
    really are one conductor, such as a closed jumper or a 0 ohm link.
    """

    def bridge(component: FileNetComponent, pins: Sequence[FileNetNode]) -> list[set[str]]:
        if _selected(component, selectors):
            return [{p.pin for p in pins}]
        return []

    return bridge


def pin_pairs(selector: str, *pairs: tuple[str, str]) -> Bridge:
    """Named pins of the selected parts conduct pairwise.

    For flow-through protection arrays, where pin 1 and pin 10 are the two ends
    of one channel: ``pin_pairs("U3", ("1", "10"), ("2", "9"))``. ``selector``
    is a reference or a ``lib_id`` glob, as for :func:`whole_part`.
    """
    groups = [set(pair) for pair in pairs]

    def bridge(component: FileNetComponent, pins: Sequence[FileNetNode]) -> list[set[str]]:
        return groups if _selected(component, (selector,)) else []

    return bridge


def same_pin_name(*selectors: str) -> Bridge:
    """Pins of the selected parts that share a pin name conduct.

    Many flow-through symbols draw each channel as two pins with one name
    (``IO1`` on either side of the body); this joins those without listing
    numbers.
    """

    def bridge(component: FileNetComponent, pins: Sequence[FileNetNode]) -> list[set[str]]:
        if not _selected(component, selectors):
            return []
        groups: dict[str, set[str]] = {}
        for pin in pins:
            if pin.pinfunction:
                groups.setdefault(pin.pinfunction, set()).add(pin.pin)
        return [g for g in groups.values() if len(g) > 1]

    return bridge


def any_of(*bridges: Bridge) -> Bridge:
    """A part conducts wherever any of ``bridges`` says it does."""

    def bridge(component: FileNetComponent, pins: Sequence[FileNetNode]) -> list[Collection[str]]:
        groups: list[Collection[str]] = []
        for each in bridges:
            groups.extend(each(component, pins))
        return groups

    return bridge


def no_parts(component: FileNetComponent, pins: Sequence[FileNetNode]) -> list[set[str]]:
    """Nothing conducts: reachability is just the starting net."""
    return []


def _selected(component: FileNetComponent, selectors: Iterable[str]) -> bool:
    for selector in selectors:
        if ":" in selector or any(c in selector for c in "*?["):
            if fnmatch.fnmatchcase(component.lib_id, selector):
                return True
        elif component.ref == selector:
            return True
    return False


_GROUND_NAME = re.compile(r"^(/.*/)?(A|D|P|C)?GND[A-Z0-9_]*$|^(/.*/)?(VSS|VEE)[A-Z0-9_]*$", re.I)
_RAIL_NAME = re.compile(
    r"^(/.*/)?(\+|-)?\d+(\.\d+)?V\d*[A-Z0-9_]*$|^(/.*/)?\+?(VCC|VDD|VBUS|VBAT|VIN|VSYS)[A-Z0-9_]*$",
    re.I,
)


def power_rails(net: FileNet) -> bool:
    """Whether ``net`` looks like a supply or ground rail.

    A net counts when any pin on it is a power input or output, or when its
    name is a conventional rail name (``GND``, ``AGND``, ``+3V3``, ``+3.3V``,
    ``VBUS``, ``VCC``...). Either alone misses cases: a passive-only ground
    plane has no power pins, and a rail named ``/LDO_OUT`` has no telling name.
    """
    if any(node.pintype in ("power_in", "power_out") for node in net.nodes):
        return True
    return bool(_GROUND_NAME.match(net.name) or _RAIL_NAME.match(net.name))


def never(net: FileNet) -> bool:
    """Stop at no net."""
    return False


@dataclass
class Reach:
    """What a traversal reached.

    ``nets`` holds every net visited, the start included. ``nodes`` holds every
    pin on those nets. ``crossed`` holds the references of the parts passed
    through, in the order they were first crossed. ``stopped`` holds the nets
    that were next to the reached ones but were not entered.
    """

    nets: set[str] = field(default_factory=set)
    nodes: list[FileNetNode] = field(default_factory=list)
    crossed: list[str] = field(default_factory=list)
    stopped: set[str] = field(default_factory=set)

    def pins_of(self, ref: str) -> list[FileNetNode]:
        """The reached pins that belong to ``ref``."""
        return [n for n in self.nodes if n.ref == ref]

    def touches(self, ref: str, pin: str | None = None) -> bool:
        """Whether ``ref`` (or its pin ``pin``) was reached."""
        return any(n.ref == ref and (pin is None or n.pin == pin) for n in self.nodes)


@dataclass(frozen=True)
class PathStep:
    reference: str
    from_pin: str
    to_pin: str
    from_net: str
    to_net: str


@dataclass(frozen=True)
class CircuitPath:
    start: FileNetNode
    end: FileNetNode
    steps: tuple[PathStep, ...]

    @property
    def nets(self) -> tuple[str, ...]:
        return (self.start.net, *(s.to_net for s in self.steps))


class Connectivity:
    """A netlist seen as a graph of nets joined by conducting parts."""

    def __init__(self, netlist: FileNetlist) -> None:
        self.netlist = netlist

    def neighbors(self, ref: str, pin: str) -> list[FileNetNode]:
        """The other pins on the same net as pin ``pin`` of ``ref``."""
        net = self.netlist.net_of(ref, pin)
        if net is None:
            return []
        return [n for n in self.netlist.nodes_on(net) if (n.ref, n.pin) != (ref, pin)]

    def path(
        self,
        a: tuple[str, str],
        b: tuple[str, str],
        *,
        through: Bridge = no_parts,
        max_crossings: int | None = None,
        forbidden: Collection[str] = (),
        stop_at: NetFilter = never,
        directed=None,
    ) -> CircuitPath | None:
        """A shortest crossing-count witness under caller-supplied conduction rules.

        ``directed(component, pins, entry_pin)`` may return permitted exit pin
        numbers for directional or state-dependent models. No transistor,
        diode or switch behavior is inferred. Missing endpoints raise; they
        cannot establish isolation.
        """
        if max_crossings is not None and (
            isinstance(max_crossings, bool)
            or not isinstance(max_crossings, int)
            or max_crossings < 0
        ):
            raise ValueError("max_crossings must be a nonnegative integer or None")
        start, end = self.netlist.node(*a), self.netlist.node(*b)
        if start is None or end is None:
            raise ValueError(f"Endpoint missing from netlist: {a if start is None else b}")
        blocked = set(forbidden)
        if start.net in blocked or end.net in blocked:
            return None
        queue: deque[tuple[str, tuple[PathStep, ...]]] = deque([(start.net, ())])
        seen = {start.net}
        while queue:
            net, steps = queue.popleft()
            if net == end.net:
                return CircuitPath(start, end, steps)
            if max_crossings is not None and len(steps) >= max_crossings:
                continue
            for entry in self.netlist.nodes_on(net):
                pins = self.netlist.pins_of(entry.ref)
                component = _component(self.netlist, entry.ref)
                exits = (
                    set(directed(component, pins, entry.pin))
                    if directed is not None
                    else {
                        number
                        for group in through(component, pins)
                        if entry.pin in group
                        for number in group
                    }
                )
                for target in pins:
                    if target.pin not in exits or target.net in seen or target.net in blocked:
                        continue
                    next_net = self.netlist.net(target.net)
                    if next_net is None or stop_at(next_net):
                        continue
                    seen.add(target.net)
                    step = PathStep(entry.ref, entry.pin, target.pin, net, target.net)
                    queue.append((target.net, (*steps, step)))
        return None

    def reach(
        self,
        start: str,
        *,
        through: Bridge = two_terminal_passives,
        stop_at: NetFilter = power_rails,
        max_crossings: int | None = None,
    ) -> Reach:
        """Every net and pin reachable from the net named ``start``.

        ``start`` itself is always entered, even when ``stop_at`` would reject
        it, so asking from a rail returns that rail. Unknown nets reach nothing.
        """
        if max_crossings is not None and (
            isinstance(max_crossings, bool)
            or not isinstance(max_crossings, int)
            or max_crossings < 0
        ):
            raise ValueError("max_crossings must be a nonnegative integer or None")
        result = Reach()
        first = self.netlist.net(start)
        if first is None:
            return result

        queue = [first]
        result.nets.add(first.name)
        distances = {first.name: 0}
        while queue:
            net = queue.pop(0)
            result.nodes.extend(net.nodes)
            if max_crossings is not None and distances[net.name] >= max_crossings:
                continue
            for ref in dict.fromkeys(n.ref for n in net.nodes):
                for next_net in self._across(ref, net.name, through):
                    if next_net.name in result.nets:
                        continue
                    if stop_at(next_net):
                        result.stopped.add(next_net.name)
                        continue
                    if ref not in result.crossed:
                        result.crossed.append(ref)
                    result.nets.add(next_net.name)
                    distances[next_net.name] = distances[net.name] + 1
                    queue.append(next_net)
        result.stopped -= result.nets
        return result

    def reach_from_pin(
        self,
        ref: str,
        pin: str,
        *,
        through: Bridge = two_terminal_passives,
        stop_at: NetFilter = power_rails,
    ) -> Reach:
        """:meth:`reach` from the net pin ``pin`` of ``ref`` is on."""
        net = self.netlist.net_of(ref, pin)
        return self.reach(net, through=through, stop_at=stop_at) if net else Reach()

    def connected(
        self,
        a: tuple[str, str],
        b: tuple[str, str],
        *,
        through: Bridge = two_terminal_passives,
        stop_at: NetFilter = power_rails,
    ) -> bool:
        """Whether pin ``a`` = (ref, pin) reaches pin ``b``."""
        return self.reach_from_pin(*a, through=through, stop_at=stop_at).touches(*b)

    def _across(self, ref: str, net: str, through: Bridge) -> list[FileNet]:
        """The nets on the far side of ``ref`` from ``net``."""
        pins = self.netlist.pins_of(ref)
        here = {p.pin for p in pins if p.net == net}
        far: dict[str, FileNet] = {}
        for group in through(_component(self.netlist, ref), pins):
            if not here.intersection(group):
                continue
            for pin in pins:
                if pin.pin in group and pin.net != net and pin.net not in far:
                    found = self.netlist.net(pin.net)
                    if found is not None:
                        far[pin.net] = found
        return list(far.values())
