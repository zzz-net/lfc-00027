import json
import os
import time
import threading
from enum import Enum
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Set, Tuple

from .models import PrintTask, TaskStatus, CounterType
from .importer import TaskImporter, ImportResult
from .storage import Storage, DATA_DIR


PREVIEWS_FILE = DATA_DIR / "preflight_summaries.json"


class PreflightCategory(str, Enum):
    SUCCESS = "success"
    AUTO_FIXABLE = "auto_fixable"
    DUPLICATE_CONFLICT = "duplicate_conflict"
    UNIMPORTABLE = "unimportable"


class ConflictType(str, Enum):
    DUPLICATE_FILENAME = "duplicate_filename"
    COUNTER_PRIORITY_CONFLICT = "counter_priority_conflict"
    BOTH = "both"


class ConflictResolution(str, Enum):
    SKIP = "skip"
    OVERRIDE_PRIORITY = "override_priority"
    KEEP_BOTH = "keep_both"


CATEGORY_LABELS = {
    PreflightCategory.SUCCESS: "成功",
    PreflightCategory.AUTO_FIXABLE: "可自动修正",
    PreflightCategory.DUPLICATE_CONFLICT: "重复/冲突",
    PreflightCategory.UNIMPORTABLE: "无法导入",
}

RESOLUTION_LABELS = {
    ConflictResolution.SKIP: "跳过",
    ConflictResolution.OVERRIDE_PRIORITY: "覆盖优先级",
    ConflictResolution.KEEP_BOTH: "保留两条",
}


@dataclass
class ConflictInfo:
    conflict_type: ConflictType
    existing_task_id: Optional[str] = None
    existing_task_filename: Optional[str] = None
    existing_task_counter: Optional[str] = None
    existing_task_priority: Optional[int] = None
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ConflictInfo":
        return cls(
            conflict_type=ConflictType(d["conflict_type"]),
            existing_task_id=d.get("existing_task_id"),
            existing_task_filename=d.get("existing_task_filename"),
            existing_task_counter=d.get("existing_task_counter"),
            existing_task_priority=d.get("existing_task_priority"),
            message=d.get("message", ""),
        )


