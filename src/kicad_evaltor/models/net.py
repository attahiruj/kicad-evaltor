from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kicad_evaltor.models.pin import Pin


@dataclass
class Net:
    name: str
    code: int | None = None
    nodes: list[Pin] = field(default_factory=list)

    @classmethod
    def from_schematic_net(cls, net: Any) -> Net:
        nodes = []
        if hasattr(net, "nodes"):
            for node in net.nodes:
                nodes.append(Pin.from_schematic_node(node))
        return cls(name=net.name, code=getattr(net, "code", None), nodes=nodes)

    @classmethod
    def from_board_net(cls, net: Any) -> Net:
        return cls(name=net.name, code=net.net_code)
