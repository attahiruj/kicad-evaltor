"""Explicit conversion of supported evidence to portable JSON values."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import PurePath
from typing import Any


def json_evidence(value: Any, *, path: str = "$", _seen: set[int] | None = None) -> Any:
    """Convert data, paths, enums and dataclasses; reject opaque/cyclic evidence.

    No repr fallback: a lock or callable cannot become convincing measurement
    evidence by silently turning it into a string.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Non-finite evidence at {path}")
        return value
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, Enum):
        return json_evidence(value.value, path=path)
    seen = set() if _seen is None else _seen
    if id(value) in seen:
        raise ValueError(f"Cyclic evidence at {path}")
    identity = id(value)
    seen.add(identity)
    try:
        if is_dataclass(value) and not isinstance(value, type):
            value = {f.name: getattr(value, f.name) for f in fields(value)}
        if isinstance(value, Mapping):
            if any(not isinstance(k, str) for k in value):
                raise TypeError(f"Evidence keys must be strings at {path}")
            return {k: json_evidence(v, path=f"{path}.{k}", _seen=seen) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
            return [json_evidence(v, path=f"{path}[{i}]", _seen=seen) for i, v in enumerate(items)]
        raise TypeError(f"Unsupported evidence {type(value).__name__} at {path}")
    finally:
        seen.remove(identity)
