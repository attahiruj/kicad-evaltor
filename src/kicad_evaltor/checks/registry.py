from __future__ import annotations

from typing import Any

from kicad_evaltor.checks.base import Check


class CheckRegistry:
    _registry: dict[str, type[Check]] = {}

    @classmethod
    def register(cls, check_cls: type[Check] | None = None, *, id: str | None = None) -> Any:
        def decorator(check_class: type[Check]) -> type[Check]:
            check_id = id or check_class.id
            if check_id in cls._registry:
                raise ValueError(f"Check with id '{check_id}' already registered")
            cls._registry[check_id] = check_class
            return check_class

        if check_cls is None:
            return decorator
        return decorator(check_cls)

    @classmethod
    def get(cls, check_id: str) -> type[Check]:
        if check_id not in cls._registry:
            raise KeyError(f"Check '{check_id}' not found in registry")
        return cls._registry[check_id]

    @classmethod
    def list(cls) -> list[tuple[str, type[Check]]]:
        return list(cls._registry.items())

    @classmethod
    def create(cls, check_id: str, **params: Any) -> Check:
        check_class = cls.get(check_id)
        return check_class(**params)

    @classmethod
    def clear(cls) -> None:
        cls._registry.clear()


register = CheckRegistry.register
