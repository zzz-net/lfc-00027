import json
import os
import time
import threading
import uuid
import copy
import logging
from enum import Enum
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

from .storage import Storage
from .export_record import ExportRecord, ExportRecordManager, ExportFileEntry, compute_file_hash

logger = logging.getLogger(__name__)

SNAPSHOT_FORMAT_VERSION = "2.0"

REVIEW_SNAPSHOTS_FILE = None
LAST_REVIEW_SNAPSHOT_FILE = None
RECOVERY_LOG_FILE = None
IMPORT_UNDO_FILE = None


def _init_paths():
    global REVIEW_SNAPSHOTS_FILE, LAST_REVIEW_SNAPSHOT_FILE, RECOVERY_LOG_FILE, IMPORT_UNDO_FILE
    from .storage import DATA_DIR
    REVIEW_SNAPSHOTS_FILE = DATA_DIR / "review_snapshots.json"
    LAST_REVIEW_SNAPSHOT_FILE = DATA_DIR / "last_review_snapshot.json"
    RECOVERY_LOG_FILE = DATA_DIR / "recovery_log.json"
    IMPORT_UNDO_FILE = DATA_DIR / "import_undo.json"


_init_paths()


class SnapshotStatus(str, Enum):
    NORMAL = "normal"
    FILE_MISSING = "file_missing"
    CONTENT_CHANGED = "content_changed"
    PERMISSION_DENIED = "permission_denied"
    RECORD_GONE = "record_gone"
    FIELDS_MISSING = "fields_missing"


SNAPSHOT_STATUS_LABELS = {
    SnapshotStatus.NORMAL: "正常",
    SnapshotStatus.FILE_MISSING: "源文件丢失",
    SnapshotStatus.CONTENT_CHANGED: "内容已变更",
    SnapshotStatus.PERMISSION_DENIED: "权限不足",
    SnapshotStatus.RECORD_GONE: "记录已删除",
    SnapshotStatus.FIELDS_MISSING: "旧版快照(字段缺失)",
}


@dataclass
class DetailTabState:
    tab_index: int = 0
    scroll_position: float = 0.0
    expanded_sections: List[str] = field(default_factory=list)
    selected_file_index: int = 0
    timeline_position: Optional[float] = None
    preview_file_path: Optional[str] = None
    filter_conditions: Dict[str, Any] = field(default_factory=dict)

    _ALL_FIELDS = (
        "tab_index", "scroll_position", "expanded_sections",
        "selected_file_index", "timeline_position",
        "preview_file_path", "filter_conditions",
    )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DetailTabState":
        if not isinstance(d, dict):
            d = {}
        return cls(
            tab_index=d.get("tab_index", 0),
            scroll_position=d.get("scroll_position", 0.0),
            expanded_sections=d.get("expanded_sections", []),
            selected_file_index=d.get("selected_file_index", 0),
            timeline_position=d.get("timeline_position"),
            preview_file_path=d.get("preview_file_path"),
            filter_conditions=d.get("filter_conditions", {}),
        )

    @classmethod
    def missing_fields(cls, d: dict) -> List[str]:
        if not isinstance(d, dict):
            return list(cls._ALL_FIELDS)
        return [k for k in cls._ALL_FIELDS if k not in d]

    def ensure_complete(self) -> "DetailTabState":
        if self.expanded_sections is None:
            self.expanded_sections = []
        if self.filter_conditions is None:
            self.filter_conditions = {}
        if not isinstance(self.tab_index, int) or self.tab_index < 0:
            self.tab_index = 0
        if not isinstance(self.scroll_position, (int, float)):
            self.scroll_position = 0.0
        if not isinstance(self.selected_file_index, int) or self.selected_file_index < 0:
            self.selected_file_index = 0
        self.scroll_position = max(0.0, min(1.0, float(self.scroll_position)))
        return self

    def merge_with(self, other: "DetailTabState",
                   prefer_other: bool = True) -> "DetailTabState":
        if prefer_other:
            if other.tab_index is not None and other.tab_index != 0:
                self.tab_index = other.tab_index
            if other.scroll_position is not None and other.scroll_position != 0.0:
                self.scroll_position = other.scroll_position
            if other.expanded_sections:
                merged = list(dict.fromkeys(self.expanded_sections + other.expanded_sections))
                self.expanded_sections = merged
            if other.selected_file_index is not None and other.selected_file_index != 0:
                self.selected_file_index = other.selected_file_index
            if other.timeline_position is not None:
                self.timeline_position = other.timeline_position
            if other.preview_file_path:
                self.preview_file_path = other.preview_file_path
            if other.filter_conditions:
                fc = dict(self.filter_conditions)
                fc.update(other.filter_conditions)
                self.filter_conditions = fc
        else:
            if self.tab_index == 0 and other.tab_index != 0:
                self.tab_index = other.tab_index
            if self.scroll_position == 0.0 and other.scroll_position != 0.0:
                self.scroll_position = other.scroll_position
            if not self.expanded_sections and other.expanded_sections:
                self.expanded_sections = list(other.expanded_sections)
            if self.selected_file_index == 0 and other.selected_file_index != 0:
                self.selected_file_index = other.selected_file_index
            if self.timeline_position is None and other.timeline_position is not None:
                self.timeline_position = other.timeline_position
            if not self.preview_file_path and other.preview_file_path:
                self.preview_file_path = other.preview_file_path
            if not self.filter_conditions and other.filter_conditions:
                self.filter_conditions = dict(other.filter_conditions)
        return self.ensure_complete()

    def describe(self) -> str:
        parts = [f"tab={self.tab_index}", f"scroll={self.scroll_position:.2f}"]
        if self.expanded_sections:
            parts.append(f"expanded={len(self.expanded_sections)}")
        if self.selected_file_index:
            parts.append(f"file_idx={self.selected_file_index}")
        if self.timeline_position is not None:
            parts.append(f"timeline={self.timeline_position}")
        if self.preview_file_path:
            parts.append(f"preview={Path(self.preview_file_path).name}")
        if self.filter_conditions:
            parts.append(f"filters={len(self.filter_conditions)}")
        return ", ".join(parts)


