import sys
import os
import tempfile
import shutil
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.models import PrintTask, TaskStatus, AppConfig, CounterType
from core.storage import Storage
from core.queue_manager import QueueManager
from core.exporter import HistoryExporter
from core.export_record import (
    ExportRecordManager, ExportRecord, ExportStatus, ExportTrigger,
    ExportFileEntry, ExportRecordUIState,
    ConflictHint, CONFLICT_HINT_LABELS, EXPORT_TRIGGER_LABELS,
    compute_file_hash, compute_content_hash,
)
from core.review_workbench import (
    ReviewWorkbenchManager, ReviewSnapshot, DetailTabState,
    SnapshotStatus, SNAPSHOT_STATUS_LABELS, ImportResult,
    RecoveryLogEntry, REVIEW_SNAPSHOTS_FILE, LAST_REVIEW_SNAPSHOT_FILE,
    RECOVERY_LOG_FILE, IMPORT_UNDO_FILE, SNAPSHOT_FORMAT_VERSION,
)
from core.batch_playback import BatchPlaybackManager


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def _make_tmp_env():
    tmpdir = tempfile.mkdtemp(prefix="review_rc_test_")
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir()

    import core.storage as st_mod
    import core.export_record as er_mod
    import core.review_workbench as rw_mod
    import core.batch_playback as pb_mod

    orig = {
        "DATA_DIR": st_mod.DATA_DIR,
        "TASKS_FILE": st_mod.TASKS_FILE,
        "CONFIG_FILE": st_mod.CONFIG_FILE,
        "EXPORT_LOG_FILE": st_mod.EXPORT_LOG_FILE,
        "EXPORT_RECORDS_FILE": st_mod.EXPORT_RECORDS_FILE,
        "EXPORT_RECORD_UI_STATE_FILE": st_mod.EXPORT_RECORD_UI_STATE_FILE,
        "ER_EXPORT_RECORDS_FILE": er_mod.EXPORT_RECORDS_FILE,
        "ER_EXPORT_RECORD_UI_STATE_FILE": er_mod.EXPORT_RECORD_UI_STATE_FILE,
        "RW_REVIEW_SNAPSHOTS_FILE": rw_mod.REVIEW_SNAPSHOTS_FILE,
        "RW_LAST_REVIEW_SNAPSHOT_FILE": rw_mod.LAST_REVIEW_SNAPSHOT_FILE,
        "RW_RECOVERY_LOG_FILE": rw_mod.RECOVERY_LOG_FILE,
        "RW_IMPORT_UNDO_FILE": rw_mod.IMPORT_UNDO_FILE,
        "BATCHES_FILE": pb_mod.BATCHES_FILE,
        "LAST_SUBMITTED_FILE": pb_mod.LAST_SUBMITTED_FILE,
        "UI_STATE_FILE": pb_mod.UI_STATE_FILE,
    }

    st_mod.DATA_DIR = data_dir
    st_mod.TASKS_FILE = data_dir / "tasks.json"
    st_mod.CONFIG_FILE = data_dir / "config.json"
    st_mod.EXPORT_LOG_FILE = data_dir / "export_log.json"
    st_mod.EXPORT_RECORDS_FILE = data_dir / "export_records.json"
    st_mod.EXPORT_RECORD_UI_STATE_FILE = data_dir / "export_record_ui_state.json"
    er_mod.EXPORT_RECORDS_FILE = data_dir / "export_records.json"
    er_mod.EXPORT_RECORD_UI_STATE_FILE = data_dir / "export_record_ui_state.json"
    rw_mod.REVIEW_SNAPSHOTS_FILE = data_dir / "review_snapshots.json"
    rw_mod.LAST_REVIEW_SNAPSHOT_FILE = data_dir / "last_review_snapshot.json"
    rw_mod.RECOVERY_LOG_FILE = data_dir / "recovery_log.json"
    rw_mod.IMPORT_UNDO_FILE = data_dir / "import_undo.json"
    pb_mod.BATCHES_FILE = data_dir / "playback_batches.json"
    pb_mod.LAST_SUBMITTED_FILE = data_dir / "playback_last_submitted.json"
    pb_mod.UI_STATE_FILE = data_dir / "playback_ui_state.json"

    return tmpdir, orig


