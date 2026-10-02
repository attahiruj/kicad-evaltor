from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Component:
    reference: str
    lib_id: str
    value: str
    footprint: str | None = None
    sheet: str | None = None
    fields: dict[str, str] | None = None
    properties: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.fields is None:
            self.fields = {}
        if self.properties is None:
            self.properties = {}

    @classmethod
    def from_schematic_symbol(cls, symbol: Any) -> Component:
        fields = {}
        if hasattr(symbol, "user_fields"):
            for k, v in symbol.user_fields.items():
                fields[k] = v
        if hasattr(symbol, "fields"):
            for k, v in symbol.fields.items():
                fields[k] = v

        footprint = None
        if hasattr(symbol, "footprint") and symbol.footprint:
            footprint = getattr(symbol.footprint, "text", None)
            if footprint:
                footprint = footprint.value

        return cls(
            reference=symbol.reference,
            lib_id=symbol.lib_id,
            value=symbol.value,
            footprint=footprint,
            sheet=getattr(symbol, "sheet", None),
            fields=fields,
        )

    @classmethod
    def from_board_footprint(cls, footprint: Any) -> Component:
        return cls(
            reference=footprint.reference,
            lib_id=footprint.lib_id,
            value=getattr(footprint, "value", ""),
            footprint=footprint.lib_id,
        )