@dataclass
class ReviewSnapshot:
    snapshot_id: str
    record_id: str
    created_at: float
    updated_at: float
    title: str
    is_pinned: bool = False
    view_order: int = 0

    record_snapshot: Optional[Dict[str, Any]] = None
    filter_snapshot: Dict[str, Any] = field(default_factory=dict)

    detail_state: DetailTabState = field(default_factory=DetailTabState)

    status: SnapshotStatus = SnapshotStatus.NORMAL
    status_detail: str = ""
    log_entries: List[str] = field(default_factory=list)

    batch_context: Optional[Dict[str, Any]] = None

    format_version: str = SNAPSHOT_FORMAT_VERSION
    is_auto: bool = False

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "record_id": self.record_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "title": self.title,
            "is_pinned": self.is_pinned,
            "view_order": self.view_order,
            "record_snapshot": self.record_snapshot,
            "filter_snapshot": self.filter_snapshot,
            "detail_state": self.detail_state.to_dict(),
            "status": self.status.value,
            "status_detail": self.status_detail,
            "log_entries": self.log_entries,
            "batch_context": self.batch_context,
            "format_version": self.format_version,
            "is_auto": self.is_auto,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewSnapshot":
        if not isinstance(d, dict):
            raise ValueError("快照数据必须是字典")

        missing_fields = []
        for key in ("snapshot_id", "record_id"):
            if key not in d:
                missing_fields.append(key)

        if missing_fields:
            raise ValueError(f"快照缺少必要字段: {', '.join(missing_fields)}")

        detail_raw = d.get("detail_state", {})
        if not isinstance(detail_raw, dict):
            detail_raw = {}

        known_detail_keys = {
            "tab_index", "scroll_position", "expanded_sections",
            "selected_file_index", "timeline_position",
            "preview_file_path", "filter_conditions",
        }
        has_missing_detail = any(k not in detail_raw for k in known_detail_keys)

        status_val = d.get("status", SnapshotStatus.NORMAL.value)
        try:
            status = SnapshotStatus(status_val)
        except ValueError:
            status = SnapshotStatus.NORMAL

        if has_missing_detail and status == SnapshotStatus.NORMAL:
            status = SnapshotStatus.FIELDS_MISSING

        snapshot = cls(
            snapshot_id=d["snapshot_id"],
            record_id=d["record_id"],
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            title=d.get("title", ""),
            is_pinned=d.get("is_pinned", False),
            view_order=d.get("view_order", 0),
            record_snapshot=d.get("record_snapshot"),
            filter_snapshot=d.get("filter_snapshot", {}),
            detail_state=DetailTabState.from_dict(detail_raw),
            status=status,
            status_detail=d.get("status_detail", ""),
            log_entries=d.get("log_entries", []),
            batch_context=d.get("batch_context"),
            format_version=d.get("format_version", "1.0"),
            is_auto=d.get("is_auto", False),
        )

        if has_missing_detail:
            snapshot.log_entries.append(
                f"[{time.strftime('%H:%M:%S')}] 旧版快照字段缺失，已自动补全默认值"
            )

        return snapshot


@dataclass
class ImportResult:
    success: bool
    imported_count: int = 0
    skipped_count: int = 0
    conflict_count: int = 0
    error_count: int = 0
    merged_count: int = 0
    messages: List[str] = field(default_factory=list)
    imported_ids: List[str] = field(default_factory=list)
    undo_available: bool = False


@dataclass
class RecoveryLogEntry:
    timestamp: float
    action: str
    detail: str
    snapshot_id: Optional[str] = None
    record_id: Optional[str] = None
    severity: str = "info"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "action": self.action,
            "detail": self.detail,
            "snapshot_id": self.snapshot_id,
            "record_id": self.record_id,
            "severity": self.severity,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RecoveryLogEntry":
        return cls(
            timestamp=d.get("timestamp", time.time()),
            action=d.get("action", ""),
            detail=d.get("detail", ""),
            snapshot_id=d.get("snapshot_id"),
            record_id=d.get("record_id"),
            severity=d.get("severity", "info"),
        )


class SnapshotSaveSource(str, Enum):
    AUTO_VIEW = "auto_view"
    AUTO_CLOSE = "auto_close"
    AUTO_TIMER = "auto_timer"
    MANUAL_UPDATE = "manual_update"
    MANUAL_CREATE = "manual_create"
    IMPORT = "import"