@dataclass
class PreflightItem:
    item_index: int
    category: PreflightCategory
    task: Optional[PrintTask] = None
    source_row: Optional[int] = None
    source_identifier: str = ""
    error_message: str = ""
    skipped_warnings: List[str] = field(default_factory=list)
    conflict_info: Optional[ConflictInfo] = None
    resolution: ConflictResolution = ConflictResolution.KEEP_BOTH
    selected: bool = True
    override_priority_value: Optional[int] = None

    def to_dict(self) -> dict:
        d = {
            "item_index": self.item_index,
            "category": self.category.value,
            "task": self.task.to_dict() if self.task else None,
            "source_row": self.source_row,
            "source_identifier": self.source_identifier,
            "error_message": self.error_message,
            "skipped_warnings": self.skipped_warnings,
            "conflict_info": self.conflict_info.to_dict() if self.conflict_info else None,
            "resolution": self.resolution.value,
            "selected": self.selected,
            "override_priority_value": self.override_priority_value,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PreflightItem":
        return cls(
            item_index=d["item_index"],
            category=PreflightCategory(d["category"]),
            task=PrintTask.from_dict(d["task"]) if d.get("task") else None,
            source_row=d.get("source_row"),
            source_identifier=d.get("source_identifier", ""),
            error_message=d.get("error_message", ""),
            skipped_warnings=d.get("skipped_warnings", []),
            conflict_info=ConflictInfo.from_dict(d["conflict_info"]) if d.get("conflict_info") else None,
            resolution=ConflictResolution(d.get("resolution", ConflictResolution.KEEP_BOTH.value)),
            selected=d.get("selected", True),
            override_priority_value=d.get("override_priority_value"),
        )


@dataclass
class PreflightSummary:
    preview_id: str
    created_at: float
    source_file: str
    source_format: str
    total_count: int
    category_counts: Dict[str, int]
    operator: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PreflightSummary":
        return cls(
            preview_id=d["preview_id"],
            created_at=d["created_at"],
            source_file=d["source_file"],
            source_format=d["source_format"],
            total_count=d["total_count"],
            category_counts=d.get("category_counts", {}),
            operator=d.get("operator"),
        )


@dataclass
class PreflightResult:
    preview_id: str
    created_at: float
    source_file: str
    source_format: str
    items: List[PreflightItem]
    operator: Optional[str] = None

    def groups(self) -> Dict[PreflightCategory, List[PreflightItem]]:
        result = {c: [] for c in PreflightCategory}
        for item in self.items:
            result[item.category].append(item)
        return result

    def category_counts(self) -> Dict[str, int]:
        g = self.groups()
        return {c.value: len(g[c]) for c in PreflightCategory}

    def summary(self) -> PreflightSummary:
        return PreflightSummary(
            preview_id=self.preview_id,
            created_at=self.created_at,
            source_file=self.source_file,
            source_format=self.source_format,
            total_count=len(self.items),
            category_counts=self.category_counts(),
            operator=self.operator,
        )

    def to_dict(self) -> dict:
        return {
            "preview_id": self.preview_id,
            "created_at": self.created_at,
            "source_file": self.source_file,
            "source_format": self.source_format,
            "items": [it.to_dict() for it in self.items],
            "operator": self.operator,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PreflightResult":
        return cls(
            preview_id=d["preview_id"],
            created_at=d["created_at"],
            source_file=d["source_file"],
            source_format=d["source_format"],
            items=[PreflightItem.from_dict(it) for it in d.get("items", [])],
            operator=d.get("operator"),
        )


ACTIVE_TASK_STATUSES = {
    TaskStatus.WAITING,
    TaskStatus.PRINTING,
    TaskStatus.FAILED,
    TaskStatus.MANUAL,
}


class PreflightChecker:

    def __init__(self, storage: Optional[Storage] = None):
        self._storage = storage or Storage()
        self._lock = threading.RLock()

    def run_preview(self, file_path: str,
                    existing_tasks: Optional[List[PrintTask]] = None,
                    default_max_retries: int = 3,
                    operator: Optional[str] = None) -> PreflightResult:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        fmt = path.suffix.lower().lstrip(".")
        if fmt == "csv":
            source_format = "csv"
        elif fmt == "json":
            source_format = "json"
        else:
            raise ValueError(f"不支持的文件格式: {fmt}")

        if existing_tasks is None:
            existing_tasks = self._storage.load_tasks()

        import_result: ImportResult = TaskImporter.import_file(
            str(path), default_max_retries=default_max_retries
        )

        return self._build_preview(
            source_file=str(path),
            source_format=source_format,
            import_result=import_result,
            existing_tasks=existing_tasks,
            operator=operator,
        )

    def _build_preview(self, source_file: str, source_format: str,
                       import_result: ImportResult,
                       existing_tasks: List[PrintTask],
                       operator: Optional[str] = None) -> PreflightResult:
        preview_id = f"pv_{int(time.time() * 1000)}_{os.urandom(2).hex()}"
        created_at = time.time()

        skipped_map: Dict[int, List[str]] = {}
        for row_num, identifier, msg in import_result.skipped:
            skipped_map.setdefault(row_num, []).append(msg)

        counter_priorities = self._storage.load_config().counter_priorities

        error_indexes: Set[int] = set()
        error_by_row: Dict[int, Tuple[str, str]] = {}
        for row_num, identifier, msg in import_result.errors:
            error_indexes.add(row_num)
            error_by_row[row_num] = (identifier, msg)

        success_by_row: Dict[int, Tuple[PrintTask, str]] = {}
        for idx, task in enumerate(import_result.success):
            row = getattr(task, "_row_num", None)
            if row is None:
                row = idx + 1
            identifier = f"{task.filename} | {task.copies} | {task.counter.value}"
            success_by_row[row] = (task, identifier)

        all_row_nums = sorted(set(list(skipped_map.keys()) + list(error_indexes) + list(success_by_row.keys())))
        if not all_row_nums:
            max_row = 0
        else:
            max_row = max(all_row_nums)
            for t in import_result.success:
                max_row = max(max_row, len(import_result.success))

        items: List[PreflightItem] = []
        item_index = 0

        task_list = import_result.success
        for row_idx, task in enumerate(task_list):
            row_num = row_idx + 1
            identifier = f"{task.filename} | {task.copies} | {task.counter.value}"
            warnings = skipped_map.get(row_num, [])

            conflict_info = self._detect_conflict(task, existing_tasks, counter_priorities)

            if conflict_info is not None:
                category = PreflightCategory.DUPLICATE_CONFLICT
            elif warnings:
                category = PreflightCategory.AUTO_FIXABLE
            else:
                category = PreflightCategory.SUCCESS

            default_resolution = (
                ConflictResolution.SKIP
                if conflict_info is not None and conflict_info.conflict_type == ConflictType.DUPLICATE_FILENAME
                else ConflictResolution.KEEP_BOTH
            )

            items.append(PreflightItem(
                item_index=item_index,
                category=category,
                task=task,
                source_row=row_num,
                source_identifier=identifier,
                skipped_warnings=warnings,
                conflict_info=conflict_info,
                resolution=default_resolution,
                selected=category != PreflightCategory.UNIMPORTABLE,
            ))
            item_index += 1

        for row_num, (identifier, msg) in error_by_row.items():
            items.append(PreflightItem(
                item_index=item_index,
                category=PreflightCategory.UNIMPORTABLE,
                task=None,
                source_row=row_num,
                source_identifier=identifier,
                error_message=msg,
                selected=False,
            ))
            item_index += 1

        items.sort(key=lambda it: (it.source_row or 99999, it.item_index))
        for i, it in enumerate(items):
            it.item_index = i

        result = PreflightResult(
            preview_id=preview_id,
            created_at=created_at,
            source_file=source_file,
            source_format=source_format,
            items=items,
            operator=operator,
        )

        self._save_summary(result.summary())

        return result

    def _detect_conflict(self, task: PrintTask, existing_tasks: List[PrintTask],
                         counter_priorities: Dict[str, int]) -> Optional[ConflictInfo]:
        active_existing = [t for t in existing_tasks if t.status in ACTIVE_TASK_STATUSES]

        dup_filename_task = None
        for t in active_existing:
            if t.filename == task.filename:
                dup_filename_task = t
                break

        same_counter_active = [t for t in active_existing if t.counter == task.counter]

        def get_prio(t: PrintTask) -> int:
            if t.priority_override is not None:
                return t.priority_override
            return counter_priorities.get(t.counter.value, 999)

        task_prio = get_prio(task)
        same_prio_counter_task = None
        for t in same_counter_active:
            if get_prio(t) == task_prio:
                same_prio_counter_task = t
                break

        if dup_filename_task is not None and same_prio_counter_task is not None:
            if dup_filename_task.id == same_prio_counter_task.id:
                return ConflictInfo(
                    conflict_type=ConflictType.BOTH,
                    existing_task_id=dup_filename_task.id,
                    existing_task_filename=dup_filename_task.filename,
                    existing_task_counter=dup_filename_task.counter.value,
                    existing_task_priority=get_prio(dup_filename_task),
                    message=f"同文件({task.filename})已存在且同柜台({task.counter.value})同优先级({task_prio})",
                )
            else:
                return ConflictInfo(
                    conflict_type=ConflictType.BOTH,
                    existing_task_id=dup_filename_task.id,
                    existing_task_filename=dup_filename_task.filename,
                    existing_task_counter=dup_filename_task.counter.value,
                    existing_task_priority=get_prio(dup_filename_task),
                    message=f"文件名重复({task.filename})且同柜台存在同优先级任务",
                )
        elif dup_filename_task is not None:
            return ConflictInfo(
                conflict_type=ConflictType.DUPLICATE_FILENAME,
                existing_task_id=dup_filename_task.id,
                existing_task_filename=dup_filename_task.filename,
                existing_task_counter=dup_filename_task.counter.value,
                existing_task_priority=get_prio(dup_filename_task),
                message=f"已存在同名未完成文件: {task.filename}",
            )
        elif same_prio_counter_task is not None:
            return ConflictInfo(
                conflict_type=ConflictType.COUNTER_PRIORITY_CONFLICT,
                existing_task_id=same_prio_counter_task.id,
                existing_task_filename=same_prio_counter_task.filename,
                existing_task_counter=same_prio_counter_task.counter.value,
                existing_task_priority=task_prio,
                message=f"同柜台({task.counter.value})已存在同优先级({task_prio})任务: {same_prio_counter_task.filename}",
            )

        return None

    # ---------------- 应用选择（入队） ----------------

    def apply_preview(self, preview_result: PreflightResult, queue_manager,
                      operator: Optional[str] = None) -> Dict[str, Any]:
        added: List[PrintTask] = []
        skipped: List[PreflightItem] = []
        failed: List[PreflightItem] = []
        log_entries: List[Dict[str, Any]] = []

        for item in preview_result.items:
            if not item.selected:
                skipped.append(item)
                log_entries.append({
                    "item_index": item.item_index,
                    "action": "deselected",
                    "source_row": item.source_row,
                    "identifier": item.source_identifier,
                    "reason": "用户未勾选",
                })
                continue

            if item.category == PreflightCategory.UNIMPORTABLE:
                failed.append(item)
                log_entries.append({
                    "item_index": item.item_index,
                    "action": "rejected",
                    "source_row": item.source_row,
                    "identifier": item.source_identifier,
                    "reason": item.error_message,
                })
                continue

            if item.category == PreflightCategory.DUPLICATE_CONFLICT:
                action, task_to_add = self._resolve_conflict(item)
                if action == "skip":
                    skipped.append(item)
                    log_entries.append({
                        "item_index": item.item_index,
                        "action": "skipped_conflict",
                        "resolution": ConflictResolution.SKIP.value,
                        "source_row": item.source_row,
                        "identifier": item.source_identifier,
                        "conflict": item.conflict_info.message if item.conflict_info else "",
                    })
                    continue
                elif action == "override" and task_to_add is not None:
                    queue_manager.add_tasks([task_to_add])
                    added.append(task_to_add)
                    log_entries.append({
                        "item_index": item.item_index,
                        "action": "added_override",
                        "resolution": ConflictResolution.OVERRIDE_PRIORITY.value,
                        "source_row": item.source_row,
                        "identifier": item.source_identifier,
                        "new_priority": task_to_add.priority_override,
                    })
                    continue
                else:
                    pass

            if item.task is None:
                failed.append(item)
                continue

            queue_manager.add_tasks([item.task])
            added.append(item.task)

            if item.category == PreflightCategory.AUTO_FIXABLE:
                log_entries.append({
                    "item_index": item.item_index,
                    "action": "added_with_fix",
                    "source_row": item.source_row,
                    "identifier": item.source_identifier,
                    "warnings": item.skipped_warnings,
                })
            else:
                log_entries.append({
                    "item_index": item.item_index,
                    "action": "added",
                    "source_row": item.source_row,
                    "identifier": item.source_identifier,
                })

        apply_record = {
            "preview_id": preview_result.preview_id,
            "applied_at": time.time(),
            "operator": operator or preview_result.operator or "系统",
            "added_count": len(added),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "added_ids": [t.id for t in added],
            "log_entries": log_entries,
        }

        self._save_apply_record(apply_record)

        return apply_record

    def _resolve_conflict(self, item: PreflightItem) -> Tuple[str, Optional[PrintTask]]:
        if item.task is None:
            return "skip", None

        resolution = item.resolution
        if resolution == ConflictResolution.SKIP:
            return "skip", None
        elif resolution == ConflictResolution.OVERRIDE_PRIORITY:
            new_task = PrintTask.from_dict(item.task.to_dict())
            if item.override_priority_value is not None:
                new_task.priority_override = item.override_priority_value
            else:
                if new_task.priority_override is not None:
                    new_prio = max(1, new_task.priority_override - 1)
                else:
                    new_prio = 1
                new_task.priority_override = new_prio
            return "override", new_task
        else:
            return "keep", item.task

    # ---------------- 持久化 ----------------

    def _save_summary(self, summary: PreflightSummary) -> None:
        with self._lock:
            summaries: List[dict] = []
            if PREVIEWS_FILE.exists():
                try:
                    with open(PREVIEWS_FILE, "r", encoding="utf-8") as f:
                        summaries = json.load(f)
                except (json.JSONDecodeError, ValueError):
                    summaries = []
            summaries.append(summary.to_dict())
            summaries = summaries[-20:]
            tmp = PREVIEWS_FILE.with_suffix(".json.tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(summaries, f, ensure_ascii=False, indent=2)
            os.replace(tmp, PREVIEWS_FILE)

    def load_last_summary(self) -> Optional[PreflightSummary]:
        with self._lock:
            if not PREVIEWS_FILE.exists():
                return None
            try:
                with open(PREVIEWS_FILE, "r", encoding="utf-8") as f:
                    summaries = json.load(f)
                if not summaries:
                    return None
                return PreflightSummary.from_dict(summaries[-1])
            except (json.JSONDecodeError, ValueError, KeyError):
                return None

    def _save_apply_record(self, record: dict) -> None:
        apply_file = PREVIEWS_FILE.parent / "preflight_apply_logs.json"
        with self._lock:
            logs: List[dict] = []
            if apply_file.exists():
                try:
                    with open(apply_file, "r", encoding="utf-8") as f:
                        logs = json.load(f)
                except (json.JSONDecodeError, ValueError):
                    logs = []
            logs.append(record)
            logs = logs[-50:]
            tmp = apply_file.with_suffix(".json.tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(logs, f, ensure_ascii=False, indent=2)
            os.replace(tmp, apply_file)

    def load_apply_logs(self) -> List[dict]:
        apply_file = PREVIEWS_FILE.parent / "preflight_apply_logs.json"
        with self._lock:
            if not apply_file.exists():
                return []
            try:
                with open(apply_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError):
                return []

    # ---------------- 导出 ----------------

    def export_preview_record(self, preview_result: PreflightResult,
                              output_dir: str,
                              apply_result: Optional[Dict[str, Any]] = None,
                              operator: Optional[str] = None) -> str:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        filename = f"preflight_{preview_result.preview_id}_{timestamp}.json"
        full_path = out_path / filename

        export_data = {
            "export_type": "preflight_record",
            "exported_at": time.time(),
            "exported_by": operator or preview_result.operator or "系统",
            "preview": preview_result.to_dict(),
            "summary": preview_result.summary().to_dict(),
        }
        if apply_result is not None:
            export_data["apply_result"] = apply_result

        with open(full_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)

        return str(full_path)

    def export_apply_logs(self, output_dir: str,
                          operator: Optional[str] = None) -> str:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        filename = f"preflight_apply_logs_{timestamp}.json"
        full_path = out_path / filename

        logs = self.load_apply_logs()
        export_data = {
            "export_type": "preflight_apply_logs",
            "exported_at": time.time(),
            "exported_by": operator or "系统",
            "logs": logs,
        }

        with open(full_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)

        return str(full_path)
