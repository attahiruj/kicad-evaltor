"""Reusable case-sensitive selection for caller-authored checks."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Selection(Generic[T]):
    items: tuple[T, ...]
    pattern: str
    total: int

    @property
    def count(self) -> int:
        return len(self.items)

    def require_count(self, minimum: int = 1, maximum: int | None = None) -> "Selection[T]":
        """Raise ValueError for invalid bounds or unexpected selection cardinality."""
        if minimum < 0 or (maximum is not None and maximum < minimum):
            raise ValueError("Invalid selection count bounds")
        if self.count < minimum or (maximum is not None and self.count > maximum):
            raise ValueError(
                f"Selection {self.pattern!r} matched {self.count} of {self.total} items"
            )
        return self


def select(items: Iterable[T], pattern: str, *, key: Callable[[T], str]) -> Selection[T]:
    """Select names/references/lib IDs with glob syntax; preserve input order."""
    source = tuple(items)
    return Selection(
        tuple(item for item in source if fnmatchcase(key(item), pattern)), pattern, len(source)
    )
