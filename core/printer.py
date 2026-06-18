import random
import time
import threading
from typing import Callable, Optional
from dataclasses import dataclass

from .models import PrintTask, TaskStatus, AppConfig


@dataclass
class PrintResult:
    success: bool
    fail_reason: Optional[str] = None


class SimulatedPrinter:
    FAILURE_REASONS = [
        "模拟卡纸：纸张在进纸口堵塞",
        "模拟缺墨：墨盒墨水不足",
        "模拟脱机：打印机网络连接中断",
        "模拟内存不足：文件过大无法缓冲",
        "模拟驱动异常：打印机驱动响应超时",
        "模拟纸张规格不匹配：纸盒无对应纸张",
    ]

    def __init__(self, get_config: Callable[[], AppConfig]):
        self._get_config = get_config
        self._current_task_id: Optional[str] = None
        self._print_lock = threading.Lock()
        self._cancel_event = threading.Event()

    @property
    def is_busy(self) -> bool:
        return self._current_task_id is not None

    @property
    def current_task_id(self) -> Optional[str]:
        return self._current_task_id

    def cancel_current(self):
        self._cancel_event.set()

    def execute_print(self, task: PrintTask,
                      on_progress: Optional[Callable[[int], None]] = None,
                      check_cancelled: Optional[Callable[[], bool]] = None) -> PrintResult:
        config = self._get_config()
        duration_ms = max(100, config.print_duration_ms)
        total_steps = max(5, duration_ms // 100)
        step_ms = duration_ms / total_steps

        with self._print_lock:
            self._current_task_id = task.id
            self._cancel_event.clear()

        try:
            for step in range(total_steps):
                if self._cancel_event.is_set():
                    return PrintResult(success=False, fail_reason="用户取消打印")
                if check_cancelled and check_cancelled():
                    return PrintResult(success=False, fail_reason="任务状态变更为已取消")

                time.sleep(step_ms / 1000.0)

                if on_progress:
                    progress = int((step + 1) / total_steps * 100)
                    try:
                        on_progress(progress)
                    except Exception:
                        pass

                if config.simulate_failure_enabled and config.simulate_failure_rate > 0:
                    failure_prob_per_step = 1 - (1 - config.simulate_failure_rate) ** (1 / total_steps)
                    if random.random() < failure_prob_per_step:
                        reason = random.choice(self.FAILURE_REASONS)
                        return PrintResult(success=False, fail_reason=reason)

            return PrintResult(success=True)

        finally:
            with self._print_lock:
                self._current_task_id = None
                self._cancel_event.clear()
