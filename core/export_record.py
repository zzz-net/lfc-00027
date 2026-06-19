import json
import os
import hashlib
import time
import threading
import uuid
from enum import Enum
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any

from .storage import Storage, EXPORT_RECORDS_FILE, EXPORT_RECORD_UI_STATE_FILE


class ExportStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class ExportTrigger(str, Enum):
    MANUAL_HISTORY = "manual_history"
    MANUAL_ALL = "manual_all"
    AUDIT_PACKAGE = "audit_package"
    PREFLIGHT_RECORD = "preflight_record"
    PREFLIGHT_APPLY_LOGS = "preflight_apply_logs"


EXPORT_TRIGGER_LABELS = {
    ExportTrigger.MANUAL_HISTORY: "手动导出历史",
    ExportTrigger.MANUAL_ALL: "手动导出全部",
    ExportTrigger.AUDIT_PACKAGE: "审计包导出",
    ExportTrigger.PREFLIGHT_RECORD: "预检记录导出",
    ExportTrigger.PREFLIGHT_APPLY_LOGS: "预检操作日志导出",
}


class ConflictHint(str, Enum):
    NONE = "none"
    FILE_DELETED = "file_deleted"
    CONTENT_CHANGED = "content_changed"
    DUPLICATE_BATCH = "duplicate_batch"


CONFLICT_HINT_LABELS = {
    ConflictHint.NONE: "无冲突",
    ConflictHint.FILE_DELETED: "原文件已删除",
    ConflictHint.CONTENT_CHANGED: "包内容已变更",
    ConflictHint.DUPLICATE_BATCH: "同批次重复导出",
}


@dataclass
class ExportFileEntry:
    filename: str
    file_path: str
    file_size: int = 0
    row_count: int = 0
    content_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExportFileEntry":
        return cls(
            filename=d.get("filename", ""),
            file_path=d.get("file_path", ""),
            file_size=d.get("file_size", 0),
            row_count=d.get("row_count", 0),
            content_hash=d.get("content_hash", ""),
        )


@dataclass
class ExportRecordUIState:
    selected_status_filter: Optional[str] = None
    selected_trigger_filter: Optional[str] = None
    search_text: str = ""
    last_viewed_record_id: Optional[str] = None
    scroll_position: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExportRecordUIState":
        return cls(
            selected_status_filter=d.get("selected_status_filter"),
            selected_trigger_filter=d.get("selected_trigger_filter"),
            search_text=d.get("search_text", ""),
            last_viewed_record_id=d.get("last_viewed_record_id"),
            scroll_position=d.get("scroll_position", 0),
        )


@dataclass
class ExportRecord:
    record_id: str
    exported_at: float
    trigger: ExportTrigger
    status: ExportStatus
    operator: str
    filter_snapshot: Dict[str, Any] = field(default_factory=dict)
    batch_summary: Optional[Dict[str, Any]] = None
    files: List[ExportFileEntry] = field(default_factory=list)
    statistics: Dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    version_tag: str = ""
    result_message: str = ""
    failure_reason: str = ""
    conflict_hint: ConflictHint = ConflictHint.NONE
    conflict_detail: str = ""
    log_entries: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "exported_at": self.exported_at,
            "trigger": self.trigger.value,
            "status": self.status.value,
            "operator": self.operator,
            "filter_snapshot": self.filter_snapshot,
            "batch_summary": self.batch_summary,
            "files": [f.to_dict() for f in self.files],
            "statistics": self.statistics,
            "content_hash": self.content_hash,
            "version_tag": self.version_tag,
            "result_message": self.result_message,
            "failure_reason": self.failure_reason,
            "conflict_hint": self.conflict_hint.value,
            "conflict_detail": self.conflict_detail,
            "log_entries": self.log_entries,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExportRecord":
        return cls(
            record_id=d["record_id"],
            exported_at=d["exported_at"],
            trigger=ExportTrigger(d["trigger"]),
            status=ExportStatus(d["status"]),
            operator=d.get("operator", ""),
            filter_snapshot=d.get("filter_snapshot", {}),
            batch_summary=d.get("batch_summary"),
            files=[ExportFileEntry.from_dict(f) for f in d.get("files", [])],
            statistics=d.get("statistics", {}),
            content_hash=d.get("content_hash", ""),
            version_tag=d.get("version_tag", ""),
            result_message=d.get("result_message", ""),
            failure_reason=d.get("failure_reason", ""),
            conflict_hint=ConflictHint(d.get("conflict_hint", ConflictHint.NONE.value)),
            conflict_detail=d.get("conflict_detail", ""),
            log_entries=d.get("log_entries", []),
        )


def compute_file_hash(file_path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, IOError):
        return ""


