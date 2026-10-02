from __future__ import annotations


def to_mm(nanometers: int | float) -> float:
    """Convert nanometers to millimeters."""
    return nanometers / 1_000_000


def to_nm(millimeters: float) -> int:
    """Convert millimeters to nanometers."""
    return int(round(millimeters * 1_000_000))


def to_mils(millimeters: float) -> float:
    """Convert millimeters to mils (thousandths of an inch)."""
    return millimeters * 39.3701


def from_mils(mils: float) -> float:
    """Convert mils to millimeters."""
    return mils / 39.3701
