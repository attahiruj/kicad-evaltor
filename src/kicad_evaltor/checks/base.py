from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, is_dataclass
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
    requirement_id: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)

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
        """A JSON-serialisable view, with the status as its plain string value."""
        return {
            "check_id": self.check_id,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "duration": self.duration,
            "requirement_id": self.requirement_id,
            "parameters": self.parameters,
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

    def __init__(self, *, requirement_id: str | None = None, **params: Any) -> None:
        if requirement_id is not None and not requirement_id.strip():
            raise ValueError("requirement_id must not be blank")
        self.requirement_id = requirement_id
        self._params: ParamsT = self.Params(**params)

    @property
    def params(self) -> ParamsT:
        return self._params

    @property
    def params_dict(self) -> dict[str, Any]:
        """A snapshot of the parameters, safe to keep after the check changes.

        Dataclass params are copied deeply. Anything else, such as a plain object
        a custom check uses, or values that cannot be copied, falls back to a
        shallow copy of its attributes rather than failing the run.
        """
        params = self._params
        if is_dataclass(params) and not isinstance(params, type):
            try:
                return asdict(params)
            except Exception:
                pass
        return dict(getattr(params, "__dict__", {}))

    @abstractmethod
    def run(self, ctx: DesignContext) -> CheckResult:
        pass


def empty_selection(check_id: str, on_empty: str, **details: Any) -> CheckResult:
    """Apply the caller's policy when a selector matches no entities."""
    if on_empty == "pass":
        return CheckResult.pass_(
            check_id, "Selection matched no entities", selected_count=0, **details
        )
    if on_empty == "fail":
        return CheckResult.fail(
            check_id, "Selection matched no entities", selected_count=0, **details
        )
    return CheckResult.skip(check_id, "Selection matched no entities", selected_count=0, **details)