def compute_content_hash(data: Any) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ExportRecordManager:
    def __init__(self, storage: Optional[Storage] = None):
        self._storage = storage or Storage()
        self._lock = threading.RLock()

    def create_record(
        self,
        trigger: ExportTrigger,
        status: ExportStatus,
        operator: str,
        filter_snapshot: Optional[Dict[str, Any]] = None,
        batch_summary: Optional[Dict[str, Any]] = None,
        files: Optional[List[ExportFileEntry]] = None,
        statistics: Optional[Dict[str, Any]] = None,
        content_hash: str = "",
        version_tag: str = "",
        result_message: str = "",
        failure_reason: str = "",
    ) -> ExportRecord:
        record_id = f"exp_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        record = ExportRecord(
            record_id=record_id,
            exported_at=time.time(),
            trigger=trigger,
            status=status,
            operator=operator,
            filter_snapshot=filter_snapshot or {},
            batch_summary=batch_summary,
            files=files or [],
            statistics=statistics or {},
            content_hash=content_hash,
            version_tag=version_tag,
            result_message=result_message,
            failure_reason=failure_reason,
        )

        conflict_hint, conflict_detail = self._detect_conflict(record)
        record.conflict_hint = conflict_hint
        record.conflict_detail = conflict_detail

        if conflict_hint != ConflictHint.NONE:
            record.log_entries.append(
                f"[{time.strftime('%H:%M:%S')}] 冲突检测: {CONFLICT_HINT_LABELS[conflict_hint]} - {conflict_detail}"
            )

        self._save_record(record)
        return record

    def _detect_conflict(self, record: ExportRecord) -> tuple:
        for f in record.files:
            if f.file_path and not Path(f.file_path).exists():
                return ConflictHint.FILE_DELETED, f"导出文件已不存在: {f.filename}"

            if f.file_path and f.content_hash and Path(f.file_path).exists():
                current_hash = compute_file_hash(f.file_path)
                if current_hash and current_hash != f.content_hash:
                    return ConflictHint.CONTENT_CHANGED, f"导出文件内容已变更: {f.filename}"

        if record.batch_summary and record.batch_summary.get("batch_id"):
            existing = self._load_all_records()
            for er in existing:
                if (
                    er.record_id != record.record_id
                    and er.trigger == record.trigger
                    and er.batch_summary
                    and er.batch_summary.get("batch_id") == record.batch_summary.get("batch_id")
                    and er.status == ExportStatus.SUCCESS
                ):
                    return ConflictHint.DUPLICATE_BATCH, (
                        f"同批次({record.batch_summary['batch_id'][:16]}...)已成功导出于 "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(er.exported_at))}"
                    )

        return ConflictHint.NONE, ""

    def check_file_conflicts(self, record: ExportRecord) -> List[Dict[str, Any]]:
        conflicts = []
        for f in record.files:
            entry = {"filename": f.filename, "file_path": f.file_path, "issues": []}
            if not f.file_path:
                continue
            if not Path(f.file_path).exists():
                entry["issues"].append("原文件已被删除，无法打开")
            elif f.content_hash and Path(f.file_path).exists():
                current_hash = compute_file_hash(f.file_path)
                if current_hash and current_hash != f.content_hash:
                    entry["issues"].append("文件内容自导出后已发生变更")
            if Path(f.file_path).exists():
                try:
                    with open(f.file_path, "r", encoding="utf-8") as test_f:
                        test_f.read(1)
                except PermissionError:
                    entry["issues"].append("权限不足，无法读取文件")
                except OSError as e:
                    entry["issues"].append(f"读取文件时出错: {e}")
            if entry["issues"]:
                conflicts.append(entry)
        return conflicts

    def load_all_records(self) -> List[ExportRecord]:
        with self._lock:
            return self._load_all_records()

    def _load_all_records(self) -> List[ExportRecord]:
        if not EXPORT_RECORDS_FILE.exists():
            return []
        try:
            with open(EXPORT_RECORDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            return [ExportRecord.from_dict(d) for d in data]
        except (json.JSONDecodeError, ValueError, KeyError):
            return []

    def load_record(self, record_id: str) -> Optional[ExportRecord]:
        with self._lock:
            for r in self._load_all_records():
                if r.record_id == record_id:
                    return r
            return None

    def query_records(
        self,
        status_filter: Optional[ExportStatus] = None,
        trigger_filter: Optional[ExportTrigger] = None,
        search_text: str = "",
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
    ) -> List[ExportRecord]:
        with self._lock:
            records = self._load_all_records()

        if status_filter is not None:
            records = [r for r in records if r.status == status_filter]
        if trigger_filter is not None:
            records = [r for r in records if r.trigger == trigger_filter]
        if start_time is not None:
            records = [r for r in records if r.exported_at >= start_time]
        if end_time is not None:
            records = [r for r in records if r.exported_at <= end_time]
        if search_text:
            lower = search_text.lower()
            records = [
                r for r in records
                if lower in r.operator.lower()
                or lower in r.result_message.lower()
                or lower in r.failure_reason.lower()
                or lower in r.record_id.lower()
                or any(lower in f.filename.lower() for f in r.files)
            ]

        records.sort(key=lambda r: r.exported_at, reverse=True)
        return records

    def _save_record(self, record: ExportRecord) -> None:
        with self._lock:
            records = self._load_all_records()
            found = False
            for i, r in enumerate(records):
                if r.record_id == record.record_id:
                    records[i] = record
                    found = True
                    break
            if not found:
                records.append(record)

            records = records[-200:]

            EXPORT_RECORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = EXPORT_RECORDS_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([r.to_dict() for r in records], f, ensure_ascii=False, indent=2)
            os.replace(tmp, EXPORT_RECORDS_FILE)

    def save_ui_state(self, state: ExportRecordUIState) -> None:
        with self._lock:
            EXPORT_RECORD_UI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = EXPORT_RECORD_UI_STATE_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, EXPORT_RECORD_UI_STATE_FILE)

    def load_ui_state(self) -> ExportRecordUIState:
        with self._lock:
            if not EXPORT_RECORD_UI_STATE_FILE.exists():
                return ExportRecordUIState()
            try:
                with open(EXPORT_RECORD_UI_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return ExportRecordUIState.from_dict(data)
            except (json.JSONDecodeError, ValueError, KeyError):
                return ExportRecordUIState()
