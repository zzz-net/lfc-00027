import json
import os
import csv
import time
import threading
import uuid
from enum import Enum
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

from .models import PrintTask, CounterType
from .importer import TaskImporter, ImportResult
from .storage import Storage, DATA_DIR
from .preflight import (
    PreflightCategory, ConflictType, ConflictResolution,
    ConflictInfo, PreflightItem, ACTIVE_TASK_STATUSES,
)


BATCHES_FILE = DATA_DIR / "playback_batches.json"
LAST_SUBMITTED_FILE = DATA_DIR / "playback_last_submitted.json"
UI_STATE_FILE = DATA_DIR / "playback_ui_state.json"


class BatchStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    CANCELLED = "cancelled"


class PlaybackGroup(str, Enum):
    SUCCESS = "success"
    AUTO_FIXABLE = "auto_fixable"
    DUPLICATE_CONFLICT = "duplicate_conflict"
    UNIMPORTABLE = "unimportable"


GROUP_LABELS = {
    PlaybackGroup.SUCCESS: "正常导入",
    PlaybackGroup.AUTO_FIXABLE: "默认值兜底",
    PlaybackGroup.DUPLICATE_CONFLICT: "冲突待决",
    PlaybackGroup.UNIMPORTABLE: "无法导入",
}

RESOLUTION_LABELS = {
    ConflictResolution.SKIP: "跳过",
    ConflictResolution.KEEP_BOTH: "保留两条",
    ConflictResolution.OVERRIDE_PRIORITY: "覆盖优先级",
}


@dataclass
class RawFieldSnapshot:
    filename: str = ""
    copies: str = ""
    counter: str = ""
    max_retries: str = ""
    priority: str = ""
    extra: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RawFieldSnapshot":
        return cls(
            filename=d.get("filename", ""),
            copies=d.get("copies", ""),
            counter=d.get("counter", ""),
            max_retries=d.get("max_retries", ""),
            priority=d.get("priority", ""),
            extra=d.get("extra", {}),
        )


@dataclass
class TimelineEntry:
    timestamp: float
    action: str
    detail: str = ""
    operator: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TimelineEntry":
        return cls(
            timestamp=d["timestamp"],
            action=d["action"],
            detail=d.get("detail", ""),
            operator=d.get("operator"),
        )


@dataclass
class ConflictCandidate:
    task_id: str
    filename: str
    counter: str
    priority: int
    status: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ConflictCandidate":
        return cls(
            task_id=d["task_id"],
            filename=d["filename"],
            counter=d["counter"],
            priority=d["priority"],
            status=d["status"],
        )