class SnapshotStateService:
    def __init__(self, manager: "ReviewWorkbenchManager"):
        self._mgr = manager

    def build_detail_state(
        self,
        tab_index: Optional[int] = None,
        scroll_position: Optional[float] = None,
        expanded_sections: Optional[List[str]] = None,
        selected_file_index: Optional[int] = None,
        timeline_position: Optional[float] = None,
        preview_file_path: Optional[str] = None,
        filter_conditions: Optional[Dict[str, Any]] = None,
        base_state: Optional[DetailTabState] = None,
    ) -> DetailTabState:
        state = base_state or DetailTabState()
        if tab_index is not None:
            state.tab_index = tab_index
        if scroll_position is not None:
            state.scroll_position = scroll_position
        if expanded_sections is not None:
            state.expanded_sections = list(expanded_sections)
        if selected_file_index is not None:
            state.selected_file_index = selected_file_index
        if timeline_position is not None:
            state.timeline_position = timeline_position
        if preview_file_path is not None:
            state.preview_file_path = preview_file_path
        if filter_conditions is not None:
            state.filter_conditions = dict(filter_conditions)
        return state.ensure_complete()

    def save_view_state(
        self,
        record_id: str,
        detail_state: DetailTabState,
        filter_snapshot: Optional[Dict[str, Any]] = None,
        batch_context: Optional[Dict[str, Any]] = None,
        source: SnapshotSaveSource = SnapshotSaveSource.AUTO_VIEW,
        snapshot_title: Optional[str] = None,
    ) -> ReviewSnapshot:
        detail_state = detail_state.ensure_complete()
        logger.info("[state_service] 保存现场 record=%s source=%s state=[%s]",
                    record_id, source.value, detail_state.describe())

        if source in (SnapshotSaveSource.AUTO_VIEW,
                      SnapshotSaveSource.AUTO_CLOSE,
                      SnapshotSaveSource.AUTO_TIMER):
            snap = self._mgr.auto_snapshot(
                record_id=record_id,
                detail_state=detail_state,
                filter_snapshot=filter_snapshot,
                batch_context=batch_context,
            )
        elif source == SnapshotSaveSource.MANUAL_CREATE:
            snap = self._mgr.create_snapshot(
                record_id=record_id,
                title=snapshot_title or "",
                detail_state=detail_state,
                filter_snapshot=filter_snapshot,
                batch_context=batch_context,
            )
        elif source == SnapshotSaveSource.MANUAL_UPDATE:
            existing = self._mgr.find_snapshot_by_record(record_id)
            if existing:
                snap = self._mgr.update_snapshot(
                    snapshot_id=existing.snapshot_id,
                    detail_state=detail_state,
                    title=snapshot_title,
                )
                if snap is None:
                    snap = self._mgr.create_snapshot(
                        record_id=record_id,
                        title=snapshot_title or "",
                        detail_state=detail_state,
                        filter_snapshot=filter_snapshot,
                        batch_context=batch_context,
                    )
            else:
                snap = self._mgr.create_snapshot(
                    record_id=record_id,
                    title=snapshot_title or "",
                    detail_state=detail_state,
                    filter_snapshot=filter_snapshot,
                    batch_context=batch_context,
                )
        else:
            snap = self._mgr.auto_snapshot(
                record_id=record_id,
                detail_state=detail_state,
                filter_snapshot=filter_snapshot,
                batch_context=batch_context,
            )

        self._mgr._log(
            action="state_saved",
            detail=f"通过 {source.value} 保存现场: {detail_state.describe()}",
            snapshot_id=snap.snapshot_id,
            record_id=record_id,
        )
        return snap

    def validate_state(self, state: DetailTabState,
                       tab_count: int = 4,
                       file_count: int = 0) -> DetailTabState:
        state = state.ensure_complete()
        if tab_count > 0 and state.tab_index >= tab_count:
            state.tab_index = max(0, tab_count - 1)
        if file_count > 0 and state.selected_file_index >= file_count:
            state.selected_file_index = max(0, file_count - 1)
        return state