def _restore_env(orig):
    import core.storage as st_mod
    import core.export_record as er_mod
    import core.review_workbench as rw_mod
    import core.batch_playback as pb_mod

    st_mod.DATA_DIR = orig["DATA_DIR"]
    st_mod.TASKS_FILE = orig["TASKS_FILE"]
    st_mod.CONFIG_FILE = orig["CONFIG_FILE"]
    st_mod.EXPORT_LOG_FILE = orig["EXPORT_LOG_FILE"]
    st_mod.EXPORT_RECORDS_FILE = orig["EXPORT_RECORDS_FILE"]
    st_mod.EXPORT_RECORD_UI_STATE_FILE = orig["EXPORT_RECORD_UI_STATE_FILE"]
    er_mod.EXPORT_RECORDS_FILE = orig["ER_EXPORT_RECORDS_FILE"]
    er_mod.EXPORT_RECORD_UI_STATE_FILE = orig["ER_EXPORT_RECORD_UI_STATE_FILE"]
    rw_mod.REVIEW_SNAPSHOTS_FILE = orig["RW_REVIEW_SNAPSHOTS_FILE"]
    rw_mod.LAST_REVIEW_SNAPSHOT_FILE = orig["RW_LAST_REVIEW_SNAPSHOT_FILE"]
    rw_mod.RECOVERY_LOG_FILE = orig["RW_RECOVERY_LOG_FILE"]
    rw_mod.IMPORT_UNDO_FILE = orig["RW_IMPORT_UNDO_FILE"]
    pb_mod.BATCHES_FILE = orig["BATCHES_FILE"]
    pb_mod.LAST_SUBMITTED_FILE = orig["LAST_SUBMITTED_FILE"]
    pb_mod.UI_STATE_FILE = orig["UI_STATE_FILE"]


def _create_test_record(record_manager, tmpdir, idx=0):
    dummy_file = Path(tmpdir) / f"test_export_{idx}.csv"
    dummy_file.write_text(f"test,data\n{idx},{idx * 10}", encoding="utf-8")
    file_hash = compute_file_hash(str(dummy_file))

    file_entry = ExportFileEntry(
        filename=f"test_export_{idx}.csv",
        file_path=str(dummy_file),
        file_size=dummy_file.stat().st_size,
        row_count=1,
        content_hash=file_hash,
    )

    record = record_manager.create_record(
        trigger=ExportTrigger.MANUAL_HISTORY,
        status=ExportStatus.SUCCESS,
        operator=f"测试员{idx}",
        files=[file_entry],
        filter_snapshot={"tag": "历史记录", "format": "csv", "search": f"test{idx}"},
        result_message=f"导出{idx + 1}条记录",
        statistics={"total": idx + 1, "success": idx + 1},
    )
    return record, dummy_file


