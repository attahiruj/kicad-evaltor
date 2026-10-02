from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Track:
    net: int
    layer: str
    start_x: float
    start_y: float
    end_x: float
    end_y: float
    width: int
    length: int | None = None

    @classmethod
    def from_board_track(cls, track: Any) -> Track:
        length = None
        if hasattr(track, "get_length"):
            length = track.get_length()
        elif hasattr(track, "length"):
            length = track.length

        return cls(
            net=track.net,
            layer=track.layer.name if hasattr(track.layer, "name") else str(track.layer),
            start_x=track.start.x,
            start_y=track.start.y,
            end_x=track.end.x,
            end_y=track.end.y,
            width=track.width,
            length=length,
        )

    @classmethod
    def from_board_via(cls, via: Any) -> Track:
        return cls(
            net=via.net,
            layer="via",
            start_x=via.position.x,
            start_y=via.position.y,
            end_x=via.position.x,
            end_y=via.position.y,
            width=via.size.width if hasattr(via.size, "width") else via.drill,
            length=via.height if hasattr(via, "height") else 0,
        )
