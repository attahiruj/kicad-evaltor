"""Placement measurements; user tests choose parts, domains, and limits."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from kicad_evaltor.pcbnew_bridge import BoardGeometry, polygon_distance
from kicad_evaltor.values import parse_quantity


def pad_distance(
    geometry: BoardGeometry, reference: str, target: str, *, net: str | None = None
) -> float | None:
    """Planar edge gap, not a copper route or a decoupling-quality verdict."""
    ref, _, number = target.partition(".")
    a, b = geometry.footprint(reference), geometry.footprint(ref)
    if a is None or b is None:
        return None
    first = [p for p in a.pads if p.copper_layers and (net is None or p.net == net)]
    second = b.pads_numbered(number) if number else [p for p in b.pads if p.copper_layers]
    return min(
        (
            polygon_distance(p.polygon, q.polygon)
            for p in first
            for q in second
            if p.polygon and q.polygon
        ),
        default=None,
    )


@dataclass(frozen=True)
class DecouplingCandidate:
    ic_reference: str
    pin: str
    supply_net: str
    capacitor: str
    return_nets: tuple[str, ...]
    capacitance_f: float | None
    distance_mm: float | None
    dnp: bool


def decoupling_candidates(
    board,
    geometry: BoardGeometry,
    reference: str,
    *,
    capacitor_references: Iterable[str],
    return_nets: Iterable[str],
    power_pins: Iterable[str],
) -> tuple[DecouplingCandidate, ...]:
    """Enumerate candidates under explicit identity and return-domain selection.

    No distance, capacitance, DNP, or one-capacitor-per-pin policy is applied.
    """
    ic = board.footprint(reference)
    if ic is None:
        raise ValueError(f"Footprint {reference!r} missing")
    returns = set(return_nets)
    capacitor_references = tuple(capacitor_references)
    results = []
    for number in power_pins:
        pad = ic.pad(number)
        if pad is None:
            raise ValueError(f"Pin {reference}.{number} missing")
        for ref in capacitor_references:
            cap = board.footprint(ref)
            if cap is None:
                raise ValueError(f"Selected capacitor {ref!r} missing")
            grounded = tuple(sorted(cap.nets & returns))
            if pad.net not in cap.nets or not grounded:
                continue
            quantity = parse_quantity(cap.value, dimension="capacitance")
            results.append(
                DecouplingCandidate(
                    reference,
                    number,
                    pad.net,
                    ref,
                    grounded,
                    quantity.value,
                    pad_distance(geometry, ref, f"{reference}.{number}", net=pad.net),
                    cap.dnp,
                )
            )
    return tuple(results)
