"""Every fitted part carries a distributor number for its own part."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.deliverables import BOMRow, expand_references, read_bom
from kicad_evaltor.sourcing.match import (
    PartRule,
    PartView,
    catalogue_mismatch,
    part_identity,
    rule_for,
    same_part,
)
from kicad_evaltor.sourcing.providers import SourcingProvider, default_provider
from kicad_evaltor.sourcing.record import PartRecord


@dataclass
class SourcingParams(CheckParams):
    """Which fields to read, which parts to skip, and what each part must be.

    ``fields`` maps a symbol field name to a platform (``{"LCSC": "lcsc",
    "Digikey": "digikey", "Mouser": "mouser"}``). ``exclude`` holds
    case-sensitive globs; a part is skipped when any matches its reference,
    lib_id, or footprint (with or without library nickname). ``rules`` are
    :class:`PartRule` objects (or dicts of their fields) saying what each
    part's record must show. With ``require_any`` a part needs a number in at
    least one configured field instead of in every one; a number that is
    present must still be right.

    With a BOM, the BOM decides which parts are checked. ``bom`` is a list of
    :class:`BOMRow` (from :func:`read_bom`), of mappings with ``references``
    and ``fields``, or of plain references (grouped cells such as
    ``"R6,R12,R17-R19"`` are expanded); ``bom_path`` instead names a BOM file
    that :func:`read_bom` reads with ``bom_reference_column``,
    ``bom_quantity_column`` and ``bom_delimiter``. Give one or the other. Every
    listed part that exists in the design is checked, through-hole or SMD,
    whatever its DNP, in-BOM or power flags and ``exclude`` say; quantities are
    not used. Those flags and ``exclude`` still say which design parts the BOM
    is expected to list: a part they would check that the BOM leaves out is
    reported, and fails only with ``bom_complete``. A BOM reference absent from
    the design always fails, as does a reference listed on more than one row.

    ``bom_fields`` maps BOM columns to platforms (``{"LCSC Part": "lcsc"}``)
    and, when given, replaces ``fields``: numbers are read from each part's BOM
    row, not from its symbol. It needs a BOM whose rows carry columns.
    """

    fields: dict[str, str] = field(default_factory=lambda: {"LCSC": "lcsc"})
    exclude: list[str] = field(default_factory=list)
    rules: list[Any] = field(default_factory=list)
    allow_shared_identical: bool = True
    require_any: bool = False
    cross_check: bool = True
    check_chip_size: bool = True
    skip_dnp: bool = True
    skip_power: bool = True
    skip_not_in_bom: bool = True
    bom: list[Any] | None = None
    bom_path: str | None = None
    bom_reference_column: str = "Reference"
    bom_quantity_column: str = "Qty"
    bom_delimiter: str = ","
    bom_fields: dict[str, str] = field(default_factory=dict)
    bom_complete: bool = False

    @property
    def number_fields(self) -> dict[str, str]:
        """Where part numbers are read from: BOM columns or symbol fields."""
        return self.bom_fields or self.fields

    def validate(self) -> None:
        if not self.fields:
            raise ValueError("fields must name at least one symbol field")
        for name, platform in self.fields.items():
            if not str(name).strip() or not str(platform).strip():
                raise ValueError("field names and platforms must not be blank")
        if isinstance(self.exclude, str):
            self.exclude = [self.exclude]
        self.rules = [
            rule if isinstance(rule, PartRule) else PartRule(**_rule_kwargs(rule))
            for rule in self.rules
        ]
        if self.bom is not None and self.bom_path is not None:
            raise ValueError("give bom or bom_path, not both")
        if self.bom is not None:
            if isinstance(self.bom, (str, BOMRow, Mapping)):
                self.bom = [self.bom]
            self.bom = [_bom_row(entry) for entry in self.bom]
        if self.bom_path is not None:
            self.bom_path = str(self.bom_path)
        for name, platform in self.bom_fields.items():
            if not str(name).strip() or not str(platform).strip():
                raise ValueError("BOM columns and platforms must not be blank")
        if self.bom_fields:
            if self.bom is None and self.bom_path is None:
                raise ValueError("bom_fields needs a bom or bom_path to read from")
            if self.bom is not None and any(not row["fields"] for row in self.bom):
                raise ValueError("bom_fields reads BOM columns; a plain reference has none")


def _bom_row(entry: Any) -> dict[str, Any]:
    """One BOM row as plain data: its references and its columns."""
    if isinstance(entry, str):
        return {"references": list(expand_references(entry)), "fields": {}}
    if isinstance(entry, BOMRow):
        return {"references": list(entry.references), "fields": dict(entry.fields)}
    if isinstance(entry, Mapping) and "references" in entry:
        references = entry["references"]
        if isinstance(references, str):
            references = expand_references(references)
        fields = {str(k): str(v) for k, v in dict(entry.get("fields") or {}).items()}
        return {"references": [str(ref) for ref in references], "fields": fields}
    raise TypeError("bom entries must be references, BOMRow objects or mappings of their fields")


def _rule_kwargs(rule: Any) -> dict[str, Any]:
    if not isinstance(rule, Mapping):
        raise TypeError("rules must be PartRule objects or mappings of their fields")
    kwargs = dict(rule)
    for name in ("category", "value_attributes", "colour_fields"):
        if isinstance(kwargs.get(name), list):
            kwargs[name] = tuple(kwargs[name])
    return kwargs


@register
class SourcingCheck(Check[SourcingParams]):
    """Look each part number up online and match the listing to the symbol.

    A format check passes one placeholder number on every part; this one
    resolves each number by exact match in the platform's catalogue and
    reports, per part, a number that is missing, malformed, absent from the
    catalogue, or a listing that is not this part (see
    :func:`catalogue_mismatch`). One number on parts that differ fails; with
    ``allow_shared_identical`` parts that differ only in a role Value
    (``RESET`` and ``BOOT`` buttons) may share one. A symbol carrying numbers
    from several platforms also needs records that agree (:func:`same_part`).

    A catalogue that cannot be reached, or refuses the credentials, makes the
    result ERROR naming that platform: an outage is never read as a missing
    part. Each number is looked up once per run.

    ``providers`` maps a platform name to a :class:`SourcingProvider`; any
    platform not given uses the built-in provider of that name.
    """

    id = "sch.sourcing"
    name = "Part Sourcing"
    description = "Checks part numbers against live distributor catalogues"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[SourcingParams]] = SourcingParams

    def __init__(
        self,
        *,
        providers: Mapping[str, SourcingProvider] | None = None,
        requirement_id: str | None = None,
        **params: Any,
    ) -> None:
        super().__init__(requirement_id=requirement_id, **params)
        given = dict(providers or {})
        self.providers: dict[str, SourcingProvider] = {
            platform: given.get(platform) or default_provider(platform)
            for platform in dict.fromkeys(self.params.number_fields.values())
        }

    def _skipped(self, symbol: Any) -> bool:
        params = self.params
        if params.skip_dnp and getattr(symbol, "dnp", False):
            return True
        if params.skip_not_in_bom and not getattr(symbol, "in_bom", True):
            return True
        part = PartView.of(symbol)
        if params.skip_power and (
            part.lib_id.startswith("power:") or part.reference.startswith("#")
        ):
            return True
        names = (part.reference, part.lib_id, part.footprint, part.footprint_name)
        return any(
            fnmatchcase(name, pattern) for pattern in params.exclude for name in names if name
        )

    def _bom_rows(self) -> list[dict[str, Any]] | None:
        """The BOM rows, read from ``bom_path`` when that is how it was given."""
        params = self.params
        if params.bom_path is None:
            return params.bom
        text = Path(params.bom_path).read_text(encoding="utf-8-sig")
        rows = read_bom(
            text,
            reference_column=params.bom_reference_column,
            quantity_column=params.bom_quantity_column,
            delimiter=params.bom_delimiter,
        )
        return [_bom_row(row) for row in rows]

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")
        params = self.params
        rules: list[PartRule] = params.rules
        number_fields = params.number_fields

        try:
            bom = self._bom_rows()
        except (OSError, ValueError) as exc:
            return CheckResult.error(self.id, f"Could not read the BOM {params.bom_path}: {exc}")

        # A multi-unit part is one part: its first unit speaks for it.
        design: dict[str, Any] = {}
        flagged: dict[str, Any] = {}
        for symbol in ctx.sheet_symbols():
            design.setdefault(symbol.reference, symbol)
            if symbol.reference not in flagged and not self._skipped(symbol):
                flagged[symbol.reference] = symbol

        listed: dict[str, dict[str, str]] = {}
        bom_details: dict[str, Any] = {}
        reasons: dict[str, list[str]] = {}
        if bom is None:
            parts = flagged
        else:
            columns = {name for row in bom for name in row["fields"]}
            absent = [name for name in params.bom_fields if bom and name not in columns]
            if absent:
                return CheckResult.error(self.id, f"The BOM has no column {', '.join(absent)}")
            rows_of: dict[str, int] = {}
            for row in bom:
                for reference in row["references"]:
                    rows_of[reference] = rows_of.get(reference, 0) + 1
                    listed.setdefault(reference, row["fields"])
            parts = {ref: design[ref] for ref in listed if ref in design}
            for ref, count in rows_of.items():
                if count > 1 and ref in parts:
                    reasons[ref] = [f"is listed on {count} BOM rows"]
            bom_details = {
                "bom_not_in_design": sorted(listed.keys() - design.keys(), key=_natural),
                "bom_not_listed": sorted(flagged.keys() - listed.keys(), key=_natural),
                "bom_overrides": sorted(parts.keys() - flagged.keys(), key=_natural),
            }

        cache: dict[tuple[str, str], PartRecord | str | None] = {}
        errors: dict[str, list[str]] = {}
        shared: dict[tuple[str, str], dict[tuple[Any, ...], list[str]]] = {}
        records: dict[str, dict[str, Any]] = {}

        for reference in sorted(parts, key=_natural):
            symbol = parts[reference]
            part = PartView.of(symbol)
            source = listed[reference] if params.bom_fields else part.fields
            found = {
                name: source.get(name, "").strip()
                for name in number_fields
                if source.get(name, "").strip()
            }
            problems: list[str] = reasons.pop(reference, [])
            if params.require_any and not found:
                problems.append(f"has no part number in {' or '.join(number_fields)}")
            elif not params.require_any:
                problems += [f"has no {name} number" for name in number_fields if name not in found]

            resolved: list[PartRecord] = []
            for name, raw in found.items():
                platform = number_fields[name]
                provider = self.providers[platform]
                try:
                    number = provider.normalize(raw)
                except ValueError as exc:
                    problems.append(f"{name} {raw!r} is malformed ({exc})")
                    continue
                identity = part_identity(
                    part, rules, include_value=not params.allow_shared_identical
                )
                shared.setdefault((name, number), {}).setdefault(identity, []).append(reference)
                key = (platform, number)
                if key not in cache:
                    try:
                        cache[key] = provider.lookup(number)
                    except LookupError as exc:
                        cache[key] = str(exc)
                outcome = cache[key]
                if isinstance(outcome, str):
                    message = f"{reference} {name} {number}: {outcome}"
                    errors.setdefault(platform, []).append(message)
                    continue
                if outcome is None:
                    problems.append(
                        f"{name} {number} was not found by exact number in the {platform} catalogue"
                    )
                    continue
                records[f"{platform}:{number}"] = outcome.to_dict()
                resolved.append(outcome)
                mismatch = catalogue_mismatch(
                    part, outcome, rules, check_chip_size=params.check_chip_size
                )
                if mismatch:
                    problems.append(f"{name} {number} {mismatch}")
            if params.cross_check and len(resolved) > 1:
                rule = rule_for(part, rules)
                disagreement = same_part(resolved, mpn_stem=rule.mpn_stem if rule else None)
                if disagreement:
                    problems.append(disagreement)
            if problems:
                reasons[reference] = problems

        reused = {
            f"{name} {number}": sorted(
                (ref for refs in groups.values() for ref in refs), key=_natural
            )
            for (name, number), groups in shared.items()
            if len(groups) > 1
        }
        for label, refs in reused.items():
            for ref in refs:
                reasons.setdefault(ref, []).append(f"shares {label} with different parts {refs}")

        reasons = {ref: reasons[ref] for ref in sorted(reasons, key=_natural)}
        details: dict[str, Any] = {
            "checked": sorted(parts, key=_natural),
            "parts": reasons,
            "reused": reused,
            "lookup_errors": errors,
            "records": records,
            **bom_details,
        }
        if errors:
            platforms = ", ".join(sorted(errors))
            first = next(iter(errors.values()))[0]
            return CheckResult.error(
                self.id,
                f"Could not verify part numbers: {platforms} lookup failed ({first})",
                **details,
            )
        unknown = bom_details.get("bom_not_in_design", [])
        unlisted = bom_details.get("bom_not_listed", []) if params.bom_complete else []
        if reasons or unknown or unlisted:
            lines = [
                f"{ref} ({parts[ref].value}): {'; '.join(why)}" for ref, why in reasons.items()
            ]
            if unknown:
                lines.append(f"BOM lists {', '.join(unknown)}, not in the design")
            if unlisted:
                lines.append(f"{', '.join(unlisted)} not on the BOM")
            heading = (
                f"{len(reasons)} part(s) not sourced correctly"
                if reasons
                else "The BOM does not match the design"
            )
            return CheckResult.fail(self.id, f"{heading}: " + " | ".join(lines), **details)
        message = f"{len(parts)} part(s) resolved to matching catalogue listings"
        if bom_details.get("bom_not_listed"):
            message += f"; not on the BOM: {', '.join(bom_details['bom_not_listed'])}"
        return CheckResult.pass_(self.id, message, **details)


def _natural(reference: str) -> tuple[Any, ...]:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", reference))