@dataclass
class PlaybackItem:
    item_index: int
    group: PlaybackGroup
    source_row: Optional[int] = None
    source_identifier: str = ""
    raw_fields: RawFieldSnapshot = field(default_factory=RawFieldSnapshot)
    parsed_task: Optional[PrintTask] = None
    error_message: str = ""
    fallback_reasons: List[str] = field(default_factory=list)
    conflict_candidates: List[ConflictCandidate] = field(default_factory=list)
    conflict_type: Optional[ConflictType] = None
    conflict_message: str = ""
    resolution: ConflictResolution = ConflictResolution.KEEP_BOTH
    override_priority_value: Optional[int] = None
    selected: bool = True
    submit_result: Optional[str] = None
    submitted_task_id: Optional[str] = None
    timeline: List[TimelineEntry] = field(default_factory=list)

    def add_timeline(self, action: str, detail: str = "", operator: Optional[str] = None):
        self.timeline.append(TimelineEntry(
            timestamp=time.time(),
            action=action,
            detail=detail,
            operator=operator,
        ))

    def to_dict(self) -> dict:
        return {
            "item_index": self.item_index,
            "group": self.group.value,
            "source_row": self.source_row,
            "source_identifier": self.source_identifier,
            "raw_fields": self.raw_fields.to_dict(),
            "parsed_task": self.parsed_task.to_dict() if self.parsed_task else None,
            "error_message": self.error_message,
            "fallback_reasons": self.fallback_reasons,
            "conflict_candidates": [c.to_dict() for c in self.conflict_candidates],
            "conflict_type": self.conflict_type.value if self.conflict_type else None,
            "conflict_message": self.conflict_message,
            "resolution": self.resolution.value,
            "override_priority_value": self.override_priority_value,
            "selected": self.selected,
            "submit_result": self.submit_result,
            "submitted_task_id": self.submitted_task_id,
            "timeline": [t.to_dict() for t in self.timeline],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlaybackItem":
        return cls(
            item_index=d["item_index"],
            group=PlaybackGroup(d["group"]),
            source_row=d.get("source_row"),
            source_identifier=d.get("source_identifier", ""),
            raw_fields=RawFieldSnapshot.from_dict(d.get("raw_fields", {})),
            parsed_task=PrintTask.from_dict(d["parsed_task"]) if d.get("parsed_task") else None,
            error_message=d.get("error_message", ""),
            fallback_reasons=d.get("fallback_reasons", []),
            conflict_candidates=[ConflictCandidate.from_dict(c) for c in d.get("conflict_candidates", [])],
            conflict_type=ConflictType(d["conflict_type"]) if d.get("conflict_type") else None,
            conflict_message=d.get("conflict_message", ""),
            resolution=ConflictResolution(d.get("resolution", ConflictResolution.KEEP_BOTH.value)),
            override_priority_value=d.get("override_priority_value"),
            selected=d.get("selected", True),
            submit_result=d.get("submit_result"),
            submitted_task_id=d.get("submitted_task_id"),
            timeline=[TimelineEntry.from_dict(t) for t in d.get("timeline", [])],
        )


@dataclass
class ImportBatch:
    batch_id: str
    created_at: float
    source_file: str
    source_format: str
    status: BatchStatus
    items: List[PlaybackItem]
    operator: Optional[str] = None
    submitted_at: Optional[float] = None
    submit_summary: Optional[Dict[str, Any]] = None
    timeline: List[TimelineEntry] = field(default_factory=list)

    def add_timeline(self, action: str, detail: str = "", operator: Optional[str] = None):
        self.timeline.append(TimelineEntry(
            timestamp=time.time(),
            action=action,
            detail=detail,
            operator=operator,
        ))

    def groups(self) -> Dict[PlaybackGroup, List[PlaybackItem]]:
        result = {g: [] for g in PlaybackGroup}
        for item in self.items:
            result[item.group].append(item)
        return result

    def group_counts(self) -> Dict[str, int]:
        g = self.groups()
        return {k.value: len(v) for k, v in g.items()}

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "source_file": self.source_file,
            "source_format": self.source_format,
            "status": self.status.value,
            "items": [it.to_dict() for it in self.items],
            "operator": self.operator,
            "submitted_at": self.submitted_at,
            "submit_summary": self.submit_summary,
            "timeline": [t.to_dict() for t in self.timeline],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ImportBatch":
        return cls(
            batch_id=d["batch_id"],
            created_at=d["created_at"],
            source_file=d["source_file"],
            source_format=d["source_format"],
            status=BatchStatus(d["status"]),
            items=[PlaybackItem.from_dict(it) for it in d.get("items", [])],
            operator=d.get("operator"),
            submitted_at=d.get("submitted_at"),
            submit_summary=d.get("submit_summary"),
            timeline=[TimelineEntry.from_dict(t) for t in d.get("timeline", [])],
        )


@dataclass
class PlaybackUIState:
    current_batch_id: Optional[str] = None
    selected_group: Optional[str] = None
    expanded_items: List[int] = field(default_factory=list)
    filter_text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PlaybackUIState":
        return cls(
            current_batch_id=d.get("current_batch_id"),
            selected_group=d.get("selected_group"),
            expanded_items=d.get("expanded_items", []),
            filter_text=d.get("filter_text", ""),
        )


class BatchPlaybackManager:
    def __init__(self, storage: Optional[Storage] = None):
        self._storage = storage or Storage()
        self._lock = threading.RLock()

    def create_batch(self, file_path: str,
                     existing_tasks: Optional[List[PrintTask]] = None,
                     default_max_retries: int = 3,
                     operator: Optional[str] = None) -> ImportBatch:
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

        parsed_rows = self._parse_all_rows(str(path), source_format, default_max_retries)

        batch = self._build_batch(
            source_file=str(path),
            source_format=source_format,
            parsed_rows=parsed_rows,
            existing_tasks=existing_tasks,
            default_max_retries=default_max_retries,
            operator=operator,
        )

        self._save_pending_batch(batch)
        self._save_ui_state(PlaybackUIState(current_batch_id=batch.batch_id))

        return batch

    def _parse_all_rows(self, file_path: str, source_format: str,
                        default_max_retries: int) -> List[Dict[str, Any]]:
        result = []
        path = Path(file_path)

        if source_format == "csv":
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames or []
                col_map = {fn.lower().strip(): fn for fn in fieldnames}

                for row_num, row in enumerate(reader, start=2):
                    raw = RawFieldSnapshot()
                    for k, v in row.items():
                        key_lower = k.lower().strip()
                        val = (v or "").strip()
                        if key_lower == "filename":
                            raw.filename = val
                        elif key_lower == "copies":
                            raw.copies = val
                        elif key_lower == "counter":
                            raw.counter = val
                        elif key_lower == "max_retries":
                            raw.max_retries = val
                        elif key_lower == "priority":
                            raw.priority = val
                        else:
                            raw.extra[k] = val

                    row_data = self._validate_and_parse_row(
                        raw, default_max_retries
                    )
                    row_data["row_num"] = row_num
                    row_data["raw_fields"] = raw
                    result.append(row_data)
        else:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for idx, item in enumerate(data, start=1):
                    raw = RawFieldSnapshot()
                    if isinstance(item, dict):
                        for k, v in item.items():
                            val_str = str(v) if v is not None else ""
                            if k == "filename":
                                raw.filename = val_str
                            elif k == "copies":
                                raw.copies = val_str
                            elif k == "counter":
                                raw.counter = val_str
                            elif k == "max_retries":
                                raw.max_retries = val_str
                            elif k == "priority":
                                raw.priority = val_str
                            else:
                                raw.extra[k] = val_str

                    row_data = self._validate_and_parse_row(
                        raw, default_max_retries
                    )
                    row_data["row_num"] = idx
                    row_data["raw_fields"] = raw
                    result.append(row_data)

        return result

    def _validate_and_parse_row(self, raw: RawFieldSnapshot,
                                default_max_retries: int) -> Dict[str, Any]:
        filename = raw.filename
        copies_str = raw.copies
        counter_str = raw.counter
        max_retries_str = raw.max_retries
        priority_str = raw.priority

        row_identifier = (
            f"{filename or '空文件名'} | "
            f"{copies_str or '空份数'} | "
            f"{counter_str or '空柜台'}"
        )

        fallback_reasons: List[str] = []
        error_message: Optional[str] = None
        task: Optional[PrintTask] = None

        validation_error = TaskImporter._validate_fields(filename, copies_str, counter_str)
        if validation_error:
            error_message = validation_error
        else:
            try:
                copies = int(copies_str)
            except ValueError:
                error_message = f"份数格式错误: '{copies_str}' 不是有效整数"
            else:
                copies_error = TaskImporter._validate_copies(copies)
                if copies_error:
                    error_message = copies_error
                else:
                    counter = TaskImporter._parse_counter(counter_str)
                    if counter is None:
                        error_message = f"未知柜台类型: '{counter_str}'"
                    else:
                        max_retries = default_max_retries
                        if max_retries_str:
                            try:
                                mr = int(max_retries_str)
                                if 0 <= mr <= 20:
                                    max_retries = mr
                                else:
                                    fallback_reasons.append(
                                        f"重试次数{mr}超出范围(0-20)，使用默认值{default_max_retries}"
                                    )
                            except ValueError:
                                fallback_reasons.append(
                                    f"重试次数字段格式错误，使用默认值{default_max_retries}"
                                )

                        priority_override = None
                        if priority_str:
                            try:
                                p = int(priority_str)
                                if 1 <= p <= 999:
                                    priority_override = p
                                else:
                                    fallback_reasons.append(
                                        f"优先级{p}超出范围(1-999)，忽略自定义优先级"
                                    )
                            except ValueError:
                                fallback_reasons.append(
                                    "优先级字段格式错误，忽略自定义优先级"
                                )

                        task = PrintTask.create(
                            filename=filename,
                            copies=copies,
                            counter=counter,
                            max_retries=max_retries,
                            priority_override=priority_override,
                        )

        return {
            "identifier": row_identifier,
            "task": task,
            "fallback_reasons": fallback_reasons,
            "error_message": error_message,
        }

    def _build_batch(self, source_file: str, source_format: str,
                     parsed_rows: List[Dict[str, Any]],
                     existing_tasks: List[PrintTask],
                     default_max_retries: int,
                     operator: Optional[str] = None) -> ImportBatch:
        batch_id = f"batch_{int(time.time() * 1000)}_{os.urandom(2).hex()}"
        created_at = time.time()

        counter_priorities = self._storage.load_config().counter_priorities

        items: List[PlaybackItem] = []
        item_index = 0

        for row_data in parsed_rows:
            row_num = row_data["row_num"]
            raw_fields = row_data["raw_fields"]
            identifier = row_data["identifier"]
            task = row_data["task"]
            fallback_reasons = row_data["fallback_reasons"]
            error_message = row_data["error_message"]

            conflict_candidates: List[ConflictCandidate] = []
            conflict_type = None
            conflict_message = ""

            if task is not None:
                conflict_info = self._detect_conflict(task, existing_tasks, counter_priorities)

                if conflict_info is not None:
                    conflict_type = conflict_info.conflict_type
                    conflict_message = conflict_info.message
                    if conflict_info.existing_task_id:
                        for et in existing_tasks:
                            if et.id == conflict_info.existing_task_id:
                                prio = et.priority_override if et.priority_override is not None \
                                    else counter_priorities.get(et.counter.value, 999)
                                conflict_candidates.append(ConflictCandidate(
                                    task_id=et.id,
                                    filename=et.filename,
                                    counter=et.counter.value,
                                    priority=prio,
                                    status=et.status.value,
                                ))
                                break

            if error_message:
                group = PlaybackGroup.UNIMPORTABLE
                default_resolution = ConflictResolution.SKIP
                selected = False
            elif conflict_type is not None:
                group = PlaybackGroup.DUPLICATE_CONFLICT
                default_resolution = (
                    ConflictResolution.SKIP
                    if conflict_type == ConflictType.DUPLICATE_FILENAME
                    else ConflictResolution.KEEP_BOTH
                )
                selected = True
            elif fallback_reasons:
                group = PlaybackGroup.AUTO_FIXABLE
                default_resolution = ConflictResolution.KEEP_BOTH
                selected = True
            else:
                group = PlaybackGroup.SUCCESS
                default_resolution = ConflictResolution.KEEP_BOTH
                selected = True

            item = PlaybackItem(
                item_index=item_index,
                group=group,
                source_row=row_num,
                source_identifier=identifier,
                raw_fields=raw_fields,
                parsed_task=task,
                fallback_reasons=fallback_reasons,
                conflict_candidates=conflict_candidates,
                conflict_type=conflict_type,
                conflict_message=conflict_message,
                resolution=default_resolution,
                selected=selected,
                error_message=error_message,
            )

            item.add_timeline("解析完成", "", operator)

            if group == PlaybackGroup.UNIMPORTABLE:
                item.add_timeline("解析失败", error_message or "未知错误", operator)
            elif group == PlaybackGroup.DUPLICATE_CONFLICT:
                item.add_timeline("冲突检测", conflict_message, operator)
                item.add_timeline("默认决策", f"{RESOLUTION_LABELS[default_resolution]}", operator)
            elif group == PlaybackGroup.AUTO_FIXABLE:
                reasons_str = "；".join(fallback_reasons)
                item.add_timeline("默认值兜底", reasons_str, operator)

            items.append(item)
            item_index += 1

        items.sort(key=lambda it: it.source_row or 0)
        for i, it in enumerate(items):
            it.item_index = i

        batch = ImportBatch(
            batch_id=batch_id,
            created_at=created_at,
            source_file=source_file,
            source_format=source_format,
            status=BatchStatus.PENDING,
            items=items,
            operator=operator,
        )
        batch.add_timeline("批次创建", f"来源: {Path(source_file).name}", operator)

        return batch

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

    def set_resolution(self, batch: ImportBatch, item_index: int,
                       resolution: ConflictResolution,
                       override_priority: Optional[int] = None,
                       operator: Optional[str] = None) -> bool:
        with self._lock:
            item = None
            for it in batch.items:
                if it.item_index == item_index:
                    item = it
                    break
            if item is None:
                return False
            if item.group != PlaybackGroup.DUPLICATE_CONFLICT:
                return False

            old_res = item.resolution
            item.resolution = resolution
            if override_priority is not None:
                item.override_priority_value = override_priority

            detail = f"决策从 {old_res.value} 改为 {resolution.value}"
            if override_priority is not None:
                detail += f" (覆盖优先级: {override_priority})"
            item.add_timeline("冲突决策", detail, operator)
            batch.add_timeline("条目决策", f"行{item.source_row}: {detail}", operator)

            self._save_pending_batch(batch)
            return True

    def set_selected(self, batch: ImportBatch, item_index: int,
                     selected: bool, operator: Optional[str] = None) -> bool:
        with self._lock:
            item = None
            for it in batch.items:
                if it.item_index == item_index:
                    item = it
                    break
            if item is None:
                return False

            item.selected = selected
            action = "勾选导入" if selected else "取消勾选"
            item.add_timeline(action, "", operator)
            batch.add_timeline("条目操作", f"行{item.source_row}: {action}", operator)
            self._save_pending_batch(batch)
            return True

    def set_group_selected(self, batch: ImportBatch, group: PlaybackGroup,
                           selected: bool, operator: Optional[str] = None) -> int:
        with self._lock:
            count = 0
            action_label = "批量勾选" if selected else "批量取消勾选"
            for item in batch.items:
                if item.group == group:
                    item.selected = selected
                    item.add_timeline(action_label, f"分组: {GROUP_LABELS[group]}", operator)
                    count += 1
            batch.add_timeline(
                "批量操作",
                f"{action_label} - 分组: {GROUP_LABELS[group]}, 数量: {count}",
                operator
            )
            self._save_pending_batch(batch)
            return count

    def undo_last_decision(self, batch: ImportBatch, item_index: int,
                           operator: Optional[str] = None) -> bool:
        with self._lock:
            item = None
            for it in batch.items:
                if it.item_index == item_index:
                    item = it
                    break
            if item is None:
                return False

            if item.group == PlaybackGroup.DUPLICATE_CONFLICT:
                default_res = (
                    ConflictResolution.SKIP
                    if item.conflict_type == ConflictType.DUPLICATE_FILENAME
                    else ConflictResolution.KEEP_BOTH
                )
                item.resolution = default_res
                item.override_priority_value = None
                item.add_timeline("撤销决策", "恢复默认决策", operator)
                batch.add_timeline(
                    "撤销决策",
                    f"行{item.source_row}: 撤销冲突决策，恢复默认",
                    operator
                )
                self._save_pending_batch(batch)
                return True
            return False

    def submit_batch(self, batch: ImportBatch, queue_manager,
                     operator: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            if batch.status != BatchStatus.PENDING:
                raise ValueError("只有待提交状态的批次才能提交")

            added: List[PrintTask] = []
            skipped: List[PlaybackItem] = []
            failed: List[PlaybackItem] = []

            for item in batch.items:
                if not item.selected:
                    skipped.append(item)
                    item.submit_result = "skipped_by_user"
                    item.add_timeline("提交结果", "用户未勾选，跳过", operator)
                    continue

                if item.group == PlaybackGroup.UNIMPORTABLE:
                    failed.append(item)
                    item.submit_result = "unimportable"
                    item.add_timeline("提交结果", "无法导入", operator)
                    continue

                if item.group == PlaybackGroup.DUPLICATE_CONFLICT:
                    result, task_to_add = self._resolve_conflict(item)
                    if result == "skip":
                        skipped.append(item)
                        item.submit_result = "skipped_conflict"
                        item.add_timeline("提交结果", f"冲突跳过: {item.resolution.value}", operator)
                        continue
                    elif result == "override" and task_to_add is not None:
                        queue_manager.add_tasks([task_to_add])
                        added.append(task_to_add)
                        item.submit_result = "added_override"
                        item.submitted_task_id = task_to_add.id
                        item.add_timeline(
                            "提交结果",
                            f"已入队 (覆盖优先级: {task_to_add.priority_override})",
                            operator
                        )
                        continue
                    else:
                        pass

                if item.parsed_task is None:
                    failed.append(item)
                    item.submit_result = "no_task"
                    continue

                queue_manager.add_tasks([item.parsed_task])
                added.append(item.parsed_task)
                item.submit_result = "added"
                item.submitted_task_id = item.parsed_task.id
                item.add_timeline("提交结果", "已入队", operator)

            summary = {
                "added_count": len(added),
                "skipped_count": len(skipped),
                "failed_count": len(failed),
                "added_ids": [t.id for t in added],
            }

            batch.status = BatchStatus.SUBMITTED
            batch.submitted_at = time.time()
            batch.submit_summary = summary
            batch.add_timeline(
                "批次提交",
                f"成功{len(added)}条, 跳过{len(skipped)}条, 失败{len(failed)}条",
                operator
            )

            self._remove_pending_batch(batch.batch_id)
            self._save_last_submitted(batch)

            return summary

    def _resolve_conflict(self, item: PlaybackItem) -> Tuple[str, Optional[PrintTask]]:
        if item.parsed_task is None:
            return "skip", None

        resolution = item.resolution
        if resolution == ConflictResolution.SKIP:
            return "skip", None
        elif resolution == ConflictResolution.OVERRIDE_PRIORITY:
            new_task = PrintTask.from_dict(item.parsed_task.to_dict())
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
            return "keep", item.parsed_task

    def export_audit_package(self, batch: ImportBatch, output_dir: str,
                             operator: Optional[str] = None) -> str:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        filename = f"audit_batch_{batch.batch_id}_{timestamp}.json"
        full_path = out_path / filename

        export_data = {
            "export_type": "playback_audit_package",
            "exported_at": time.time(),
            "exported_by": operator or batch.operator or "系统",
            "batch": batch.to_dict(),
        }

        with open(full_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)

        return str(full_path)

    def load_pending_batch(self, batch_id: str) -> Optional[ImportBatch]:
        with self._lock:
            batches = self._load_all_pending()
            for b in batches:
                if b.batch_id == batch_id:
                    return b
            return None

    def load_all_pending(self) -> List[ImportBatch]:
        with self._lock:
            return self._load_all_pending()

    def _load_all_pending(self) -> List[ImportBatch]:
        if not BATCHES_FILE.exists():
            return []
        try:
            with open(BATCHES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            return [ImportBatch.from_dict(d) for d in data]
        except (json.JSONDecodeError, ValueError, KeyError):
            return []

    def _save_pending_batch(self, batch: ImportBatch) -> None:
        batches = self._load_all_pending()
        found = False
        for i, b in enumerate(batches):
            if b.batch_id == batch.batch_id:
                batches[i] = batch
                found = True
                break
        if not found:
            batches.append(batch)

        BATCHES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BATCHES_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([b.to_dict() for b in batches], f, ensure_ascii=False, indent=2)
        os.replace(tmp, BATCHES_FILE)

    def _remove_pending_batch(self, batch_id: str) -> None:
        batches = self._load_all_pending()
        batches = [b for b in batches if b.batch_id != batch_id]
        BATCHES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BATCHES_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([b.to_dict() for b in batches], f, ensure_ascii=False, indent=2)
        os.replace(tmp, BATCHES_FILE)

    def load_last_submitted(self) -> Optional[ImportBatch]:
        with self._lock:
            if not LAST_SUBMITTED_FILE.exists():
                return None
            try:
                with open(LAST_SUBMITTED_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return ImportBatch.from_dict(data)
            except (json.JSONDecodeError, ValueError, KeyError):
                return None

    def _save_last_submitted(self, batch: ImportBatch) -> None:
        LAST_SUBMITTED_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LAST_SUBMITTED_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(batch.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, LAST_SUBMITTED_FILE)

    def load_ui_state(self) -> PlaybackUIState:
        with self._lock:
            if not UI_STATE_FILE.exists():
                return PlaybackUIState()
            try:
                with open(UI_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return PlaybackUIState.from_dict(data)
            except (json.JSONDecodeError, ValueError, KeyError):
                return PlaybackUIState()

    def _save_ui_state(self, state: PlaybackUIState) -> None:
        UI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = UI_STATE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, UI_STATE_FILE)

    def save_ui_state(self, state: PlaybackUIState) -> None:
        with self._lock:
            self._save_ui_state(state)
