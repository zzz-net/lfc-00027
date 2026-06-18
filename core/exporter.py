import csv
import json
import time
import os
from pathlib import Path
from typing import List, Optional, Set
from dataclasses import dataclass

from .models import PrintTask, TaskStatus
from .storage import Storage


@dataclass
class ExportResult:
    success: bool
    file_path: str
    count: int
    message: str


class HistoryExporter:
    TERMINAL_STATUSES: Set[TaskStatus] = {TaskStatus.COMPLETED, TaskStatus.CANCELLED}

    def __init__(self, storage: Storage):
        self._storage = storage

    @classmethod
    def _fmt_time(cls, ts: Optional[float]) -> str:
        if not ts:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    @classmethod
    def _task_to_row(cls, t: PrintTask) -> dict:
        return {
            "任务ID": t.id,
            "文件名": t.filename,
            "份数": t.copies,
            "所属柜台": t.counter.value,
            "状态": t.status.value,
            "重试次数": t.retry_count,
            "最大重试": t.max_retries,
            "暂停": "是" if t.paused else "否",
            "失败原因": t.fail_reason or "",
            "操作者": t.operator or "",
            "创建时间": cls._fmt_time(t.created_at),
            "开始时间": cls._fmt_time(t.started_at),
            "完成时间": cls._fmt_time(t.completed_at),
            "更新时间": cls._fmt_time(t.updated_at),
            "自定义优先级": t.priority_override or "",
        }

    def export_all(self, tasks: List[PrintTask], output_dir: str,
                   operator: str = "系统", fmt: str = "csv") -> ExportResult:
        return self._export(tasks, output_dir, operator, fmt, "全部任务", all_tasks=True)

    def export_history(self, tasks: List[PrintTask], output_dir: str,
                       operator: str = "系统", fmt: str = "csv") -> ExportResult:
        history = [t for t in tasks if t.status in self.TERMINAL_STATUSES]
        if not history:
            return ExportResult(success=False, file_path="", count=0,
                                message="没有已完成或已取消的历史任务可导出")
        return self._export(history, output_dir, operator, fmt, "历史记录")

    def export_failed_manual(self, tasks: List[PrintTask], output_dir: str,
                             operator: str = "系统", fmt: str = "csv") -> ExportResult:
        fm = [t for t in tasks if t.status in (TaskStatus.FAILED, TaskStatus.MANUAL)]
        if not fm:
            return ExportResult(success=False, file_path="", count=0,
                                message="没有失败或需人工处理的任务可导出")
        return self._export(fm, output_dir, operator, fmt, "异常任务")

    def _export(self, tasks: List[PrintTask], output_dir: str, operator: str,
                fmt: str, tag: str, all_tasks: bool = False) -> ExportResult:
        try:
            out_path = Path(output_dir)
            out_path.mkdir(parents=True, exist_ok=True)

            timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
            ext = fmt.lower()
            if ext not in ("csv", "json"):
                ext = "csv"
            filename = f"print_{tag}_{timestamp}.{ext}"
            full_path = out_path / filename

            rows = [self._task_to_row(t) for t in tasks]

            if ext == "csv":
                with open(full_path, "w", encoding="utf-8-sig", newline="") as f:
                    if rows:
                        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                        writer.writeheader()
                        writer.writerows(rows)
                    else:
                        f.write("")
            else:
                with open(full_path, "w", encoding="utf-8") as f:
                    json.dump(rows, f, ensure_ascii=False, indent=2)

            count = len(tasks)
            record = {
                "file": str(full_path),
                "filename": filename,
                "tag": tag,
                "format": ext,
                "count": count,
                "operator": operator,
            }
            self._storage.log_export(record)

            msg = f"成功导出{count}条{tag}到: {full_path}"
            return ExportResult(success=True, file_path=str(full_path), count=count, message=msg)

        except PermissionError as e:
            return ExportResult(success=False, file_path="", count=0,
                                message=f"导出失败：权限不足 {e}")
        except OSError as e:
            return ExportResult(success=False, file_path="", count=0,
                                message=f"导出失败：IO错误 {e}")
        except Exception as e:
            return ExportResult(success=False, file_path="", count=0,
                                message=f"导出失败：{type(e).__name__}: {e}")
