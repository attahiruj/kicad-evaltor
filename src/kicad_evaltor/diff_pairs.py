"""Differential pairs on a board: finding them by name, and measuring how they run.

Pairs are found the way KiCad's router and DRC find them
(``DRC_ENGINE::MatchDpSuffix``): trailing digits and underscores are skipped,
then the name must end in ``+`` / ``-`` or ``P`` / ``N``. ``USB_D+`` pairs with
``USB_D-``, ``TX_P`` with ``TX_N``, and ``LVDS_P0`` with ``LVDS_N0``. Other
conventions, such as ``USB_DP`` / ``USB_DM`` or lower-case ``_p`` / ``_n``, are
added as explicit suffix pairs.

Measuring needs only the board file. Each track of one net is sampled along its
length and compared with the nearest track of the other net on the same layer:
the edge-to-edge distance is the gap, and a stretch whose gap is beyond the
coupling distance is uncoupled.
"""

from __future__ import annotations

import fnmatch
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from kicad_evaltor.board_file import BoardTrack, FileBoard, arc_circle

Point = tuple[float, float]

KICAD_SUFFIXES: tuple[tuple[str, str], ...] = (("+", "-"), ("P", "N"))
"""The polarity markers KiCad itself recognises, positive first."""


@dataclass(frozen=True)
class DiffPair:
    base: str
    positive: str
    negative: str

    @property
    def nets(self) -> tuple[str, str]:
        return (self.positive, self.negative)


def kicad_complement(net: str) -> tuple[str, str, int] | None:
    """KiCad's own pair match: (base, complement net, +1 or -1), or None.

    A port of ``DRC_ENGINE::MatchDpSuffix``: trailing digits and underscores are
    skipped, then a final ``+``, ``-``, ``P`` or ``N`` marks the polarity.
    """
    count = 0
    for ch in reversed(net):
        count += 1
        if ch.isdigit() or ch == "_":
            continue
        swap = {"+": ("-", 1), "-": ("+", -1), "P": ("N", 1), "N": ("P", -1)}.get(ch)
        if swap is None:
            return None
        base = net[: len(net) - count]
        return base, base + swap[0] + net[len(net) - count + 1 :], swap[1]
    return None


def complement(
    net: str, suffixes: Sequence[tuple[str, str]] | None = None
) -> tuple[str, str, int] | None:
    """The other half of ``net``'s pair: (base, complement, polarity), or None.

    ``suffixes`` None uses KiCad's rule. Otherwise each ``(positive, negative)``
    suffix is tried, longest first, so ``("_DP", "_DM")`` wins over ``("P", "N")``.
    """
    if suffixes is None:
        return kicad_complement(net)
    for pos, neg in sorted(suffixes, key=lambda pair: -max(len(pair[0]), len(pair[1]))):
        if net.endswith(pos):
            base = net[: len(net) - len(pos)]
            return base, base + neg, 1
        if net.endswith(neg):
            base = net[: len(net) - len(neg)]
            return base, base + pos, -1
    return None


def find_diff_pairs(
    nets: Iterable[str],
    *,
    suffixes: Sequence[tuple[str, str]] | None = None,
    pattern: str | None = None,
) -> list[DiffPair]:
    """Every pair whose two halves are both among ``nets``.

    ``pattern`` keeps only pairs with a net matching the wildcard (``"/USB*"``).
    """
    names = set(nets)
    found: dict[str, DiffPair] = {}
    for net in sorted(names):
        match = complement(net, suffixes)
        if match is None:
            continue
        base, other, polarity = match
        if other not in names:
            continue
        positive, negative = (net, other) if polarity > 0 else (other, net)
        if pattern and not any(fnmatch.fnmatchcase(n, pattern) for n in (positive, negative)):
            continue
        found[positive] = DiffPair(base, positive, negative)
    return [found[k] for k in sorted(found)]


