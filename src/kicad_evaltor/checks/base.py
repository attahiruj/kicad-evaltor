from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

if TYPE_CHECKING:
    from kicad_evaltor.core.context import DesignContext


class TestStatus(Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"


class CheckCategory(Enum):
    SCHEMATIC = "schematic"
    PCB = "pcb"
    PROJECT = "project"


@dataclass
class CheckResult:
    status: TestStatus
    check_id: str
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    duration: float = 0.0

    @classmethod
    def pass_(cls, check_id: str, message: str = "", **details: Any) -> CheckResult:
        return cls(status=TestStatus.PASS, check_id=check_id, message=message, details=details)

    @classmethod
    def fail(cls, check_id: str, message: str = "", **details: Any) -> CheckResult:
        return cls(status=TestStatus.FAIL, check_id=check_id, message=message, details=details)

    @classmethod
    def error(cls, check_id: str, message: str, **details: Any) -> CheckResult:
        return cls(status=TestStatus.ERROR, check_id=check_id, message=message, details=details)

    @classmethod
    def skip(cls, check_id: str, reason: str, **details: Any) -> CheckResult:
        return cls(status=TestStatus.SKIP, check_id=check_id, message=reason, details=details)

    @property
    def is_pass(self) -> bool:
        return self.status == TestStatus.PASS

    @property
    def is_fail(self) -> bool:
        return self.status in (TestStatus.FAIL, TestStatus.ERROR)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable view of this result.

        The ``status`` becomes its plain string value so the payload can be fed
        straight to ``json.dumps`` and compared without importing the enum.
        """
        return {
            "check_id": self.check_id,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "duration": self.duration,
        }


@dataclass(init=False)
class CheckParams:
    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        pass


# Each check is parameterised by its own params dataclass so that `self.params`
# is typed as that dataclass rather than the empty base, which would make every
# attribute access on it a type error.
ParamsT = TypeVar("ParamsT", bound=CheckParams)


class Check(ABC, Generic[ParamsT]):
    id: str
    name: str
    description: str
    category: CheckCategory

    Params: ClassVar[type[ParamsT]]

    def __init__(self, **params: Any) -> None:
        self._params: ParamsT = self.Params(**params)

    @property
    def params(self) -> ParamsT:
        return self._params

    @property
    def params_dict(self) -> dict[str, Any]:
        return self._params.__dict__

    @abstractmethod
    def run(self, ctx: DesignContext) -> CheckResult:
        pass
