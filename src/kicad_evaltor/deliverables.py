"""Structured BOM and project-policy facts; callers author all acceptance rules."""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path


def expand_references(text: str) -> tuple[str, ...]:
    """Parse grouped references and bounded ranges from a designated BOM cell."""
    result = []
    text = re.sub(r"\s*-\s*", "-", text.strip())
    for word in re.split(r"[,;\s]+", text):
        if not word:
            continue
        match = re.fullmatch(r"([A-Za-z#_]+)(\d+)(?:-([A-Za-z#_]*)(\d+))?", word)
        if not match:
            raise ValueError(f"Invalid grouped reference {word!r}")
        prefix, low, other, high = match.groups()
        if high is None:
            result.append(word)
            continue
        if other not in ("", prefix) or int(high) < int(low) or int(high) - int(low) > 100000:
            raise ValueError(f"Invalid reference range {word!r}")
        result.extend(f"{prefix}{n}" for n in range(int(low), int(high) + 1))
    return tuple(result)


@dataclass(frozen=True)
class BOMRow:
    references: tuple[str, ...]
    quantity: int
    fields: dict[str, str]


def read_bom(
    text: str, *, reference_column: str, quantity_column: str, delimiter: str = ","
) -> tuple[BOMRow, ...]:
    rows = []
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if (
        not reader.fieldnames
        or len(reader.fieldnames) != len(set(reader.fieldnames))
        or not {reference_column, quantity_column} <= set(reader.fieldnames)
    ):
        raise ValueError("BOM must contain the caller-selected reference and quantity columns")
    for row in reader:
        if None in row or any(v is None for v in row.values()):
            raise ValueError("Malformed BOM row")
        quantity = int(row[quantity_column])
        if quantity < 0:
            raise ValueError("BOM quantity must be nonnegative")
        rows.append(BOMRow(expand_references(row[reference_column]), quantity, dict(row)))
    return tuple(rows)


def reconcile_bom(rows: Iterable[BOMRow], expected: Mapping[str, Mapping[str, str]]) -> dict:
    """Differences for explicitly selected parts and fields, including quantities."""
    rows = tuple(rows)
    counts = Counter(ref for row in rows for ref in row.references)
    listed = {ref: row for row in rows for ref in row.references}
    return {
        "missing": sorted(set(expected) - listed.keys()),
        "extra": sorted(listed.keys() - expected.keys()),
        "duplicates": sorted(r for r, count in counts.items() if count > 1),
        "quantity_mismatches": [
            {
                "references": row.references,
                "quantity": row.quantity,
                "reference_count": len(row.references),
            }
            for row in rows
            if row.quantity != len(row.references)
        ],
        "field_mismatches": [
            {
                "reference": ref,
                "field": name,
                "expected": value,
                "actual": listed[ref].fields.get(name),
            }
            for ref, fields in expected.items()
            if ref in listed
            for name, value in fields.items()
            if listed[ref].fields.get(name) != value
        ],
    }


def project_rule_settings(path: str | Path, *, domain: str) -> dict:
    if domain not in ("erc", "drc"):
        raise ValueError("domain must be erc or drc")
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    settings = (
        doc.get("erc", {}) if domain == "erc" else doc.get("board", {}).get("design_settings", {})
    )
    return {
        "severities": dict(settings.get("rule_severities", {})),
        "exclusions": list(settings.get(f"{domain}_exclusions", [])),
    }
