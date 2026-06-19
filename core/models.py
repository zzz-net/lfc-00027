from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional
import uuid
import time


class TaskStatus(str, Enum):
    WAITING = "等待中"
    PRINTING = "打印中"
    FAILED = "失败"
    MANUAL = "需人工处理"
    CANCELLED = "已取消"
    COMPLETED = "已完成"


class CounterType(str, Enum):
    A = "A类柜台"
    B = "B类柜台"
    C = "C类柜台"
    D = "D类柜台"


COUNTER_PRIORITY_DEFAULT = {
    CounterType.A: 1,
    CounterType.B: 2,
    CounterType.C: 3,
    CounterType.D: 4,
}


@dataclass
class PrintTask:
    id: str
    filename: str
    copies: int
    counter: CounterType
    status: TaskStatus = TaskStatus.WAITING
    retry_count: int = 0
    max_retries: int = 3
    fail_reason: Optional[str] = None
    operator: Optional[str] = None
    paused: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    priority_override: Optional[int] = None

    @classmethod
    def create(cls, filename: str, copies: int, counter: CounterType,
               max_retries: int = 3, priority_override: Optional[int] = None) -> "PrintTask":
        return cls(
            id=str(uuid.uuid4()),
            filename=filename,
            copies=copies,
            counter=counter,
            max_retries=max_retries,
            priority_override=priority_override,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["counter"] = self.counter.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PrintTask":
        return cls(
            id=d["id"],
            filename=d["filename"],
            copies=d["copies"],
            counter=CounterType(d["counter"]),
            status=TaskStatus(d["status"]),
            retry_count=d.get("retry_count", 0),
            max_retries=d.get("max_retries", 3),
            fail_reason=d.get("fail_reason"),
            operator=d.get("operator"),
            paused=d.get("paused", False),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            started_at=d.get("started_at"),
            completed_at=d.get("completed_at"),
            priority_override=d.get("priority_override"),
        )

    def touch(self):
        self.updated_at = time.time()


@dataclass
class AppConfig:
    counter_priorities: dict = field(default_factory=lambda: {
        CounterType.A.value: 1,
        CounterType.B.value: 2,
        CounterType.C.value: 3,
        CounterType.D.value: 4,
    })
    max_retries_default: int = 3
    simulate_failure_rate: float = 0.0
    simulate_failure_enabled: bool = False
    print_duration_ms: int = 2000
    operator_name: str = "系统管理员"
    global_paused: bool = False
    last_export_record: Optional[dict] = None
    export_record_ui_state: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AppConfig":
        return cls(
            counter_priorities=d.get("counter_priorities", {
                CounterType.A.value: 1,
                CounterType.B.value: 2,
                CounterType.C.value: 3,
                CounterType.D.value: 4,
            }),
            max_retries_default=d.get("max_retries_default", 3),
            simulate_failure_rate=d.get("simulate_failure_rate", 0.0),
            simulate_failure_enabled=d.get("simulate_failure_enabled", False),
            print_duration_ms=d.get("print_duration_ms", 2000),
            operator_name=d.get("operator_name", "系统管理员"),
            global_paused=d.get("global_paused", False),
            last_export_record=d.get("last_export_record"),
            export_record_ui_state=d.get("export_record_ui_state"),
        )
