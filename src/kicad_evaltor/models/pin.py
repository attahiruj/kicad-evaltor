from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Pin:
    ref: str
    pin: str
    net: str
    x: float | None = None
    y: float | None = None

    @classmethod
    def from_schematic_node(cls, node: Any) -> Pin:
        return cls(
            ref=node.ref,
            pin=node.pin,
            net=node.net,
            x=getattr(node, "x", None),
            y=getattr(node, "y", None),
        )

    @classmethod
    def from_board_pad(cls, pad: Any) -> Pin:
        return cls(
            ref=pad.parent.reference if hasattr(pad, "parent") else "",
            pin=pad.pad_number,
            net=pad.net.name if pad.net else "",
            x=pad.position.x if hasattr(pad.position, "x") else None,
            y=pad.position.y if hasattr(pad.position, "y") else None,
        )
