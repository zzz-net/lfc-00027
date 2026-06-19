import json
import os
import time
import threading
import uuid
from enum import Enum
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

from .storage import Storage
from .export_record import ExportRecord, ExportRecordManager, ExportFileEntry, compute_file_hash


REVIEW_SNAPSHOTS_FILE = None
LAST_REVIEW_SNAPSHOT_FILE = None


def _init_paths():
    global REVIEW_SNAPSHOTS_FILE, LAST_REVIEW_SNAPSHOT_FILE
    from .storage import DATA_DIR
    REVIEW_SNAPSHOTS_FILE = DATA_DIR / "review_snapshots.json"
    LAST_REVIEW_SNAPSHOT_FILE = DATA_DIR / "last_review_snapshot.json"


_init_paths()


class SnapshotStatus(str, Enum):
    NORMAL = "normal"
    FILE_MISSING = "file_missing"
    CONTENT_CHANGED = "content_changed"
    PERMISSION_DENIED = "permission_denied"
    RECORD_GONE = "record_gone"


SNAPSHOT_STATUS_LABELS = {
    SnapshotStatus.NORMAL: "正常",
    SnapshotStatus.FILE_MISSING: "源文件丢失",
    SnapshotStatus.CONTENT_CHANGED: "内容已变更",
    SnapshotStatus.PERMISSION_DENIED: "权限不足",
    SnapshotStatus.RECORD_GONE: "记录已删除",
}


@dataclass
class DetailTabState:
    tab_index: int = 0
    scroll_position: float = 0.0
    expanded_sections: List[str] = field(default_factory=list)
    selected_file_index: int = 0
    timeline_position: Optional[float] = None
    preview_file_path: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DetailTabState":
        return cls(
            tab_index=d.get("tab_index", 0),
            scroll_position=d.get("scroll_position", 0.0),
            expanded_sections=d.get("expanded_sections", []),
            selected_file_index=d.get("selected_file_index", 0),
            timeline_position=d.get("timeline_position"),
            preview_file_path=d.get("preview_file_path"),
        )


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
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewSnapshot":
        return cls(
            snapshot_id=d["snapshot_id"],
            record_id=d["record_id"],
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            title=d.get("title", ""),
            is_pinned=d.get("is_pinned", False),
            view_order=d.get("view_order", 0),
            record_snapshot=d.get("record_snapshot"),
            filter_snapshot=d.get("filter_snapshot", {}),
            detail_state=DetailTabState.from_dict(d.get("detail_state", {})),
            status=SnapshotStatus(d.get("status", SnapshotStatus.NORMAL.value)),
            status_detail=d.get("status_detail", ""),
            log_entries=d.get("log_entries", []),
            batch_context=d.get("batch_context"),
        )


@dataclass
class ImportResult:
    success: bool
    imported_count: int = 0
    skipped_count: int = 0
    conflict_count: int = 0
    error_count: int = 0
    messages: List[str] = field(default_factory=list)
    imported_ids: List[str] = field(default_factory=list)


