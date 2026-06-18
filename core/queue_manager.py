import threading
import time
from typing import List, Optional, Callable, Set
from collections import deque

from .models import PrintTask, TaskStatus, CounterType, AppConfig
from .storage import Storage
from .printer import SimulatedPrinter, PrintResult


class QueueManager:
    def __init__(self, storage: Storage, config: AppConfig,
                 on_tasks_changed: Optional[Callable[[], None]] = None,
                 on_log: Optional[Callable[[str], None]] = None):
        self._storage = storage
        self._config = config
        self._tasks: List[PrintTask] = storage.load_tasks()
        self._on_tasks_changed = on_tasks_changed
        self._on_log = on_log
        self._lock = threading.RLock()
        self._printer = SimulatedPrinter(get_config=lambda: self._config)
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_stop_event = threading.Event()
        self._global_paused = config.global_paused
        self._dirty = False

        self._tasks = [t for t in self._tasks if t is not None]
        for t in self._tasks:
            if t.status == TaskStatus.PRINTING:
                t.status = TaskStatus.WAITING
                t.started_at = None
                t.touch()
                self._mark_dirty()
        if self._dirty:
            self._save()

    @property
    def tasks(self) -> List[PrintTask]:
        with self._lock:
            return list(self._tasks)

    @property
    def config(self) -> AppConfig:
        return self._config

    def update_config(self, new_config: AppConfig):
        with self._lock:
            self._config = new_config
            self._global_paused = new_config.global_paused
            self._storage.save_config(new_config)
            self._notify()

    @property
    def printer(self) -> SimulatedPrinter:
        return self._printer

    @property
    def is_worker_running(self) -> bool:
        return self._worker_thread is not None and self._worker_thread.is_alive()

    @property
    def global_paused(self) -> bool:
        return self._global_paused

    def set_global_paused(self, paused: bool, operator: Optional[str] = None):
        with self._lock:
            self._global_paused = paused
            self._config.global_paused = paused
            self._storage.save_config(self._config)
            if operator:
                self._log(f"{'全局暂停' if paused else '全局继续'}：操作者 {operator}")
            self._notify()

    def add_tasks(self, new_tasks: List[PrintTask]) -> int:
        with self._lock:
            count = 0
            for t in new_tasks:
                if t is None:
                    continue
                self._tasks.append(t)
                count += 1
                self._log(f"导入任务：{t.filename}（{t.counter.value}，{t.copies}份）")
            self._mark_dirty()
            self._save()
            self._sort_tasks()
            self._notify()
            return count

    def get_task(self, task_id: str) -> Optional[PrintTask]:
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    return t
            return None

    def pause_task(self, task_id: str, operator: Optional[str] = None) -> bool:
        with self._lock:
            task = self._find_task(task_id)
            if not task:
                return False
            if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.MANUAL):
                self._log(f"暂停失败：{task.filename} 状态为{task.status.value}，不可暂停")
                return False
            if task.paused:
                self._log(f"暂停失败：{task.filename} 已经处于暂停状态")
                return False
            task.paused = True
            task.operator = operator or self._config.operator_name
            task.touch()
            self._log(f"暂停任务：{task.filename}（操作者：{task.operator}）")
            self._save()
            self._notify()
            return True

    def resume_task(self, task_id: str, operator: Optional[str] = None) -> bool:
        with self._lock:
            task = self._find_task(task_id)
            if not task:
                return False
            if not task.paused:
                self._log(f"继续失败：{task.filename} 不处于暂停状态")
                return False
            if task.status == TaskStatus.CANCELLED:
                self._log(f"继续失败：{task.filename} 已取消，不能直接继续，请先使用'撤销取消'")
                return False
            task.paused = False
            task.operator = operator or self._config.operator_name
            task.touch()
            self._log(f"继续任务：{task.filename}（操作者：{task.operator}）")
            self._save()
            self._notify()
            return True

    def cancel_task(self, task_id: str, operator: Optional[str] = None) -> bool:
        with self._lock:
            task = self._find_task(task_id)
            if not task:
                return False
            if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                self._log(f"取消失败：{task.filename} 状态为{task.status.value}，不可取消")
                return False
            if self._printer.current_task_id == task_id:
                self._printer.cancel_current()

            task.status = TaskStatus.CANCELLED
            task.paused = False
            task.operator = operator or self._config.operator_name
            task.touch()
            self._log(f"取消任务：{task.filename}（操作者：{task.operator}）")
            self._save()
            self._notify()
            return True

    def uncancel_task(self, task_id: str, operator: Optional[str] = None) -> bool:
        with self._lock:
            task = self._find_task(task_id)
            if not task:
                return False
            if task.status != TaskStatus.CANCELLED:
                self._log(f"撤销取消失败：{task.filename} 状态为{task.status.value}，不是已取消状态")
                return False
            task.status = TaskStatus.WAITING
            task.fail_reason = None
            task.operator = operator or self._config.operator_name
            task.touch()
            self._log(f"撤销取消：{task.filename}（操作者：{task.operator}），恢复为等待中")
            self._save()
            self._sort_tasks()
            self._notify()
            return True

    def retry_task(self, task_id: str, operator: Optional[str] = None) -> bool:
        with self._lock:
            task = self._find_task(task_id)
            if not task:
                return False
            if task.status not in (TaskStatus.FAILED, TaskStatus.MANUAL):
                self._log(f"重试失败：{task.filename} 状态为{task.status.value}，不可重试")
                return False
            if task.status == TaskStatus.FAILED and task.retry_count >= task.max_retries > 0:
                self._log(f"重试失败：{task.filename} 已达到最大重试次数({task.max_retries})，需人工处理")
                return False
            task.status = TaskStatus.WAITING
            task.fail_reason = None
            task.operator = operator or self._config.operator_name
            task.touch()
            self._log(f"重试任务：{task.filename}（操作者：{task.operator}），当前重试次数{task.retry_count}")
            self._save()
            self._sort_tasks()
            self._notify()
            return True

    def mark_manual_done(self, task_id: str, operator: Optional[str] = None) -> bool:
        with self._lock:
            task = self._find_task(task_id)
            if not task:
                return False
            if task.status != TaskStatus.MANUAL:
                self._log(f"人工完成失败：{task.filename} 状态为{task.status.value}，不是需人工处理状态")
                return False
            task.status = TaskStatus.COMPLETED
            task.completed_at = time.time()
            task.fail_reason = None
            task.operator = operator or self._config.operator_name
            task.touch()
            self._log(f"人工处理完成：{task.filename}（操作者：{task.operator}）")
            self._save()
            self._notify()
            return True

    def remove_task(self, task_id: str, operator: Optional[str] = None) -> bool:
        with self._lock:
            task = self._find_task(task_id)
            if not task:
                return False
            if self._printer.current_task_id == task_id:
                self._printer.cancel_current()
            self._tasks.remove(task)
            self._log(f"删除任务：{task.filename}（操作者：{operator or self._config.operator_name}）")
            self._save()
            self._notify()
            return True

    def clear_history(self, operator: Optional[str] = None) -> int:
        with self._lock:
            before = len(self._tasks)
            self._tasks = [
                t for t in self._tasks
                if t.status in (TaskStatus.WAITING, TaskStatus.PRINTING,
                                TaskStatus.FAILED, TaskStatus.MANUAL)
            ]
            removed = before - len(self._tasks)
            if removed > 0:
                self._log(f"清空历史记录：删除{removed}条已完成/已取消任务（操作者：{operator or self._config.operator_name}）")
                self._save()
                self._notify()
            return removed

    def start_worker(self):
        with self._lock:
            if self.is_worker_running:
                return
            self._worker_stop_event.clear()
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()
            self._log("打印调度器已启动")

    def stop_worker(self):
        with self._lock:
            if not self.is_worker_running:
                return
            self._worker_stop_event.set()
            self._printer.cancel_current()
            if self._worker_thread:
                self._worker_thread.join(timeout=5.0)
            self._worker_thread = None
            self._log("打印调度器已停止")

    def _worker_loop(self):
        while not self._worker_stop_event.is_set():
            try:
                self._worker_tick()
            except Exception as e:
                self._log(f"调度器异常: {e}")
            self._worker_stop_event.wait(0.3)

    def _worker_tick(self):
        with self._lock:
            if self._global_paused:
                return
            if self._printer.is_busy:
                return
            task = self._find_next_printable_task()
            if task is None:
                return

            task.status = TaskStatus.PRINTING
            task.started_at = time.time()
            task.touch()
            self._mark_dirty()
            self._save()
            self._notify()
            task_id = task.id
            self._log(f"开始打印：{task.filename}（{task.counter.value}，{task.copies}份）")

        def on_progress(p: int):
            pass

        result = self._printer.execute_print(
            task,
            on_progress=on_progress,
            check_cancelled=lambda: self._check_task_cancelled(task_id),
        )

        with self._lock:
            current = self._find_task(task_id)
            if not current:
                return

            if result.success and current.status != TaskStatus.CANCELLED:
                current.status = TaskStatus.COMPLETED
                current.completed_at = time.time()
                current.fail_reason = None
                current.operator = self._config.operator_name
                current.touch()
                self._log(f"打印完成：{current.filename}")
            elif not result.success and result.fail_reason == "用户取消打印":
                current.status = TaskStatus.CANCELLED
                current.fail_reason = result.fail_reason
                current.operator = self._config.operator_name
                current.touch()
                self._log(f"打印已取消：{current.filename}")
            elif not result.success:
                current.retry_count += 1
                current.fail_reason = result.fail_reason
                if current.max_retries <= 0 or current.retry_count >= current.max_retries:
                    current.status = TaskStatus.MANUAL
                    current.operator = self._config.operator_name
                    current.touch()
                    self._log(f"超过重试次数({current.max_retries})，需人工处理：{current.filename} 原因：{result.fail_reason}")
                else:
                    current.status = TaskStatus.FAILED
                    current.operator = self._config.operator_name
                    current.touch()
                    self._log(f"打印失败（第{current.retry_count}次）：{current.filename} 原因：{result.fail_reason}")
            current.started_at = None
            self._save()
            self._notify()

    def _check_task_cancelled(self, task_id: str) -> bool:
        with self._lock:
            task = self._find_task(task_id)
            if not task:
                return True
            return task.status == TaskStatus.CANCELLED

    def _find_task(self, task_id: str) -> Optional[PrintTask]:
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None

    def _find_next_printable_task(self) -> Optional[PrintTask]:
        self._sort_tasks()
        for t in self._tasks:
            if t.paused:
                continue
            if t.status == TaskStatus.WAITING:
                return t
            if t.status == TaskStatus.FAILED:
                if t.max_retries <= 0 or t.retry_count < t.max_retries:
                    return t
        return None

    def _sort_tasks(self):
        priorities = self._config.counter_priorities

        def sort_key(t: PrintTask):
            if t.priority_override is not None:
                prio = t.priority_override
            else:
                prio = priorities.get(t.counter.value, 999)
            active_states = {TaskStatus.WAITING, TaskStatus.PRINTING,
                             TaskStatus.FAILED, TaskStatus.MANUAL}
            state_order = 0 if t.status in active_states else 1
            return (state_order, prio, t.created_at, t.id)

        self._tasks.sort(key=sort_key)

    def get_tasks_by_status(self, status: TaskStatus) -> List[PrintTask]:
        with self._lock:
            return [t for t in self._tasks if t.status == status]

    def get_statistics(self) -> dict:
        with self._lock:
            stats = {s.value: 0 for s in TaskStatus}
            paused = 0
            for t in self._tasks:
                stats[t.status.value] += 1
                if t.paused:
                    paused += 1
            stats["总暂停数"] = paused
            stats["总任务数"] = len(self._tasks)
            return stats

    def _mark_dirty(self):
        self._dirty = True

    def _save(self):
        if self._dirty:
            self._storage.save_tasks(self._tasks)
            self._dirty = False

    def _notify(self):
        if self._on_tasks_changed:
            try:
                self._on_tasks_changed()
            except Exception:
                pass

    def _log(self, msg: str):
        if self._on_log:
            try:
                timestamp = time.strftime("%H:%M:%S", time.localtime())
                self._on_log(f"[{timestamp}] {msg}")
            except Exception:
                pass