@dataclass
class PairMeasurement:
    """How a pair is routed, all in mm."""

    lengths: dict[str, float]
    widths: dict[str, list[float]]
    vias: dict[str, int]
    coupled_length: float = 0.0
    uncoupled_length: dict[str, float] = field(default_factory=dict)
    gap_min: float | None = None
    gap_max: float | None = None
    method: str = "nearest_track_sampling"
    resolution_mm: float = 0.05

    @property
    def skew(self) -> float:
        values = list(self.lengths.values())
        return max(values) - min(values)


def measure_pair(
    board: FileBoard,
    pair: DiffPair,
    *,
    coupling_distance: float,
    include_vias: bool | None = None,
    step: float = 0.05,
) -> PairMeasurement:
    """Lengths, widths, gap and coupling of a pair.

    A point on one net's track is coupled when the nearest track of the other
    net, on the same layer, has its edge within ``coupling_distance``. Gaps are
    reported over coupled points only, which leaves out the pad breakout where a
    pair has to split.
    """
    if (
        not math.isfinite(step)
        or step <= 0
        or not math.isfinite(coupling_distance)
        or coupling_distance < 0
    ):
        raise ValueError(
            "step must be finite and positive; coupling_distance finite and nonnegative"
        )
    tracks = {net: board.tracks_on(net) for net in pair.nets}
    measurement = PairMeasurement(
        lengths={n: board.net_length(n, include_vias=include_vias) for n in pair.nets},
        widths={n: sorted({t.width for t in tracks[n]}) for n in pair.nets},
        vias={n: len(board.vias_on(n)) for n in pair.nets},
        resolution_mm=step,
    )
    gaps: list[float] = []
    coupled = {n: 0.0 for n in pair.nets}
    lines = {id(t): _polyline(t) for net in pair.nets for t in tracks[net]}
    for net, other in (pair.nets, pair.nets[::-1]):
        uncoupled = 0.0
        for track in tracks[net]:
            others = [(t, lines[id(t)]) for t in tracks[other] if t.layer == track.layer]
            for point, weight in _samples(track, step):
                gap = min(
                    (_edge_gap(point, track.width, t, line) for t, line in others),
                    default=math.inf,
                )
                if gap <= coupling_distance:
                    coupled[net] += weight
                    gaps.append(gap)
                else:
                    uncoupled += weight
        measurement.uncoupled_length[net] = round(uncoupled, 4)
    measurement.coupled_length = round(min(coupled.values()), 4)
    if gaps:
        measurement.gap_min = round(min(gaps), 4)
        measurement.gap_max = round(max(gaps), 4)
    return measurement


def _polyline(track: BoardTrack) -> list[Point]:
    if track.mid is None:
        return [track.start, track.end]
    # An arc as short chords, fine enough that a gap is good to a micron.
    pieces = max(4, int(track.length / 0.05))
    circle = arc_circle(track.start, track.mid, track.end)
    if circle is None:
        return [track.start, track.end]
    ux, uy, r, a0, sweep = circle
    return [
        (ux + r * math.cos(a0 + sweep * i / pieces), uy + r * math.sin(a0 + sweep * i / pieces))
        for i in range(pieces + 1)
    ]


def _samples(track: BoardTrack, step: float) -> Iterable[tuple[Point, float]]:
    """Points along the track's centreline, each with the length it stands for."""
    points = _polyline(track)
    for a, b in pairwise(points):
        length = math.dist(a, b)
        n = max(1, math.ceil(length / step))
        for i in range(n):
            t = (i + 0.5) / n
            yield (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])), length / n


def _edge_gap(
    point: Point, width: float, other: BoardTrack, line: list[Point] | None = None
) -> float:
    points = line if line is not None else _polyline(other)
    centre = min(_segment_distance(point, a, b) for a, b in pairwise(points))
    return centre - width / 2 - other.width / 2


def _segment_distance(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = (
        0.0
        if length2 == 0
        else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    )
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))