class ReviewWorkbenchManager:
    def __init__(self, storage: Optional[Storage] = None,
                 record_manager: Optional[ExportRecordManager] = None):
        self._storage = storage or Storage()
        self._record_manager = record_manager or ExportRecordManager(self._storage)
        self._lock = threading.RLock()
        self._max_snapshots = 50

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

            snapshot = ReviewSnapshot(
                snapshot_id=snapshot_id,
                record_id=record_id,
                created_at=time.time(),
                updated_at=time.time(),
                title=title,
                record_snapshot=record_snapshot,
                filter_snapshot=filter_snapshot or {},
                detail_state=detail_state or DetailTabState(),
                batch_context=batch_context,
            )

            self._check_snapshot_status(snapshot)
            self._save_snapshot(snapshot)
            self._save_last_snapshot_id(snapshot_id)

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
                snapshot.detail_state = detail_state
            if title is not None:
                snapshot.title = title

            snapshot.updated_at = time.time()
            self._check_snapshot_status(snapshot)
            self._save_snapshot(snapshot)

            return snapshot

    def get_snapshot(self, snapshot_id: str) -> Optional[ReviewSnapshot]:
        with self._lock:
            return self._load_snapshot(snapshot_id)

    def get_snapshot_record(self, snapshot: ReviewSnapshot) -> Optional[ExportRecord]:
        record = self._record_manager.load_record(snapshot.record_id)
        if record:
            return record
        if snapshot.record_snapshot:
            return ExportRecord.from_dict(snapshot.record_snapshot)
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

    def pin_snapshot(self, snapshot_id: str, pinned: bool = True) -> bool:
        with self._lock:
            snapshot = self._load_snapshot(snapshot_id)
            if not snapshot:
                return False
            snapshot.is_pinned = pinned
            snapshot.updated_at = time.time()
            self._save_snapshot(snapshot)
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
            new_snapshots = [s for s in snapshots if s.snapshot_id != snapshot_id]
            if len(new_snapshots) == len(snapshots):
                return False
            self._save_all_snapshots(new_snapshots)

            last_id = self._load_last_snapshot_id()
            if last_id == snapshot_id:
                self._save_last_snapshot_id(None)

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

        record = self._record_manager.load_record(snapshot.record_id)
        if not record:
            issues.append({
                "type": "record_gone",
                "severity": "warning",
                "message": "原始导出记录已被删除，快照中保留了记录副本",
            })
            record_data = snapshot.record_snapshot
            if record_data:
                record = ExportRecord.from_dict(record_data)
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
                    "export_version": "1.0",
                    "exported_at": time.time(),
                    "snapshot_count": len(snapshots),
                    "snapshots": [s.to_dict() for s in snapshots],
                }

                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                tmp_path = Path(output_path).with_suffix(".tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, output_path)

                return True, f"成功导出 {len(snapshots)} 个快照"
            except PermissionError as e:
                return False, f"权限不足，无法写入文件: {e}"
            except OSError as e:
                return False, f"写入文件失败: {e}"

    def import_snapshots(self, input_path: str, conflict_strategy: str = "skip") -> ImportResult:
        result = ImportResult(success=False)

        with self._lock:
            try:
                with open(input_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if not isinstance(data, dict) or "snapshots" not in data:
                    result.messages.append("文件格式错误：缺少 snapshots 字段")
                    result.error_count = 1
                    return result

                imported_data = data["snapshots"]
                existing = self._load_all_snapshots()
                existing_ids = {s.snapshot_id: s for s in existing}

                new_snapshots = []
                for item in imported_data:
                    try:
                        snapshot = ReviewSnapshot.from_dict(item)
                    except (KeyError, ValueError) as e:
                        result.error_count += 1
                        result.messages.append(f"跳过格式错误的快照: {e}")
                        continue

                    if snapshot.snapshot_id in existing_ids:
                        result.conflict_count += 1
                        if conflict_strategy == "skip":
                            result.skipped_count += 1
                            result.messages.append(
                                f"跳过同名快照: {snapshot.snapshot_id} ({snapshot.title[:30]})"
                            )
                            continue
                        elif conflict_strategy == "overwrite":
                            result.messages.append(
                                f"覆盖同名快照: {snapshot.snapshot_id}"
                            )
                        elif conflict_strategy == "rename":
                            new_id = f"{snapshot.snapshot_id}_imported_{int(time.time())}"
                            snapshot.snapshot_id = new_id
                            result.messages.append(
                                f"重命名导入快照: {new_id}"
                            )
                        else:
                            result.skipped_count += 1
                            continue

                    self._check_snapshot_status(snapshot)
                    new_snapshots.append(snapshot)
                    result.imported_count += 1
                    result.imported_ids.append(snapshot.snapshot_id)

                merged = existing + new_snapshots
                merged = merged[-self._max_snapshots:]
                self._save_all_snapshots(merged)

                result.success = True
                result.messages.append(
                    f"导入完成: 成功{result.imported_count}条, "
                    f"跳过{result.skipped_count}条, "
                    f"冲突{result.conflict_count}条, "
                    f"错误{result.error_count}条"
                )
                return result

            except FileNotFoundError:
                result.messages.append("导入文件不存在")
                result.error_count = 1
                return result
            except PermissionError as e:
                result.messages.append(f"权限不足，无法读取文件: {e}")
                result.error_count = 1
                return result
            except json.JSONDecodeError as e:
                result.messages.append(f"JSON 格式错误: {e}")
                result.error_count = 1
                return result

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
            return [ReviewSnapshot.from_dict(d) for d in data]
        except (json.JSONDecodeError, ValueError, KeyError):
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