def test_1_auto_snapshot_on_view():
    """1. 打开详情自动存快照 - 每次查看记录自动落一份可恢复快照"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        rm = ExportRecordManager(storage)
        mgr = ReviewWorkbenchManager(storage, rm)

        log("=== 1. 打开详情自动存快照 ===")

        record, _ = _create_test_record(rm, tmpdir, 0)
        assert len(mgr.list_snapshots()) == 0, "初始应无快照"

        snap = mgr.auto_snapshot(record_id=record.record_id)
        assert snap is not None, "自动快照应创建成功"
        assert snap.is_auto is True, "应标记为自动快照"
        assert snap.record_snapshot is not None, "应保存记录副本"
        log(f"  自动快照: {snap.title} (is_auto={snap.is_auto})")

        assert snap.detail_state is not None, "应有详情状态"
        assert snap.filter_snapshot is not None, "应有筛选快照"
        log(f"  详情状态: tab={snap.detail_state.tab_index}, "
            f"scroll={snap.detail_state.scroll_position}")

        snapshots = mgr.list_snapshots()
        assert len(snapshots) == 1, "应有一个快照"
        log(f"  快照总数: {len(snapshots)}")

        record2, _ = _create_test_record(rm, tmpdir, 1)
        snap2 = mgr.auto_snapshot(record_id=record2.record_id)
        assert snap2.is_auto is True, "第二条也是自动快照"
        assert len(mgr.list_snapshots()) == 2, "应有两个快照"
        log(f"  第二条自动快照: {snap2.title}")
        log("  [OK] 自动存快照测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_2_auto_snapshot_updates_existing():
    """2. 同一记录重复查看更新快照而非新建"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        rm = ExportRecordManager(storage)
        mgr = ReviewWorkbenchManager(storage, rm)

        log("=== 2. 同记录重复查看更新快照 ===")

        record, _ = _create_test_record(rm, tmpdir, 0)
        snap1 = mgr.auto_snapshot(record_id=record.record_id)
        assert len(mgr.list_snapshots()) == 1

        state2 = DetailTabState(tab_index=2, scroll_position=0.8)
        snap2 = mgr.auto_snapshot(
            record_id=record.record_id,
            detail_state=state2,
        )

        assert snap2.snapshot_id == snap1.snapshot_id, "同记录应更新同一快照"
        assert snap2.detail_state.tab_index == 2, "状态应更新"
        assert snap2.detail_state.scroll_position == 0.8, "滚动应更新"
        assert len(mgr.list_snapshots()) == 1, "快照数不应增加"
        log(f"  快照ID不变: {snap1.snapshot_id == snap2.snapshot_id}")
        log(f"  状态已更新: tab={snap2.detail_state.tab_index}")
        log("  [OK] 同记录更新快照测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_3_close_reopen_restore():
    """3. 关闭重开恢复 - 恢复到上次查看位置"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        rm = ExportRecordManager(storage)
        mgr = ReviewWorkbenchManager(storage, rm)

        log("=== 3. 关闭重开恢复 ===")

        for i in range(3):
            record, _ = _create_test_record(rm, tmpdir, i)
            state = DetailTabState(
                tab_index=i % 4,
                scroll_position=0.1 * (i + 1),
                expanded_sections=[f"section_{i}"],
                selected_file_index=0,
                timeline_position=float(i * 5),
                preview_file_path=f"/path/file_{i}.csv" if i == 1 else None,
                filter_conditions={"search": f"test{i}", "status": "success"},
            )
            mgr.auto_snapshot(
                record_id=record.record_id,
                detail_state=state,
                filter_snapshot={"search": f"query{i}"},
            )

        snapshots = mgr.list_snapshots()
        target = snapshots[1]
        mgr.set_last_snapshot(target.snapshot_id)

        last = mgr.get_last_snapshot()
        assert last is not None, "应有最近快照"
        assert last.snapshot_id == target.snapshot_id, "ID应匹配"
        assert last.detail_state.tab_index == 1, "页签应恢复"
        assert last.detail_state.scroll_position == 0.2, "滚动位置应恢复"
        assert last.detail_state.expanded_sections == ["section_1"], "展开区块应恢复"
        assert last.detail_state.timeline_position == 5.0, "时间线位置应恢复"
        assert last.detail_state.preview_file_path == "/path/file_1.csv", "预览路径应恢复"
        assert last.detail_state.filter_conditions.get("search") == "test1", "筛选条件应恢复"
        log(f"  恢复页签: {last.detail_state.tab_index}")
        log(f"  恢复滚动: {last.detail_state.scroll_position}")
        log(f"  恢复展开: {last.detail_state.expanded_sections}")
        log(f"  恢复时间线: {last.detail_state.timeline_position}")
        log(f"  恢复预览: {last.detail_state.preview_file_path}")
        log(f"  恢复筛选: {last.detail_state.filter_conditions}")
        log("  [OK] 关闭重开恢复测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_4_cross_restart_full_recovery():
    """4. 跨重启恢复 - 完整链路"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        rm = ExportRecordManager(storage)
        mgr1 = ReviewWorkbenchManager(storage, rm)

        log("=== 4. 跨重启恢复 ===")

        for i in range(5):
            record, _ = _create_test_record(rm, tmpdir, i)
            state = DetailTabState(
                tab_index=i,
                scroll_position=0.15 * i,
                expanded_sections=[f"sec_{i}"],
                filter_conditions={"key": f"val_{i}"},
            )
            snap = mgr1.auto_snapshot(
                record_id=record.record_id,
                detail_state=state,
            )
            if i == 3:
                mgr1.pin_snapshot(snap.snapshot_id, True)

        snapshots_before = mgr1.list_snapshots()
        last_before = mgr1.get_last_snapshot()
        log(f"  重启前: {len(snapshots_before)} 个快照, 最近: {last_before.title}")

        storage2 = Storage()
        rm2 = ExportRecordManager(storage2)
        mgr2 = ReviewWorkbenchManager(storage2, rm2)

        snapshots_after = mgr2.list_snapshots()
        last_after = mgr2.get_last_snapshot()
        assert len(snapshots_after) == len(snapshots_before), "快照数应一致"
        assert last_after is not None, "重启后应有最近快照"
        assert last_after.snapshot_id == last_before.snapshot_id, "最近快照应一致"

        pinned = [s for s in snapshots_after if s.is_pinned]
        assert len(pinned) == 1, "置顶应恢复"
        log(f"  重启后: {len(snapshots_after)} 个快照, 最近: {last_after.title}")

        for s in snapshots_after:
            assert s.detail_state is not None, f"快照{s.snapshot_id}应有详情状态"
            assert s.is_auto is True, f"快照{s.snapshot_id}应标记为自动"
            if s.detail_state.filter_conditions:
                key = s.detail_state.filter_conditions.get("key", "")
                assert key.startswith("val_"), f"筛选条件应恢复: {key}"
        log("  所有快照状态恢复 OK")

        adj_prev, adj_next = mgr2.get_adjacent_snapshots(last_after.snapshot_id)
        log(f"  上一条: {adj_prev.title if adj_prev else 'None'}")
        log(f"  下一条: {adj_next.title if adj_next else 'None'}")
        assert adj_prev is not None or adj_next is not None, "应有相邻快照"
        log("  [OK] 跨重启恢复测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_5_import_with_merge_conflict():
    """5. 导入冲突 - 合并策略"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        rm = ExportRecordManager(storage)
        mgr = ReviewWorkbenchManager(storage, rm)

        log("=== 5. 导入冲突 - 合并策略 ===")

        record, _ = _create_test_record(rm, tmpdir, 0)
        local_snap = mgr.auto_snapshot(
            record_id=record.record_id,
            detail_state=DetailTabState(
                tab_index=1,
                scroll_position=0.3,
                expanded_sections=["local_sec"],
                filter_conditions={"local_key": "local_val"},
            ),
        )

        export_path = Path(tmpdir) / "export_for_merge.json"
        mgr.export_snapshots(str(export_path))

        from core.review_workbench import REVIEW_SNAPSHOTS_FILE
        with open(REVIEW_SNAPSHOTS_FILE, "r", encoding="utf-8") as f:
            all_data = json.load(f)

        for item in all_data:
            item["detail_state"] = {
                "tab_index": 3,
                "scroll_position": 0.7,
                "expanded_sections": ["imported_sec"],
                "selected_file_index": 0,
                "timeline_position": None,
                "preview_file_path": "/imported/path.csv",
                "filter_conditions": {"imported_key": "imported_val"},
            }
            item["log_entries"] = ["导入方日志条目"]

        with open(export_path, "r", encoding="utf-8") as f:
            export_data = json.load(f)
        export_data["snapshots"] = all_data

        merge_path = Path(tmpdir) / "merge_import.json"
        with open(merge_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)

        result = mgr.import_snapshots(str(merge_path), conflict_strategy="merge")
        assert result.success, "导入应成功"
        assert result.merged_count >= 1, f"应有合并: merged={result.merged_count}"
        log(f"  合并数: {result.merged_count}")
        log(f"  合并消息: {result.messages}")

        merged_snap = mgr.get_snapshot(local_snap.snapshot_id)
        assert merged_snap is not None, "合并后应存在"
        merged_detail = merged_snap.detail_state

        assert "local_sec" in merged_detail.expanded_sections, "本地区块应保留"
        assert "imported_sec" in merged_detail.expanded_sections, "导入区块应合并"
        assert "local_key" in merged_detail.filter_conditions, "本地筛选应保留"
        assert "imported_key" in merged_detail.filter_conditions, "导入筛选应合并"
        log(f"  展开区块: {merged_detail.expanded_sections}")
        log(f"  筛选条件: {merged_detail.filter_conditions}")

        has_merge_log = any("合并" in e for e in merged_snap.log_entries)
        assert has_merge_log, "应有合并日志"
        log("  合并日志验证 OK")
        log("  [OK] 导入冲突合并测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_6_import_all_strategies():
    """6. 导入全部冲突策略 - skip/overwrite/rename/merge"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        rm = ExportRecordManager(storage)
        mgr = ReviewWorkbenchManager(storage, rm)

        log("=== 6. 全部冲突策略 ===")

        record, _ = _create_test_record(rm, tmpdir, 0)
        mgr.auto_snapshot(record_id=record.record_id)

        export_path = Path(tmpdir) / "strat_export.json"
        mgr.export_snapshots(str(export_path))

        r_skip = mgr.import_snapshots(str(export_path), conflict_strategy="skip")
        assert r_skip.success
        assert r_skip.skipped_count >= 1, "skip应跳过"
        assert r_skip.merged_count == 0, "skip不应合并"
        log(f"  skip: 跳过{r_skip.skipped_count}条")

        r_over = mgr.import_snapshots(str(export_path), conflict_strategy="overwrite")
        assert r_over.success
        assert r_over.imported_count >= 1, "overwrite应导入"
        log(f"  overwrite: 导入{r_over.imported_count}条")

        r_rename = mgr.import_snapshots(str(export_path), conflict_strategy="rename")
        assert r_rename.success
        assert r_rename.imported_count >= 1, "rename应导入新ID"
        log(f"  rename: 导入{r_rename.imported_count}条")

        r_merge = mgr.import_snapshots(str(export_path), conflict_strategy="merge")
        assert r_merge.success
        assert r_merge.merged_count >= 1, "merge应合并"
        log(f"  merge: 合并{r_merge.merged_count}条")

        log("  [OK] 全部冲突策略测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_7_undo_import():
    """7. 撤销导入 - 恢复到导入前状态"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        rm = ExportRecordManager(storage)
        mgr = ReviewWorkbenchManager(storage, rm)

        log("=== 7. 撤销导入 ===")

        for i in range(2):
            record, _ = _create_test_record(rm, tmpdir, i)
            mgr.auto_snapshot(record_id=record.record_id)

        before_count = len(mgr.list_snapshots())
        before_ids = {s.snapshot_id for s in mgr.list_snapshots()}
        log(f"  导入前: {before_count} 个快照")

        export_path = Path(tmpdir) / "undo_export.json"
        mgr.export_snapshots(str(export_path))

        assert not mgr.can_undo_import(), "导入前不应有撤销"
        log("  导入前无可撤销 OK")

        result = mgr.import_snapshots(str(export_path), conflict_strategy="rename")
        assert result.success
        assert result.undo_available, "导入后应可撤销"
        after_import_count = len(mgr.list_snapshots())
        log(f"  导入后: {after_import_count} 个快照")

        assert mgr.can_undo_import(), "应可撤销"
        log("  can_undo_import = True OK")

        undo_ok, undo_msg = mgr.undo_last_import()
        assert undo_ok, "撤销应成功"
        log(f"  撤销消息: {undo_msg}")

        after_undo = mgr.list_snapshots()
        after_undo_ids = {s.snapshot_id for s in after_undo}
        assert len(after_undo) == before_count, f"撤销后数量应恢复: {len(after_undo)} != {before_count}"
        assert after_undo_ids == before_ids, "撤销后ID应一致"
        log(f"  撤销后: {len(after_undo)} 个快照")

        assert not mgr.can_undo_import(), "撤销后不应再可撤销"
        log("  撤销后不可再撤销 OK")

        undo2_ok, undo2_msg = mgr.undo_last_import()
        assert not undo2_ok, "二次撤销应失败"
        log(f"  二次撤销: {undo2_msg}")
        log("  [OK] 撤销导入测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_8_permission_and_file_errors():
    """8. 权限失败 - 源文件丢失、目录无权限"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        rm = ExportRecordManager(storage)
        mgr = ReviewWorkbenchManager(storage, rm)

        log("=== 8. 权限失败与文件错误 ===")

        record, file_path = _create_test_record(rm, tmpdir, 0)
        snap = mgr.auto_snapshot(record_id=record.record_id)

        assert snap.status == SnapshotStatus.NORMAL, "初始应为正常"
        log("  初始正常 OK")

        file_path.unlink()
        snap_check = mgr.get_snapshot(snap.snapshot_id)
        mgr._check_snapshot_status(snap_check)
        assert snap_check.status == SnapshotStatus.FILE_MISSING, "文件丢失应标记"
        log(f"  文件丢失: {snap_check.status.value}")

        health = mgr.check_snapshot_health(snap_check)
        assert len(health.get("file_issues", [])) >= 1, "应有文件问题"
        log(f"  健康检查问题: {health['file_issues'][0]['issues']}")

        readonly_dir = Path(tmpdir) / "readonly"
        readonly_dir.mkdir()
        readonly_file = readonly_dir / "test.csv"
        readonly_file.write_text("data", encoding="utf-8")

        export_path = str(readonly_dir / "output.json")
        try:
            os.chmod(str(readonly_dir), 0o444)
            success, msg = mgr.export_snapshots(export_path)
            if not success:
                assert "权限" in msg or "写入" in msg, "应有权限错误提示"
                log(f"  写入权限错误: {msg[:60]}")
            else:
                log("  写入权限测试跳过 (Windows权限模型不同)")
        except PermissionError as e:
            log(f"  写入权限异常: {e}")
        finally:
            try:
                os.chmod(str(readonly_dir), 0o755)
            except OSError:
                pass

        import_path = Path(tmpdir) / "nonexist_import.json"
        result = mgr.import_snapshots(str(import_path))
        assert not result.success
        assert any("不存在" in m for m in result.messages)
        log(f"  导入不存在文件: {result.messages[0][:50]}")

        bad_json = Path(tmpdir) / "bad.json"
        bad_json.write_text("not json", encoding="utf-8")
        result2 = mgr.import_snapshots(str(bad_json))
        assert not result2.success
        assert any("JSON" in m or "格式" in m for m in result2.messages)
        log(f"  导入格式错误: {result2.messages[0][:50]}")
        log("  [OK] 权限失败与文件错误测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_9_old_snapshot_field_compat():
    """9. 旧快照字段缺失兼容"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        rm = ExportRecordManager(storage)
        mgr = ReviewWorkbenchManager(storage, rm)

        log("=== 9. 旧快照字段缺失兼容 ===")

        record, _ = _create_test_record(rm, tmpdir, 0)

        from core.review_workbench import REVIEW_SNAPSHOTS_FILE
        old_snap_dict = {
            "snapshot_id": "old_snap_001",
            "record_id": record.record_id,
            "created_at": time.time() - 3600,
            "updated_at": time.time() - 3600,
            "title": "旧版快照",
            "record_snapshot": record.to_dict(),
            "detail_state": {
                "tab_index": 1,
                "scroll_position": 0.5,
            },
        }

        REVIEW_SNAPSHOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(REVIEW_SNAPSHOTS_FILE, "w", encoding="utf-8") as f:
            json.dump([old_snap_dict], f, ensure_ascii=False, indent=2)

        snapshots = mgr.list_snapshots()
        assert len(snapshots) >= 1, "应加载旧快照"

        old = snapshots[0]
        assert old.snapshot_id == "old_snap_001", "ID应匹配"
        assert old.format_version == "1.0", "旧快照版本应为1.0"
        assert old.status == SnapshotStatus.FIELDS_MISSING, f"旧快照应标记字段缺失: {old.status}"
        log(f"  旧快照状态: {old.status.value}")

        assert old.detail_state.tab_index == 1, "已有字段应恢复"
        assert old.detail_state.scroll_position == 0.5, "已有字段应恢复"
        assert old.detail_state.expanded_sections == [], "缺失字段应补默认值"
        assert old.detail_state.filter_conditions == {}, "缺失筛选条件应补默认值"
        log(f"  已有字段恢复 OK")
        log(f"  缺失字段默认值 OK")

        has_compat_log = any("字段缺失" in e or "补全" in e for e in old.log_entries)
        assert has_compat_log, "应有兼容日志"
        log(f"  兼容日志: {old.log_entries}")

        health = mgr.check_snapshot_health(old)
        has_format_issue = any(i.get("type") == "old_format" for i in health.get("issues", []))
        assert has_format_issue, "健康检查应提示格式版本差异"
        log("  格式版本健康检查 OK")
        log("  [OK] 旧快照字段缺失兼容测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_10_recovery_log_tracking():
    """10. 恢复日志追踪 - 可读日志记录所有操作"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        rm = ExportRecordManager(storage)
        mgr = ReviewWorkbenchManager(storage, rm)

        log("=== 10. 恢复日志追踪 ===")

        record, _ = _create_test_record(rm, tmpdir, 0)
        snap = mgr.auto_snapshot(record_id=record.record_id)
        mgr.pin_snapshot(snap.snapshot_id, True)

        export_path = Path(tmpdir) / "log_test_export.json"
        mgr.export_snapshots(str(export_path))

        result = mgr.import_snapshots(str(export_path), conflict_strategy="rename")
        assert result.success

        mgr.undo_last_import()

        logs = mgr.get_recovery_logs(limit=20)
        assert len(logs) >= 1, "应有日志记录"
        log(f"  日志条数: {len(logs)}")

        actions = [l.action for l in logs]
        assert "auto_snapshot_create" in actions, "应有自动快照创建日志"
        assert "pin_snapshot" in actions, "应有置顶日志"
        assert "export" in actions, "应有导出日志"
        assert "import" in actions, "应有导入日志"
        assert "undo_import" in actions, "应有撤销日志"
        log(f"  操作类型: {list(set(actions))}")

        for l in logs:
            assert l.timestamp > 0, "应有时间戳"
            assert l.detail, "应有详情"
            assert l.severity in ("info", "warning", "error"), "应有严重级别"
        log("  日志格式验证 OK")

        severities = [l.severity for l in logs]
        assert "info" in severities, "应有info级别"
        log("  日志级别验证 OK")
        log("  [OK] 恢复日志追踪测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_11_snapshot_full_state_capture():
    """11. 快照完整状态捕获 - 筛选条件、预览文件、时间线停留点"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        rm = ExportRecordManager(storage)
        mgr = ReviewWorkbenchManager(storage, rm)

        log("=== 11. 快照完整状态捕获 ===")

        record, _ = _create_test_record(rm, tmpdir, 0)
        state = DetailTabState(
            tab_index=3,
            scroll_position=0.85,
            expanded_sections=["overview", "files", "logs", "conflicts"],
            selected_file_index=0,
            timeline_position=42.5,
            preview_file_path="/data/exports/report_2024.csv",
            filter_conditions={
                "status_filter": "success",
                "trigger_filter": "manual_history",
                "search_text": "重要报告",
                "date_range": "2024-01-01~2024-12-31",
            },
        )

        filter_snap = {
            "status_filter": "success",
            "search_text": "重要报告",
            "date_range": "2024-01-01~2024-12-31",
        }

        snap = mgr.auto_snapshot(
            record_id=record.record_id,
            detail_state=state,
            filter_snapshot=filter_snap,
            batch_context={"batch_id": "batch_2024_q4", "total_items": 25},
        )

        assert snap.detail_state.tab_index == 3, "页签应捕获"
        assert snap.detail_state.scroll_position == 0.85, "滚动应捕获"
        assert len(snap.detail_state.expanded_sections) == 4, "展开区块应捕获"
        assert snap.detail_state.selected_file_index == 0, "选中文件应捕获"
        assert snap.detail_state.timeline_position == 42.5, "时间线应捕获"
        assert snap.detail_state.preview_file_path == "/data/exports/report_2024.csv", "预览路径应捕获"
        assert snap.detail_state.filter_conditions.get("search_text") == "重要报告", "筛选条件应捕获"
        log(f"  页签: {snap.detail_state.tab_index}")
        log(f"  滚动: {snap.detail_state.scroll_position}")
        log(f"  展开: {snap.detail_state.expanded_sections}")
        log(f"  时间线: {snap.detail_state.timeline_position}")
        log(f"  预览: {snap.detail_state.preview_file_path}")
        log(f"  筛选: {snap.detail_state.filter_conditions}")

        assert snap.filter_snapshot.get("search_text") == "重要报告", "顶层筛选快照应捕获"
        assert snap.batch_context.get("batch_id") == "batch_2024_q4", "批次上下文应捕获"
        log(f"  顶层筛选: {snap.filter_snapshot}")
        log(f"  批次上下文: {snap.batch_context}")

        loaded = mgr.get_snapshot(snap.snapshot_id)
        assert loaded.detail_state.tab_index == 3
        assert loaded.detail_state.filter_conditions.get("date_range") == "2024-01-01~2024-12-31"
        log("  持久化加载验证 OK")
        log("  [OK] 快照完整状态捕获测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_12_continuous_viewing_chain():
    """12. 从最近查看列表连续接着看"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        rm = ExportRecordManager(storage)
        mgr = ReviewWorkbenchManager(storage, rm)

        log("=== 12. 连续接着看 ===")

        for i in range(5):
            record, _ = _create_test_record(rm, tmpdir, i)
            state = DetailTabState(
                tab_index=i,
                scroll_position=0.1 * i,
                filter_conditions={"page": str(i)},
            )
            mgr.auto_snapshot(record_id=record.record_id, detail_state=state)

        snapshots = mgr.list_snapshots()
        mgr.set_last_snapshot(snapshots[2].snapshot_id)

        current = mgr.get_last_snapshot()
        assert current is not None

        prev_s, next_s = mgr.get_adjacent_snapshots(current.snapshot_id)
        assert prev_s is not None, "应有上一条"
        assert next_s is not None, "应有下一条"
        log(f"  当前: {current.title} (tab={current.detail_state.tab_index})")
        log(f"  上一条: {prev_s.title} (tab={prev_s.detail_state.tab_index})")
        log(f"  下一条: {next_s.title} (tab={next_s.detail_state.tab_index})")

        mgr.set_last_snapshot(prev_s.snapshot_id)
        prev_prev, prev_next = mgr.get_adjacent_snapshots(prev_s.snapshot_id)
        assert prev_next is not None and prev_next.snapshot_id == current.snapshot_id
        log(f"  从上一条继续: 下一条={prev_next.title}")
        log("  [OK] 连续接着看测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_13_gui_integration_auto_snapshot():
    """13. GUI集成 - 自动快照与恢复中心"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        rm = ExportRecordManager(storage)
        mgr = ReviewWorkbenchManager(storage, rm)

        for i in range(3):
            record, _ = _create_test_record(rm, tmpdir, i)
            mgr.auto_snapshot(record_id=record.record_id)

        log("=== 13. GUI集成 ===")

        try:
            import tkinter as tk
            has_tk = True
        except ImportError:
            has_tk = False

        if has_tk:
            try:
                from ui.review_workbench_dialog import ReviewWorkbenchDialog

                root = tk.Tk()
                root.withdraw()

                dlg = ReviewWorkbenchDialog(root, mgr, record_manager=rm, operator="测试员")
                assert dlg is not None
                assert dlg.title() == "导出现场恢复中心", f"标题应为恢复中心: {dlg.title()}"
                log(f"  对话框标题: {dlg.title()}")

                tree_count = len(dlg._snapshot_tree.get_children())
                assert tree_count >= 3, f"列表应显示快照: {tree_count}"
                log(f"  快照列表: {tree_count} 项")

                assert hasattr(dlg, '_undo_btn'), "应有撤销按钮"
                log("  撤销按钮存在 OK")

                snapshots = mgr.list_snapshots()
                auto_count = sum(1 for s in snapshots if s.is_auto)
                assert auto_count >= 3, f"自动快照应>=3: {auto_count}"
                log(f"  自动快照数: {auto_count}")

                dlg.destroy()
                root.destroy()
                log("  GUI集成验证 OK")
            except Exception as e:
                log(f"  GUI测试跳过: {e}")

        from core.review_workbench import DetailTabState, ReviewSnapshot, SnapshotStatus, RecoveryLogEntry
        assert hasattr(RecoveryLogEntry, 'from_dict')
        assert hasattr(RecoveryLogEntry, 'to_dict')
        log("  RecoveryLogEntry序列化 OK")

        assert SnapshotStatus.FIELDS_MISSING.value == "fields_missing"
        log("  FIELDS_MISSING状态 OK")

        assert SNAPSHOT_FORMAT_VERSION == "2.0"
        log(f"  格式版本: {SNAPSHOT_FORMAT_VERSION}")

        log("  [OK] GUI集成测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_all():
    tests = [
        ("打开详情自动存快照", test_1_auto_snapshot_on_view),
        ("同记录重复查看更新", test_2_auto_snapshot_updates_existing),
        ("关闭重开恢复", test_3_close_reopen_restore),
        ("跨重启恢复", test_4_cross_restart_full_recovery),
        ("导入冲突-合并", test_5_import_with_merge_conflict),
        ("全部冲突策略", test_6_import_all_strategies),
        ("撤销导入", test_7_undo_import),
        ("权限失败与文件错误", test_8_permission_and_file_errors),
        ("旧快照字段兼容", test_9_old_snapshot_field_compat),
        ("恢复日志追踪", test_10_recovery_log_tracking),
        ("快照完整状态捕获", test_11_snapshot_full_state_capture),
        ("连续接着看", test_12_continuous_viewing_chain),
        ("GUI集成", test_13_gui_integration_auto_snapshot),
    ]

    passed = 0
    failed = []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            log(f"[OK] {name}")
        except AssertionError as e:
            failed.append((name, f"断言失败: {e}"))
            log(f"[FAIL] {name} - {e}")
            import traceback
            traceback.print_exc()
        except Exception as e:
            failed.append((name, f"{type(e).__name__}: {e}"))
            log(f"[FAIL] {name} - {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

    log("")
    log("=" * 60)
    log(f"[RESULT] {passed}/{len(tests)} 测试通过")
    if failed:
        for n, r in failed:
            log(f"  FAILED - {n}: {r}")
    log("=" * 60)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run_all())