class ReviewWorkbenchManager:
    def __init__(self, storage: Optional[Storage] = None,
                 record_manager: Optional[ExportRecordManager] = None):
        self._storage = storage or Storage()
        self._record_manager = record_manager or ExportRecordManager(self._storage)
        self._lock = threading.RLock()
        self._max_snapshots = 50
        self._active_auto_snapshots: Dict[str, str] = {}
        self._state_service = SnapshotStateService(self)

    @property
    def state_service(self) -> SnapshotStateService:
        return self._state_service

    def _log(self, action: str, detail: str,
             snapshot_id: Optional[str] = None,
             record_id: Optional[str] = None,
             severity: str = "info"):
        entry = RecoveryLogEntry(
            timestamp=time.time(),
            action=action,
            detail=detail,
            snapshot_id=snapshot_id,
            record_id=record_id,
            severity=severity,
        )
        self._append_log(entry)
        log_level = {
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
        }.get(severity, logging.INFO)
        logger.log(log_level, "[%s] %s (snap=%s rec=%s)", action, detail,
                   snapshot_id, record_id)

    def _append_log(self, entry: RecoveryLogEntry):
        if RECOVERY_LOG_FILE is None:
            _init_paths()
        try:
            RECOVERY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            logs = []
            if RECOVERY_LOG_FILE.exists():
                try:
                    with open(RECOVERY_LOG_FILE, "r", encoding="utf-8") as f:
                        logs = json.load(f)
                except (json.JSONDecodeError, ValueError):
                    logs = []
            logs.append(entry.to_dict())
            logs = logs[-200:]
            tmp = RECOVERY_LOG_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(logs, f, ensure_ascii=False, indent=2)
            os.replace(tmp, RECOVERY_LOG_FILE)
        except Exception as e:
            logger.error("写入恢复日志失败: %s", e)

    def get_recovery_logs(self, limit: int = 50) -> List[RecoveryLogEntry]:
        if RECOVERY_LOG_FILE is None:
            _init_paths()
        if not RECOVERY_LOG_FILE.exists():
            return []
        try:
            with open(RECOVERY_LOG_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
            entries = [RecoveryLogEntry.from_dict(d) for d in logs[-limit:]]
            entries.reverse()
            return entries
        except (json.JSONDecodeError, ValueError):
            return []

    def auto_snapshot(
        self,
        record_id: str,
        detail_state: Optional[DetailTabState] = None,
        filter_snapshot: Optional[Dict[str, Any]] = None,
        batch_context: Optional[Dict[str, Any]] = None,
    ) -> ReviewSnapshot:
        with self._lock:
            if record_id in self._active_auto_snapshots:
                existing_id = self._active_auto_snapshots[record_id]
                existing = self._load_snapshot(existing_id)
                if existing:
                    if detail_state:
                        detail_state.ensure_complete()
                        existing.detail_state.merge_with(detail_state, prefer_other=True)
                    if filter_snapshot:
                        merged_fs = dict(existing.filter_snapshot)
                        merged_fs.update(filter_snapshot)
                        existing.filter_snapshot = merged_fs
                    if batch_context:
                        merged_bc = dict(existing.batch_context) if existing.batch_context else {}
                        merged_bc.update(batch_context)
                        existing.batch_context = merged_bc
                    existing.detail_state.ensure_complete()
                    existing.updated_at = time.time()
                    record = self._record_manager.load_record(record_id)
                    if record:
                        existing.record_snapshot = record.to_dict()
                    self._check_snapshot_status(existing)
                    self._save_snapshot(existing)
                    self._save_last_snapshot_id(existing_id)
                    self._log("auto_snapshot_update",
                              f"更新自动快照: {existing.title} state=[{existing.detail_state.describe()}]",
                              snapshot_id=existing_id, record_id=record_id)
                    return existing

            record = self._record_manager.load_record(record_id)
            snapshot_id = f"rev_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

            record_snapshot = None
            if record:
                record_snapshot = record.to_dict()

            title = ""
            if record:
                t_str = time.strftime("%m-%d %H:%M", time.localtime(record.exported_at))
                title = f"{record.operator} - {t_str} - {record.result_message[:20]}"
            else:
                title = f"记录 {record_id[:12]}..."

            state = (detail_state or DetailTabState()).ensure_complete()
            if filter_snapshot:
                fc = dict(filter_snapshot)
                fc.update(state.filter_conditions)
                state.filter_conditions = fc

            snapshot = ReviewSnapshot(
                snapshot_id=snapshot_id,
                record_id=record_id,
                created_at=time.time(),
                updated_at=time.time(),
                title=title,
                record_snapshot=record_snapshot,
                filter_snapshot=filter_snapshot or {},
                detail_state=state,
                batch_context=batch_context,
                is_auto=True,
                format_version=SNAPSHOT_FORMAT_VERSION,
            )

            self._check_snapshot_status(snapshot)
            self._save_snapshot(snapshot)
            self._save_last_snapshot_id(snapshot_id)
            self._active_auto_snapshots[record_id] = snapshot_id

            self._log("auto_snapshot_create",
                      f"创建自动快照: {title} state=[{state.describe()}]",
                      snapshot_id=snapshot_id, record_id=record_id)

            return snapshot

    def create_snapshot(
        self,
        record_id: str,
        title: str = "",
        detail_state: Optional[DetailTabState] = None,
        filter_snapshot: Optional[Dict[str, Any]] = None,
        batch_context: Optional[Dict[str, Any]] = None,
    ) -> ReviewSnapshot:
        with self._lock:
            record = self._record_manager.load_record(record_id)
            snapshot_id = f"rev_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

            record_snapshot = None
            if record:
                record_snapshot = record.to_dict()

            if not title and record:
                t_str = time.strftime("%m-%d %H:%M", time.localtime(record.exported_at))
                title = f"{record.operator} - {t_str} - {record.result_message[:20]}"

            state = (detail_state or DetailTabState()).ensure_complete()
            if filter_snapshot and not state.filter_conditions:
                state.filter_conditions = dict(filter_snapshot)

            snapshot = ReviewSnapshot(
                snapshot_id=snapshot_id,
                record_id=record_id,
                created_at=time.time(),
                updated_at=time.time(),
                title=title,
                record_snapshot=record_snapshot,
                filter_snapshot=filter_snapshot or {},
                detail_state=state,
                batch_context=batch_context,
                format_version=SNAPSHOT_FORMAT_VERSION,
            )

            self._check_snapshot_status(snapshot)
            self._save_snapshot(snapshot)
            self._save_last_snapshot_id(snapshot_id)

            self._log("snapshot_create",
                      f"创建手动快照: {title} state=[{state.describe()}]",
                      snapshot_id=snapshot_id, record_id=record_id)

            return snapshot

    def update_snapshot(
        self,
        snapshot_id: str,
        detail_state: Optional[DetailTabState] = None,
        title: Optional[str] = None,
    ) -> Optional[ReviewSnapshot]:
        with self._lock:
            snapshot = self._load_snapshot(snapshot_id)
            if not snapshot:
                return None

            if detail_state:
                detail_state.ensure_complete()
                snapshot.detail_state.merge_with(detail_state, prefer_other=True)
                snapshot.detail_state.ensure_complete()
            if title is not None:
                snapshot.title = title

            snapshot.updated_at = time.time()
            self._check_snapshot_status(snapshot)
            self._save_snapshot(snapshot)

            self._log("snapshot_update",
                      f"更新快照: {snapshot.title} state=[{snapshot.detail_state.describe()}]",
                      snapshot_id=snapshot_id, record_id=snapshot.record_id)

            return snapshot

    def get_snapshot(self, snapshot_id: str) -> Optional[ReviewSnapshot]:
        with self._lock:
            return self._load_snapshot(snapshot_id)

    def get_snapshot_record(self, snapshot: ReviewSnapshot) -> Optional[ExportRecord]:
        record = self._record_manager.load_record(snapshot.record_id)
        if record:
            return record
        if snapshot.record_snapshot:
            try:
                return ExportRecord.from_dict(snapshot.record_snapshot)
            except (KeyError, ValueError, TypeError):
                return None
        return None

    def list_snapshots(self, include_record_gone: bool = True) -> List[ReviewSnapshot]:
        with self._lock:
            snapshots = self._load_all_snapshots()

            for s in snapshots:
                self._check_snapshot_status(s)

            if not include_record_gone:
                snapshots = [s for s in snapshots if s.status != SnapshotStatus.RECORD_GONE]

            snapshots.sort(key=lambda s: (-s.is_pinned, s.view_order, -s.updated_at))
            return snapshots

    def find_snapshot_by_record(self, record_id: str) -> Optional[ReviewSnapshot]:
        with self._lock:
            snapshots = self._load_all_snapshots()
            for s in reversed(snapshots):
                if s.record_id == record_id:
                    return s
            return None

    def pin_snapshot(self, snapshot_id: str, pinned: bool = True) -> bool:
        with self._lock:
            snapshot = self._load_snapshot(snapshot_id)
            if not snapshot:
                return False
            snapshot.is_pinned = pinned
            snapshot.updated_at = time.time()
            self._save_snapshot(snapshot)
            self._log("pin_snapshot", f"{'置顶' if pinned else '取消置顶'}: {snapshot.title}",
                      snapshot_id=snapshot_id)
            return True

    def set_view_order(self, snapshot_id: str, order: int) -> bool:
        with self._lock:
            snapshot = self._load_snapshot(snapshot_id)
            if not snapshot:
                return False
            snapshot.view_order = order
            snapshot.updated_at = time.time()
            self._save_snapshot(snapshot)
            return True

    def delete_snapshot(self, snapshot_id: str) -> bool:
        with self._lock:
            snapshots = self._load_all_snapshots()
            target = None
            for s in snapshots:
                if s.snapshot_id == snapshot_id:
                    target = s
                    break

            new_snapshots = [s for s in snapshots if s.snapshot_id != snapshot_id]
            if len(new_snapshots) == len(snapshots):
                return False
            self._save_all_snapshots(new_snapshots)

            last_id = self._load_last_snapshot_id()
            if last_id == snapshot_id:
                self._save_last_snapshot_id(None)

            if target:
                for rid, sid in list(self._active_auto_snapshots.items()):
                    if sid == snapshot_id:
                        del self._active_auto_snapshots[rid]

            self._log("delete_snapshot", f"删除快照: {target.title if target else snapshot_id}",
                      snapshot_id=snapshot_id, severity="warning")

            return True

    def get_last_snapshot(self) -> Optional[ReviewSnapshot]:
        with self._lock:
            last_id = self._load_last_snapshot_id()
            if not last_id:
                return None
            return self._load_snapshot(last_id)

    def set_last_snapshot(self, snapshot_id: str) -> None:
        with self._lock:
            self._save_last_snapshot_id(snapshot_id)

    def get_adjacent_snapshots(self, snapshot_id: str) -> Tuple[Optional[ReviewSnapshot], Optional[ReviewSnapshot]]:
        snapshots = self.list_snapshots()
        idx = None
        for i, s in enumerate(snapshots):
            if s.snapshot_id == snapshot_id:
                idx = i
                break

        if idx is None:
            return None, None

        prev_s = snapshots[idx - 1] if idx > 0 else None
        next_s = snapshots[idx + 1] if idx < len(snapshots) - 1 else None
        return prev_s, next_s

    def get_batch_context_snapshots(self, snapshot: ReviewSnapshot) -> List[ReviewSnapshot]:
        if not snapshot.batch_context or not snapshot.batch_context.get("batch_id"):
            return []

        batch_id = snapshot.batch_context["batch_id"]
        all_snapshots = self.list_snapshots()
        return [
            s for s in all_snapshots
            if s.snapshot_id != snapshot.snapshot_id
            and s.batch_context
            and s.batch_context.get("batch_id") == batch_id
        ]

    def check_snapshot_health(self, snapshot: ReviewSnapshot) -> Dict[str, Any]:
        issues = []

        if snapshot.format_version != SNAPSHOT_FORMAT_VERSION:
            issues.append({
                "type": "old_format",
                "severity": "info",
                "message": f"快照格式版本 {snapshot.format_version}，当前版本 {SNAPSHOT_FORMAT_VERSION}，部分字段可能缺失",
            })

        record = self._record_manager.load_record(snapshot.record_id)
        if not record:
            issues.append({
                "type": "record_gone",
                "severity": "warning",
                "message": "原始导出记录已被删除，快照中保留了记录副本",
            })
            record_data = snapshot.record_snapshot
            if record_data:
                try:
                    record = ExportRecord.from_dict(record_data)
                except (KeyError, ValueError, TypeError):
                    return {
                        "status": SnapshotStatus.RECORD_GONE,
                        "issues": issues,
                        "can_view": False,
                    }
            else:
                return {
                    "status": SnapshotStatus.RECORD_GONE,
                    "issues": issues,
                    "can_view": False,
                }
        else:
            if snapshot.record_snapshot:
                snapshot_hash = snapshot.record_snapshot.get("content_hash", "")
                if snapshot_hash and record.content_hash and snapshot_hash != record.content_hash:
                    issues.append({
                        "type": "record_changed",
                        "severity": "info",
                        "message": "导出记录内容已更新，快照保留的是旧版本",
                    })

        file_issues = []
        for f in record.files:
            entry = {"filename": f.filename, "issues": []}
            if not f.file_path:
                continue
            path = Path(f.file_path)
            if not path.exists():
                entry["issues"].append("源文件已被删除")
            else:
                try:
                    with open(f.file_path, "r", encoding="utf-8") as test_f:
                        test_f.read(1)
                except PermissionError:
                    entry["issues"].append("权限不足，无法读取")
                except OSError as e:
                    entry["issues"].append(f"读取错误: {e}")

                if f.content_hash:
                    current_hash = compute_file_hash(f.file_path)
                    if current_hash and current_hash != f.content_hash:
                        entry["issues"].append("文件内容自导出后已变更")

            if entry["issues"]:
                file_issues.append(entry)

        record_gone = any(i.get("type") == "record_gone" for i in issues)
        old_format = any(i.get("type") == "old_format" for i in issues)

        overall_status = SnapshotStatus.NORMAL
        if record_gone:
            overall_status = SnapshotStatus.RECORD_GONE
        elif file_issues:
            all_missing = all("已被删除" in " ".join(e["issues"]) for e in file_issues)
            any_perm = any("权限不足" in " ".join(e["issues"]) for e in file_issues)
            if all_missing:
                overall_status = SnapshotStatus.FILE_MISSING
            elif any_perm:
                overall_status = SnapshotStatus.PERMISSION_DENIED
            else:
                overall_status = SnapshotStatus.CONTENT_CHANGED
        elif old_format:
            overall_status = SnapshotStatus.FIELDS_MISSING

        can_view = snapshot.record_snapshot is not None or not record_gone

        return {
            "status": overall_status,
            "issues": issues,
            "file_issues": file_issues,
            "can_view": can_view,
        }

    def export_snapshots(self, output_path: str, snapshot_ids: Optional[List[str]] = None) -> Tuple[bool, str]:
        with self._lock:
            try:
                snapshots = self._load_all_snapshots()
                if snapshot_ids:
                    snapshots = [s for s in snapshots if s.snapshot_id in snapshot_ids]

                data = {
                    "export_version": "2.0",
                    "exported_at": time.time(),
                    "snapshot_count": len(snapshots),
                    "snapshots": [s.to_dict() for s in snapshots],
                }

                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                tmp_path = Path(output_path).with_suffix(".tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, output_path)

                self._log("export", f"导出 {len(snapshots)} 个快照到 {output_path}")
                return True, f"成功导出 {len(snapshots)} 个快照"
            except PermissionError as e:
                self._log("export_fail", f"权限不足: {output_path} - {e}", severity="error")
                return False, f"权限不足，无法写入文件: {e}"
            except OSError as e:
                self._log("export_fail", f"写入失败: {output_path} - {e}", severity="error")
                return False, f"写入文件失败: {e}"

    def import_snapshots(self, input_path: str, conflict_strategy: str = "skip") -> ImportResult:
        result = ImportResult(success=False)

        with self._lock:
            try:
                with open(input_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                result.messages.append("导入文件不存在")
                result.error_count = 1
                self._log("import_fail", f"文件不存在: {input_path}", severity="error")
                return result
            except PermissionError as e:
                result.messages.append(f"权限不足，无法读取文件: {e}")
                result.error_count = 1
                self._log("import_fail", f"权限不足: {input_path} - {e}", severity="error")
                return result
            except json.JSONDecodeError as e:
                result.messages.append(f"JSON 格式错误: {e}")
                result.error_count = 1
                self._log("import_fail", f"JSON格式错误: {input_path} - {e}", severity="error")
                return result

            if not isinstance(data, dict) or "snapshots" not in data:
                result.messages.append("文件格式错误：缺少 snapshots 字段")
                result.error_count = 1
                self._log("import_fail", f"缺少snapshots字段: {input_path}", severity="error")
                return result

            imported_data = data["snapshots"]
            existing = self._load_all_snapshots()
            existing_ids = {s.snapshot_id: s for s in existing}

            pre_import_backup = [s.to_dict() for s in existing]

            new_snapshots = []
            overwritten_ids = []

            for item in imported_data:
                try:
                    snapshot = ReviewSnapshot.from_dict(item)
                except (KeyError, ValueError) as e:
                    result.error_count += 1
                    result.messages.append(f"跳过格式错误的快照: {e}")
                    self._log("import_skip", f"格式错误: {e}", severity="warning")
                    continue

                if snapshot.snapshot_id in existing_ids:
                    result.conflict_count += 1
                    existing_snap = existing_ids[snapshot.snapshot_id]

                    if conflict_strategy == "skip":
                        result.skipped_count += 1
                        result.messages.append(
                            f"跳过同名快照: {snapshot.snapshot_id} ({snapshot.title[:30]})"
                        )
                        continue
                    elif conflict_strategy == "overwrite":
                        overwritten_ids.append(snapshot.snapshot_id)
                        result.messages.append(
                            f"覆盖同名快照: {snapshot.snapshot_id}"
                        )
                        existing = [s for s in existing if s.snapshot_id != snapshot.snapshot_id]
                    elif conflict_strategy == "rename":
                        new_id = f"{snapshot.snapshot_id}_imported_{int(time.time())}"
                        snapshot.snapshot_id = new_id
                        result.messages.append(
                            f"另存为新快照: {new_id}"
                        )
                    elif conflict_strategy == "merge":
                        merged = self._merge_snapshots(existing_snap, snapshot)
                        existing = [s for s in existing if s.snapshot_id != merged.snapshot_id]
                        snapshot = merged
                        result.merged_count += 1
                        result.messages.append(
                            f"合并快照: {snapshot.snapshot_id} ({snapshot.title[:30]})"
                        )
                    else:
                        result.skipped_count += 1
                        continue

                self._check_snapshot_status(snapshot)
                new_snapshots.append(snapshot)
                result.imported_count += 1
                result.imported_ids.append(snapshot.snapshot_id)

            merged_list = existing + new_snapshots
            merged_list = merged_list[-self._max_snapshots:]
            self._save_all_snapshots(merged_list)

            if result.imported_count > 0 or result.merged_count > 0:
                self._save_import_undo(pre_import_backup, overwritten_ids)

            result.undo_available = result.imported_count > 0 or result.merged_count > 0
            result.success = True
            result.messages.append(
                f"导入完成: 成功{result.imported_count}条, "
                f"合并{result.merged_count}条, "
                f"跳过{result.skipped_count}条, "
                f"冲突{result.conflict_count}条, "
                f"错误{result.error_count}条"
            )

            self._log("import", f"导入 {result.imported_count} 条快照 (策略={conflict_strategy})",
                      severity="info")

            return result

    def undo_last_import(self) -> Tuple[bool, str]:
        with self._lock:
            undo_data = self._load_import_undo()
            if not undo_data:
                self._log("undo_fail", "无可撤销的导入", severity="warning")
                return False, "没有可撤销的最近一次导入"

            backup_snapshots = undo_data.get("backup_snapshots", [])
            overwritten_ids = undo_data.get("overwritten_ids", [])

            snapshots = []
            for d in backup_snapshots:
                try:
                    snapshots.append(ReviewSnapshot.from_dict(d))
                except (KeyError, ValueError):
                    continue

            self._save_all_snapshots(snapshots)

            self._clear_import_undo()

            self._active_auto_snapshots.clear()

            msg = f"已撤销最近一次导入，恢复到 {len(snapshots)} 个快照"
            if overwritten_ids:
                msg += f"（恢复了 {len(overwritten_ids)} 个被覆盖的快照）"

            self._log("undo_import", msg, severity="info")
            return True, msg

    def can_undo_import(self) -> bool:
        undo_data = self._load_import_undo()
        return undo_data is not None

    def _merge_snapshots(self, existing: ReviewSnapshot, incoming: ReviewSnapshot) -> ReviewSnapshot:
        merged = copy.deepcopy(existing)

        if incoming.updated_at > merged.updated_at:
            merged.updated_at = incoming.updated_at

        if incoming.record_snapshot and not merged.record_snapshot:
            merged.record_snapshot = incoming.record_snapshot
        elif incoming.record_snapshot and merged.record_snapshot:
            if incoming.updated_at > existing.updated_at:
                merged.record_snapshot = incoming.record_snapshot

        merged_detail = merged.detail_state
        incoming_detail = incoming.detail_state

        if not merged_detail.expanded_sections and incoming_detail.expanded_sections:
            merged_detail.expanded_sections = incoming_detail.expanded_sections
        elif incoming_detail.expanded_sections:
            merged_set = set(merged_detail.expanded_sections)
            for sec in incoming_detail.expanded_sections:
                if sec not in merged_set:
                    merged_detail.expanded_sections.append(sec)

        if not merged_detail.filter_conditions and incoming_detail.filter_conditions:
            merged_detail.filter_conditions = incoming_detail.filter_conditions
        elif incoming_detail.filter_conditions:
            merged_detail.filter_conditions.update(incoming_detail.filter_conditions)

        if incoming_detail.preview_file_path and not merged_detail.preview_file_path:
            merged_detail.preview_file_path = incoming_detail.preview_file_path

        if incoming_detail.timeline_position is not None and merged_detail.timeline_position is None:
            merged_detail.timeline_position = incoming_detail.timeline_position

        merged.log_entries.extend(incoming.log_entries)
        merged.log_entries = merged.log_entries[-20:]

        if incoming.batch_context and not merged.batch_context:
            merged.batch_context = incoming.batch_context

        merged.format_version = SNAPSHOT_FORMAT_VERSION
        merged.is_auto = existing.is_auto or incoming.is_auto

        merged.log_entries.append(
            f"[{time.strftime('%H:%M:%S')}] 合并自导入快照 {incoming.snapshot_id}"
        )

        return merged

    def _save_import_undo(self, backup_snapshots: List[dict], overwritten_ids: List[str]):
        if IMPORT_UNDO_FILE is None:
            _init_paths()
        IMPORT_UNDO_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "backup_snapshots": backup_snapshots,
            "overwritten_ids": overwritten_ids,
            "timestamp": time.time(),
        }
        tmp = IMPORT_UNDO_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, IMPORT_UNDO_FILE)

    def _load_import_undo(self) -> Optional[dict]:
        if IMPORT_UNDO_FILE is None:
            _init_paths()
        if not IMPORT_UNDO_FILE.exists():
            return None
        try:
            with open(IMPORT_UNDO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return None

    def _clear_import_undo(self):
        if IMPORT_UNDO_FILE is None:
            _init_paths()
        if IMPORT_UNDO_FILE.exists():
            try:
                IMPORT_UNDO_FILE.unlink()
            except OSError:
                pass

    def _check_snapshot_status(self, snapshot: ReviewSnapshot) -> None:
        health = self.check_snapshot_health(snapshot)
        snapshot.status = health["status"]

        details = []
        for issue in health.get("issues", []):
            details.append(issue["message"])
        for fissue in health.get("file_issues", []):
            for issue in fissue["issues"]:
                details.append(f"{fissue['filename']}: {issue}")

        snapshot.status_detail = "; ".join(details[:3])

    def _load_all_snapshots(self) -> List[ReviewSnapshot]:
        if REVIEW_SNAPSHOTS_FILE is None:
            _init_paths()
        if not REVIEW_SNAPSHOTS_FILE.exists():
            return []
        try:
            with open(REVIEW_SNAPSHOTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            snapshots = []
            for d in data:
                try:
                    snapshots.append(ReviewSnapshot.from_dict(d))
                except (KeyError, ValueError):
                    continue
            return snapshots
        except (json.JSONDecodeError, ValueError):
            return []

    def _load_snapshot(self, snapshot_id: str) -> Optional[ReviewSnapshot]:
        for s in self._load_all_snapshots():
            if s.snapshot_id == snapshot_id:
                return s
        return None

    def _save_snapshot(self, snapshot: ReviewSnapshot) -> None:
        snapshots = self._load_all_snapshots()
        found = False
        for i, s in enumerate(snapshots):
            if s.snapshot_id == snapshot.snapshot_id:
                snapshots[i] = snapshot
                found = True
                break
        if not found:
            snapshots.append(snapshot)

        snapshots = snapshots[-self._max_snapshots:]
        self._save_all_snapshots(snapshots)

    def _save_all_snapshots(self, snapshots: List[ReviewSnapshot]) -> None:
        if REVIEW_SNAPSHOTS_FILE is None:
            _init_paths()
        REVIEW_SNAPSHOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = REVIEW_SNAPSHOTS_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([s.to_dict() for s in snapshots], f, ensure_ascii=False, indent=2)
        os.replace(tmp, REVIEW_SNAPSHOTS_FILE)

    def _save_last_snapshot_id(self, snapshot_id: Optional[str]) -> None:
        if LAST_REVIEW_SNAPSHOT_FILE is None:
            _init_paths()
        LAST_REVIEW_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {"last_snapshot_id": snapshot_id, "updated_at": time.time()}
        tmp = LAST_REVIEW_SNAPSHOT_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LAST_REVIEW_SNAPSHOT_FILE)

    def _load_last_snapshot_id(self) -> Optional[str]:
        if LAST_REVIEW_SNAPSHOT_FILE is None:
            _init_paths()
        if not LAST_REVIEW_SNAPSHOT_FILE.exists():
            return None
        try:
            with open(LAST_REVIEW_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("last_snapshot_id")
        except (json.JSONDecodeError, ValueError, KeyError):
            return None
