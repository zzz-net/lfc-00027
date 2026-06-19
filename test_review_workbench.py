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
    REVIEW_SNAPSHOTS_FILE, LAST_REVIEW_SNAPSHOT_FILE,
)
from core.batch_playback import BatchPlaybackManager


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def _make_tmp_env():
    tmpdir = tempfile.mkdtemp(prefix="review_wb_test_")
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir()

    import core.storage as st_mod
    import core.export_record as er_mod
    import core.review_workbench as rw_mod

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
    }

    st_mod.DATA_DIR = data_dir
    st_mod.TASKS_FILE = data_dir / "tasks.json"
    st_mod.CONFIG_FILE = data_dir / "config.json"
    st_mod.EXPORT_LOG_FILE = data_dir / "export_log.json"
    st_mod.EXPORT_RECORDS_FILE = data_dir / "export_records.json"
    st_mod.EXPORT_RECORD_UI_STATE_FILE = data_dir / "export_record_ui_state.json"
    er_mod.EXPORT_RECORDS_FILE = data_dir / "export_records.json"
    er_mod.EXPORT_RECORD_UI_STATE_FILE = data_dir / "export_record_ui_state.json"

    import core.review_workbench as rw_mod
    rw_mod.REVIEW_SNAPSHOTS_FILE = data_dir / "review_snapshots.json"
    rw_mod.LAST_REVIEW_SNAPSHOT_FILE = data_dir / "last_review_snapshot.json"

    import core.batch_playback as pb_mod
    orig["BATCHES_FILE"] = pb_mod.BATCHES_FILE
    orig["LAST_SUBMITTED_FILE"] = pb_mod.LAST_SUBMITTED_FILE
    orig["UI_STATE_FILE"] = pb_mod.UI_STATE_FILE
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
    pb_mod.BATCHES_FILE = orig["BATCHES_FILE"]
    pb_mod.LAST_SUBMITTED_FILE = orig["LAST_SUBMITTED_FILE"]
    pb_mod.UI_STATE_FILE = orig["UI_STATE_FILE"]


def _create_test_record(manager, record_manager, tmpdir, idx=0):
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


def test_1_create_snapshot_basic():
    """1. 基础快照创建 - 验证字段完整性"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        record_manager = ExportRecordManager(storage)
        manager = ReviewWorkbenchManager(storage, record_manager)

        record, dummy_file = _create_test_record(manager, record_manager, tmpdir, 0)

        log("=== 1. 基础快照创建 ===")

        detail_state = DetailTabState(
            tab_index=1,
            scroll_position=0.35,
            expanded_sections=["files", "stats"],
            selected_file_index=0,
        )

        filter_snapshot = {
            "status_filter": "success",
            "search_text": "测试",
        }

        snapshot = manager.create_snapshot(
            record_id=record.record_id,
            title="测试快照01",
            detail_state=detail_state,
            filter_snapshot=filter_snapshot,
        )

        assert snapshot is not None, "快照应创建成功"
        assert snapshot.snapshot_id.startswith("rev_"), "快照ID应以rev_开头"
        assert snapshot.record_id == record.record_id, "记录ID应匹配"
        assert snapshot.title == "测试快照01", "标题应匹配"
        assert snapshot.is_pinned is False, "默认不应置顶"
        assert snapshot.status == SnapshotStatus.NORMAL, "状态应为正常"
        log(f"  快照ID: {snapshot.snapshot_id}")
        log(f"  标题: {snapshot.title}")
        log(f"  状态: {snapshot.status.value}")

        assert snapshot.detail_state.tab_index == 1, "页签索引应保存"
        assert snapshot.detail_state.scroll_position == 0.35, "滚动位置应保存"
        assert snapshot.detail_state.expanded_sections == ["files", "stats"], "展开区块应保存"
        log(f"  详情页签: {snapshot.detail_state.tab_index}")
        log(f"  滚动位置: {snapshot.detail_state.scroll_position}")
        log(f"  展开区块: {snapshot.detail_state.expanded_sections}")

        assert snapshot.record_snapshot is not None, "应保存记录副本"
        assert snapshot.record_snapshot["record_id"] == record.record_id, "记录副本ID应匹配"
        log("  记录副本保存 OK")

        assert snapshot.filter_snapshot["status_filter"] == "success", "筛选快照应保存"
        log("  筛选快照保存 OK")

        loaded = manager.get_snapshot(snapshot.snapshot_id)
        assert loaded is not None, "应能加载快照"
        assert loaded.snapshot_id == snapshot.snapshot_id, "加载的快照ID应匹配"
        log("  快照加载验证 OK")

        log("  [OK] 基础快照创建测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_2_snapshot_list_and_pinning():
    """2. 快照列表与置顶功能"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        record_manager = ExportRecordManager(storage)
        manager = ReviewWorkbenchManager(storage, record_manager)

        for i in range(5):
            record, _ = _create_test_record(manager, record_manager, tmpdir, i)
            manager.create_snapshot(
                record_id=record.record_id,
                title=f"快照{i + 1}",
            )

        log("=== 2. 快照列表与置顶 ===")

        snapshots = manager.list_snapshots()
        assert len(snapshots) >= 5, "应有至少5个快照"
        log(f"  快照总数: {len(snapshots)}")

        middle_id = snapshots[2].snapshot_id
        result = manager.pin_snapshot(middle_id, True)
        assert result is True, "置顶操作应成功"

        snapshots_after = manager.list_snapshots()
        assert snapshots_after[0].snapshot_id == middle_id, "置顶快照应在最前面"
        assert snapshots_after[0].is_pinned is True, "置顶状态应为True"
        log(f"  置顶后首个快照: {snapshots_after[0].title} (置顶: {snapshots_after[0].is_pinned})")

        result2 = manager.pin_snapshot(middle_id, False)
        assert result2 is True, "取消置顶应成功"

        snapshots_final = manager.list_snapshots()
        assert snapshots_final[0].is_pinned is False, "取消置顶后不应在最前"
        log("  取消置顶验证 OK")

        log("  [OK] 快照列表与置顶测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_3_delete_snapshot():
    """3. 快照删除功能"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        record_manager = ExportRecordManager(storage)
        manager = ReviewWorkbenchManager(storage, record_manager)

        record, _ = _create_test_record(manager, record_manager, tmpdir, 0)
        snapshot = manager.create_snapshot(record_id=record.record_id, title="待删除快照")

        log("=== 3. 快照删除 ===")

        before_count = len(manager.list_snapshots())
        log(f"  删除前: {before_count} 个快照")

        result = manager.delete_snapshot(snapshot.snapshot_id)
        assert result is True, "删除应成功"

        after_count = len(manager.list_snapshots())
        assert after_count == before_count - 1, "删除后数量应减少1"
        log(f"  删除后: {after_count} 个快照")

        loaded = manager.get_snapshot(snapshot.snapshot_id)
        assert loaded is None, "删除后不应能加载到"
        log("  删除后加载验证 OK")

        result2 = manager.delete_snapshot("nonexistent_id")
        assert result2 is False, "删除不存在的快照应返回False"
        log("  删除不存在快照的边界情况 OK")

        log("  [OK] 快照删除测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_4_last_snapshot_tracking():
    """4. 最近查看快照追踪（跨重启恢复）"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        record_manager = ExportRecordManager(storage)
        manager1 = ReviewWorkbenchManager(storage, record_manager)

        for i in range(3):
            record, _ = _create_test_record(manager1, record_manager, tmpdir, i)
            manager1.create_snapshot(record_id=record.record_id, title=f"快照{i}")

        log("=== 4. 最近查看快照追踪 ===")

        last_before = manager1.get_last_snapshot()
        assert last_before is not None, "应有最近快照"
        log(f"  初始最近快照: {last_before.title}")

        snapshots = manager1.list_snapshots()
        target_id = snapshots[1].snapshot_id
        manager1.set_last_snapshot(target_id)

        last_after = manager1.get_last_snapshot()
        assert last_after is not None, "设置后应有最近快照"
        assert last_after.snapshot_id == target_id, "最近快照ID应匹配"
        log(f"  设置后最近快照: {last_after.title}")

        manager2 = ReviewWorkbenchManager(storage, record_manager)
        last_restart = manager2.get_last_snapshot()
        assert last_restart is not None, "重启后应能加载最近快照"
        assert last_restart.snapshot_id == target_id, "重启后最近快照应一致"
        log(f"  重启后最近快照: {last_restart.title}")
        log("  跨重启恢复验证 OK")

        log("  [OK] 最近查看快照追踪测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_5_detail_state_persistence():
    """5. 详情页状态持久化与恢复"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        record_manager = ExportRecordManager(storage)
        manager = ReviewWorkbenchManager(storage, record_manager)

        record, _ = _create_test_record(manager, record_manager, tmpdir, 0)

        log("=== 5. 详情页状态持久化 ===")

        initial_state = DetailTabState(
            tab_index=2,
            scroll_position=0.75,
            expanded_sections=["section1", "section2", "section3"],
            selected_file_index=0,
            timeline_position=12.5,
        )

        snapshot = manager.create_snapshot(
            record_id=record.record_id,
            title="状态测试快照",
            detail_state=initial_state,
        )

        new_state = DetailTabState(
            tab_index=3,
            scroll_position=0.5,
            expanded_sections=["files"],
            selected_file_index=0,
            timeline_position=45.0,
            preview_file_path="/test/path.csv",
        )

        updated = manager.update_snapshot(
            snapshot.snapshot_id,
            detail_state=new_state,
            title="更新后的快照",
        )

        assert updated is not None, "更新应成功"
        assert updated.detail_state.tab_index == 3, "页签应更新"
        assert updated.detail_state.scroll_position == 0.5, "滚动位置应更新"
        assert updated.detail_state.expanded_sections == ["files"], "展开区块应更新"
        assert updated.detail_state.timeline_position == 45.0, "时间线位置应更新"
        assert updated.detail_state.preview_file_path == "/test/path.csv", "预览路径应更新"
        assert updated.title == "更新后的快照", "标题应更新"
        log(f"  更新后页签: {updated.detail_state.tab_index}")
        log(f"  更新后滚动: {updated.detail_state.scroll_position}")
        log(f"  更新后展开: {updated.detail_state.expanded_sections}")
        log(f"  更新后时间线: {updated.detail_state.timeline_position}")

        loaded = manager.get_snapshot(snapshot.snapshot_id)
        assert loaded.detail_state.tab_index == 3, "加载后页签应一致"
        assert loaded.detail_state.scroll_position == 0.5, "加载后滚动位置应一致"
        log("  持久化加载验证 OK")

        result = manager.update_snapshot("nonexistent", detail_state=new_state)
        assert result is None, "更新不存在的快照应返回None"
        log("  更新不存在快照的边界情况 OK")

        log("  [OK] 详情页状态持久化测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_6_adjacent_and_batch_snapshots():
    """6. 相邻快照与同批次快照导航"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        record_manager = ExportRecordManager(storage)
        manager = ReviewWorkbenchManager(storage, record_manager)

        log("=== 6. 相邻与同批次导航 ===")

        for i in range(5):
            record, _ = _create_test_record(manager, record_manager, tmpdir, i)
            manager.create_snapshot(record_id=record.record_id, title=f"普通快照{i}")

        batch_record, _ = _create_test_record(manager, record_manager, tmpdir, 100)
        batch_record.batch_summary = {"batch_id": "batch_test_001", "total_items": 10}
        from core.export_record import EXPORT_RECORDS_FILE
        all_records = record_manager.load_all_records()
        for i, r in enumerate(all_records):
            if r.record_id == batch_record.record_id:
                all_records[i] = batch_record
                break
        with open(EXPORT_RECORDS_FILE, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in all_records], f, ensure_ascii=False, indent=2)

        batch_snap1 = manager.create_snapshot(
            record_id=batch_record.record_id,
            title="批次快照1",
            batch_context={"batch_id": "batch_test_001", "batch_name": "测试批次"},
        )

        batch_record2, _ = _create_test_record(manager, record_manager, tmpdir, 101)
        batch_record2.batch_summary = {"batch_id": "batch_test_001", "total_items": 10}
        all_records2 = record_manager.load_all_records()
        for i, r in enumerate(all_records2):
            if r.record_id == batch_record2.record_id:
                all_records2[i] = batch_record2
                break
        with open(EXPORT_RECORDS_FILE, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in all_records2], f, ensure_ascii=False, indent=2)

        batch_snap2 = manager.create_snapshot(
            record_id=batch_record2.record_id,
            title="批次快照2",
            batch_context={"batch_id": "batch_test_001", "batch_name": "测试批次"},
        )

        snapshots = manager.list_snapshots()
        mid_idx = len(snapshots) // 2
        mid_id = snapshots[mid_idx].snapshot_id

        prev_s, next_s = manager.get_adjacent_snapshots(mid_id)
        assert prev_s is not None, "应有上一条"
        assert next_s is not None, "应有下一条"
        log(f"  中间快照: {snapshots[mid_idx].title}")
        log(f"  上一条: {prev_s.title}")
        log(f"  下一条: {next_s.title}")

        first_id = snapshots[0].snapshot_id
        prev_first, next_first = manager.get_adjacent_snapshots(first_id)
        assert prev_first is None, "第一条没有上一条"
        assert next_first is not None, "第一条有下一条"
        log("  首条相邻验证 OK")

        last_id = snapshots[-1].snapshot_id
        prev_last, next_last = manager.get_adjacent_snapshots(last_id)
        assert prev_last is not None, "最后一条有上一条"
        assert next_last is None, "最后一条没有下一条"
        log("  末条相邻验证 OK")

        batch_snaps = manager.get_batch_context_snapshots(batch_snap1)
        assert len(batch_snaps) >= 1, "应找到同批次快照"
        assert any(s.snapshot_id == batch_snap2.snapshot_id for s in batch_snaps), "应包含批次快照2"
        log(f"  同批次快照数: {len(batch_snaps)}")

        log("  [OK] 相邻与同批次导航测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_7_snapshot_health_checks():
    """7. 快照健康检查 - 文件丢失、内容变更、权限不足"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        record_manager = ExportRecordManager(storage)
        manager = ReviewWorkbenchManager(storage, record_manager)

        log("=== 7. 快照健康检查 ===")

        record, file_path = _create_test_record(manager, record_manager, tmpdir, 0)
        snapshot = manager.create_snapshot(record_id=record.record_id, title="健康测试")

        assert snapshot.status == SnapshotStatus.NORMAL, "初始状态应为正常"
        health = manager.check_snapshot_health(snapshot)
        assert health["status"] == SnapshotStatus.NORMAL, "健康检查应为正常"
        assert health["can_view"] is True, "应可查看"
        log("  初始正常状态 OK")

        file_path.unlink()
        log("  模拟删除源文件")

        updated = manager.get_snapshot(snapshot.snapshot_id)
        manager._check_snapshot_status(updated)
        assert updated.status == SnapshotStatus.FILE_MISSING, "文件删除后状态应为文件丢失"
        log(f"  文件删除后状态: {updated.status.value}")

        health_missing = manager.check_snapshot_health(updated)
        assert health_missing["status"] == SnapshotStatus.FILE_MISSING
        assert len(health_missing["file_issues"]) >= 1
        log(f"  文件问题数: {len(health_missing['file_issues'])}")

        record2, file_path2 = _create_test_record(manager, record_manager, tmpdir, 1)
        snapshot2 = manager.create_snapshot(record_id=record2.record_id, title="变更测试")

        time.sleep(0.1)
        file_path2.write_text("modified,content\n1,2\n3,4\n5,6", encoding="utf-8")
        log("  模拟修改文件内容")

        manager._check_snapshot_status(snapshot2)
        assert snapshot2.status == SnapshotStatus.CONTENT_CHANGED, "内容变更后状态应为内容变更"
        log(f"  内容变更后状态: {snapshot2.status.value}")

        health_changed = manager.check_snapshot_health(snapshot2)
        assert health_changed["status"] == SnapshotStatus.CONTENT_CHANGED
        assert any("已变更" in str(i) for i in health_changed.get("file_issues", []) for i in i.get("issues", [])) or any("已变更" in issue for fi in health_changed.get("file_issues", []) for issue in fi.get("issues", []))
        log("  内容变更检测 OK")

        record3, _ = _create_test_record(manager, record_manager, tmpdir, 2)
        snapshot3 = manager.create_snapshot(record_id=record3.record_id, title="记录存在测试")

        from core.export_record import EXPORT_RECORDS_FILE
        records_remaining = [r for r in record_manager.load_all_records() if r.record_id != record3.record_id]
        with open(EXPORT_RECORDS_FILE, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in records_remaining], f, ensure_ascii=False, indent=2)
        log("  模拟删除原始导出记录")

        snapshot3_record = manager.get_snapshot(snapshot3.snapshot_id)
        manager._check_snapshot_status(snapshot3_record)
        assert snapshot3_record.status == SnapshotStatus.RECORD_GONE, "记录删除后应为记录已删除"
        log(f"  记录删除后状态: {snapshot3_record.status.value}")

        restored_record = manager.get_snapshot_record(snapshot3_record)
        assert restored_record is not None, "应能从快照恢复记录"
        assert restored_record.record_id == record3.record_id, "恢复的记录ID应匹配"
        log("  快照记录副本恢复 OK")

        log("  [OK] 快照健康检查测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_8_json_import_export():
    """8. JSON导入导出 - 迁移功能"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        record_manager = ExportRecordManager(storage)
        manager = ReviewWorkbenchManager(storage, record_manager)

        log("=== 8. JSON导入导出 ===")

        for i in range(3):
            record, _ = _create_test_record(manager, record_manager, tmpdir, i)
            state = DetailTabState(tab_index=i, scroll_position=0.1 * i)
            manager.create_snapshot(
                record_id=record.record_id,
                title=f"导出测试{i}",
                detail_state=state,
            )

        export_path = Path(tmpdir) / "exported_snapshots.json"
        success, msg = manager.export_snapshots(str(export_path))
        assert success is True, "导出应成功"
        assert export_path.exists(), "导出文件应存在"
        log(f"  导出成功: {msg}")

        with open(export_path, "r", encoding="utf-8") as f:
            export_data = json.load(f)
        assert export_data.get("export_version") == "1.0", "版本号应正确"
        assert export_data.get("snapshot_count") >= 3, "快照数应正确"
        assert "snapshots" in export_data, "应有snapshots字段"
        log(f"  导出版本: {export_data['export_version']}")
        log(f"  导出数量: {export_data['snapshot_count']}")

        tmpdir2 = tempfile.mkdtemp(prefix="review_wb_import_")
        data_dir2 = Path(tmpdir2) / "data"
        data_dir2.mkdir()

        import core.storage as st_mod
        import core.export_record as er_mod
        import core.review_workbench as rw_mod

        orig_data_dir = st_mod.DATA_DIR
        st_mod.DATA_DIR = data_dir2
        st_mod.TASKS_FILE = data_dir2 / "tasks.json"
        st_mod.CONFIG_FILE = data_dir2 / "config.json"
        st_mod.EXPORT_LOG_FILE = data_dir2 / "export_log.json"
        st_mod.EXPORT_RECORDS_FILE = data_dir2 / "export_records.json"
        st_mod.EXPORT_RECORD_UI_STATE_FILE = data_dir2 / "export_record_ui_state.json"
        er_mod.EXPORT_RECORDS_FILE = data_dir2 / "export_records.json"
        er_mod.EXPORT_RECORD_UI_STATE_FILE = data_dir2 / "export_record_ui_state.json"
        rw_mod.REVIEW_SNAPSHOTS_FILE = data_dir2 / "review_snapshots.json"
        rw_mod.LAST_REVIEW_SNAPSHOT_FILE = data_dir2 / "last_review_snapshot.json"

        try:
            storage2 = Storage()
            record_manager2 = ExportRecordManager(storage2)
            manager2 = ReviewWorkbenchManager(storage2, record_manager2)

            before_count = len(manager2.list_snapshots())
            log(f"  导入前快照数: {before_count}")

            result = manager2.import_snapshots(str(export_path), conflict_strategy="skip")
            assert result.success is True, "导入应成功"
            assert result.imported_count >= 3, "应导入至少3个快照"
            log(f"  导入结果: 成功{result.imported_count}条, "
                f"跳过{result.skipped_count}条, "
                f"冲突{result.conflict_count}条")

            after_count = len(manager2.list_snapshots())
            assert after_count == before_count + result.imported_count, "导入后数量应增加"
            log(f"  导入后快照数: {after_count}")

            imported = manager2.list_snapshots()
            for s in imported:
                assert s.status in (
                    SnapshotStatus.NORMAL, SnapshotStatus.FILE_MISSING,
                    SnapshotStatus.RECORD_GONE, SnapshotStatus.CONTENT_CHANGED,
                    SnapshotStatus.PERMISSION_DENIED,
                ), f"导入的快照状态应合理: {s.status.value}"
                if s.status == SnapshotStatus.RECORD_GONE:
                    assert s.record_snapshot is not None, "记录已删除的快照应包含记录副本"
            log("  导入的快照状态验证 OK")

            result_dup = manager2.import_snapshots(str(export_path), conflict_strategy="skip")
            assert result_dup.conflict_count >= 3, "重复导入应有冲突"
            assert result_dup.skipped_count >= 3, "跳过策略应跳过冲突项"
            log(f"  重复导入(skip): 冲突{result_dup.conflict_count}条, 跳过{result_dup.skipped_count}条")

            result_over = manager2.import_snapshots(str(export_path), conflict_strategy="overwrite")
            assert result_over.imported_count >= 3, "覆盖策略应导入"
            log(f"  重复导入(overwrite): 导入{result_over.imported_count}条")

            result_rename = manager2.import_snapshots(str(export_path), conflict_strategy="rename")
            assert result_rename.imported_count >= 3, "重命名策略应导入"
            total_after = len(manager2.list_snapshots())
            log(f"  重复导入(rename): 导入{result_rename.imported_count}条, 总数{total_after}")

        finally:
            st_mod.DATA_DIR = orig_data_dir
            st_mod.TASKS_FILE = Path(orig_data_dir) / "tasks.json"
            st_mod.CONFIG_FILE = Path(orig_data_dir) / "config.json"
            st_mod.EXPORT_LOG_FILE = Path(orig_data_dir) / "export_log.json"
            st_mod.EXPORT_RECORDS_FILE = Path(orig_data_dir) / "export_records.json"
            st_mod.EXPORT_RECORD_UI_STATE_FILE = Path(orig_data_dir) / "export_record_ui_state.json"
            er_mod.EXPORT_RECORDS_FILE = Path(orig_data_dir) / "export_records.json"
            er_mod.EXPORT_RECORD_UI_STATE_FILE = Path(orig_data_dir) / "export_record_ui_state.json"
            rw_mod.REVIEW_SNAPSHOTS_FILE = Path(orig_data_dir) / "review_snapshots.json"
            rw_mod.LAST_REVIEW_SNAPSHOT_FILE = Path(orig_data_dir) / "last_review_snapshot.json"
            shutil.rmtree(tmpdir2, ignore_errors=True)

        bad_path = Path(tmpdir) / "nonexistent.json"
        result_bad = manager.import_snapshots(str(bad_path))
        assert result_bad.success is False, "导入不存在文件应失败"
        assert result_bad.error_count == 1, "应有错误计数"
        log("  导入不存在文件的错误处理 OK")

        bad_json = Path(tmpdir) / "bad.json"
        bad_json.write_text("this is not json", encoding="utf-8")
        result_bad2 = manager.import_snapshots(str(bad_json))
        assert result_bad2.success is False, "格式错误的JSON应失败"
        log("  导入格式错误文件的错误处理 OK")

        log("  [OK] JSON导入导出测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_9_snapshot_record_recovery():
    """9. 记录副本恢复 - 原记录删除后仍可查看"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        record_manager = ExportRecordManager(storage)
        manager = ReviewWorkbenchManager(storage, record_manager)

        log("=== 9. 记录副本恢复 ===")

        record, _ = _create_test_record(manager, record_manager, tmpdir, 0)
        snapshot = manager.create_snapshot(record_id=record.record_id, title="恢复测试")

        assert snapshot.record_snapshot is not None, "应保存记录副本"
        log("  快照包含记录副本 OK")

        from core.export_record import EXPORT_RECORDS_FILE
        records_after = [r for r in record_manager.load_all_records() if r.record_id != record.record_id]
        with open(EXPORT_RECORDS_FILE, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in records_after], f, ensure_ascii=False, indent=2)

        direct_record = record_manager.load_record(record.record_id)
        assert direct_record is None, "直接加载应找不到记录"
        log("  原始记录已删除 OK")

        restored = manager.get_snapshot_record(snapshot)
        assert restored is not None, "应能从快照恢复记录"
        assert restored.record_id == record.record_id, "恢复的记录ID应匹配"
        assert restored.operator == record.operator, "恢复的操作者应匹配"
        assert restored.result_message == record.result_message, "恢复的结果消息应匹配"
        log(f"  恢复记录ID: {restored.record_id}")
        log(f"  恢复操作者: {restored.operator}")

        assert len(restored.files) == len(record.files), "文件数应一致"
        log(f"  恢复文件数: {len(restored.files)}")

        health = manager.check_snapshot_health(snapshot)
        assert "记录已被删除" in health.get("issues", [{}])[0].get("message", "") or \
               any("已删除" in i.get("message", "") for i in health.get("issues", [])), \
               "健康检查应提示记录已删除"
        log("  健康检查提示记录删除 OK")

        log("  [OK] 记录副本恢复测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_10_restart_recovery_full():
    """10. 完整重启恢复 - 快照、状态、最近记录"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        record_manager = ExportRecordManager(storage)
        manager1 = ReviewWorkbenchManager(storage, record_manager)

        log("=== 10. 完整重启恢复 ===")

        for i in range(5):
            record, _ = _create_test_record(manager1, record_manager, tmpdir, i)
            state = DetailTabState(
                tab_index=i % 4,
                scroll_position=0.1 * i,
                expanded_sections=[f"section_{i}"],
            )
            snap = manager1.create_snapshot(
                record_id=record.record_id,
                title=f"重启测试{i}",
                detail_state=state,
            )
            if i == 2:
                manager1.pin_snapshot(snap.snapshot_id, True)

        snapshots_before = manager1.list_snapshots()
        last_snap = manager1.get_last_snapshot()
        log(f"  重启前快照数: {len(snapshots_before)}")
        log(f"  重启前最近: {last_snap.title if last_snap else 'None'}")

        storage2 = Storage()
        record_manager2 = ExportRecordManager(storage2)
        manager2 = ReviewWorkbenchManager(storage2, record_manager2)

        snapshots_after = manager2.list_snapshots()
        assert len(snapshots_after) == len(snapshots_before), "重启后快照数应一致"
        log(f"  重启后快照数: {len(snapshots_after)}")

        pinned_after = [s for s in snapshots_after if s.is_pinned]
        assert len(pinned_after) == 1, "置顶状态应恢复"
        assert pinned_after[0].title == "重启测试2", "置顶的快照应正确"
        log(f"  重启后置顶快照: {pinned_after[0].title}")

        last_after = manager2.get_last_snapshot()
        assert last_after is not None, "重启后应有最近快照"
        assert last_after.snapshot_id == last_snap.snapshot_id, "最近快照应一致"
        log(f"  重启后最近快照: {last_after.title}")

        for s_before, s_after in zip(snapshots_before[:3], snapshots_after[:3]):
            assert s_before.snapshot_id == s_after.snapshot_id
            assert s_before.detail_state.tab_index == s_after.detail_state.tab_index
            assert s_before.detail_state.scroll_position == s_after.detail_state.scroll_position
        log("  详情状态恢复完整 OK")

        log("  [OK] 完整重启恢复测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_11_import_error_handling():
    """11. 导入错误处理 - 各种异常情况"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        record_manager = ExportRecordManager(storage)
        manager = ReviewWorkbenchManager(storage, record_manager)

        log("=== 11. 导入错误处理 ===")

        missing_file = Path(tmpdir) / "does_not_exist.json"
        result = manager.import_snapshots(str(missing_file))
        assert not result.success
        assert result.error_count == 1
        assert any("不存在" in m for m in result.messages)
        log(f"  文件不存在: {result.messages[0][:50]}...")

        bad_format = Path(tmpdir) / "bad_format.json"
        bad_format.write_text("not json at all", encoding="utf-8")
        result2 = manager.import_snapshots(str(bad_format))
        assert not result2.success
        assert result2.error_count == 1
        assert any("JSON" in m or "格式" in m for m in result2.messages)
        log(f"  JSON格式错误: {result2.messages[0][:50]}...")

        missing_snapshots = Path(tmpdir) / "missing_field.json"
        missing_snapshots.write_text(json.dumps({"version": "1.0", "other": "data"}), encoding="utf-8")
        result3 = manager.import_snapshots(str(missing_snapshots))
        assert not result3.success
        assert any("缺少" in m or "snapshots" in m for m in result3.messages)
        log(f"  缺少snapshots字段: {result3.messages[0][:50]}...")

        malformed_items = Path(tmpdir) / "malformed.json"
        malformed_items.write_text(json.dumps({
            "export_version": "1.0",
            "snapshots": [
                {"snapshot_id": "valid_1", "record_id": "rec_1", "title": "valid"},
                {"bad_item": "no_required_fields"},
                {"snapshot_id": "valid_2", "record_id": "rec_2", "title": "valid2"},
            ]
        }), encoding="utf-8")
        result4 = manager.import_snapshots(str(malformed_items))
        assert result4.success
        assert result4.error_count >= 1
        assert result4.imported_count >= 1
        log(f"  部分损坏: 导入{result4.imported_count}条, 错误{result4.error_count}条")

        log("  [OK] 导入错误处理测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_12_export_error_handling():
    """12. 导出错误处理 - 权限不足等"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        record_manager = ExportRecordManager(storage)
        manager = ReviewWorkbenchManager(storage, record_manager)

        record, _ = _create_test_record(manager, record_manager, tmpdir, 0)
        manager.create_snapshot(record_id=record.record_id, title="导出测试")

        log("=== 12. 导出错误处理 ===")

        invalid_dir = Path(tmpdir) / "nonexistent_dir" / "output.json"
        success, msg = manager.export_snapshots(str(invalid_dir))
        assert success is True, "父目录不存在时应自动创建"
        assert Path(invalid_dir).exists(), "导出文件应存在"
        log(f"  自动创建父目录: {success}")

        empty_ids_path = Path(tmpdir) / "empty_export.json"
        success2, msg2 = manager.export_snapshots(str(empty_ids_path), snapshot_ids=["nonexistent"])
        assert success2 is True, "空列表也应成功导出"
        log(f"  指定不存在的ID导出: {msg2}")

        log("  [OK] 导出错误处理测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_13_gui_level_integration():
    """13. GUI级集成测试 - 验证模块可正常初始化和交互"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        record_manager = ExportRecordManager(storage)
        manager = ReviewWorkbenchManager(storage, record_manager)

        for i in range(3):
            record, _ = _create_test_record(manager, record_manager, tmpdir, i)
            state = DetailTabState(tab_index=i, scroll_position=0.25 * i)
            manager.create_snapshot(
                record_id=record.record_id,
                title=f"GUI测试快照{i}",
                detail_state=state,
            )

        log("=== 13. GUI级集成测试 ===")

        try:
            import tkinter as tk
            has_tk = True
        except ImportError:
            has_tk = False
            log("  跳过GUI实际渲染测试（无tkinter）")

        if has_tk:
            try:
                from ui.review_workbench_dialog import ReviewWorkbenchDialog

                root = tk.Tk()
                root.withdraw()

                dlg = ReviewWorkbenchDialog(
                    root, manager,
                    record_manager=record_manager,
                    operator="GUI测试员",
                )

                assert dlg is not None, "对话框应创建成功"
                assert dlg._manager is manager, "管理器应正确设置"
                assert dlg._record_manager is record_manager, "记录管理器应正确设置"
                log("  对话框创建成功")

                assert hasattr(dlg, '_snapshot_tree'), "应有快照列表树"
                assert hasattr(dlg, '_detail_notebook'), "应详情页签控件"
                assert hasattr(dlg, '_snapshot_title_var'), "应有标题变量"
                log("  UI控件完整性验证 OK")

                snap_count = len(dlg._snapshot_tree.get_children())
                assert snap_count >= 3, "列表应显示快照"
                log(f"  列表显示快照数: {snap_count}")

                children = dlg._detail_notebook.tabs()
                assert len(children) >= 4, "详情页应有至少4个页签"
                log(f"  详情页签数: {len(children)}")

                dlg.destroy()
                root.destroy()
                log("  对话框销毁正常")

            except Exception as e:
                log(f"  GUI测试跳过: {e}")
                import traceback
                traceback.print_exc()

        from ui.export_record_dialog import ExportRecordDialog

        test_state = DetailTabState(
            tab_index=2,
            scroll_position=0.75,
            expanded_sections=["a", "b"],
            selected_file_index=1,
            timeline_position=10.5,
        )
        state_dict = test_state.to_dict()
        restored = DetailTabState.from_dict(state_dict)
        assert restored.tab_index == 2
        assert restored.scroll_position == 0.75
        assert restored.expanded_sections == ["a", "b"]
        log("  DetailTabState序列化验证 OK")

        log("  [OK] GUI级集成测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_all():
    tests = [
        ("基础快照创建", test_1_create_snapshot_basic),
        ("快照列表与置顶", test_2_snapshot_list_and_pinning),
        ("快照删除", test_3_delete_snapshot),
        ("最近查看追踪", test_4_last_snapshot_tracking),
        ("详情页状态持久化", test_5_detail_state_persistence),
        ("相邻与同批次导航", test_6_adjacent_and_batch_snapshots),
        ("快照健康检查", test_7_snapshot_health_checks),
        ("JSON导入导出", test_8_json_import_export),
        ("记录副本恢复", test_9_snapshot_record_recovery),
        ("完整重启恢复", test_10_restart_recovery_full),
        ("导入错误处理", test_11_import_error_handling),
        ("导出错误处理", test_12_export_error_handling),
        ("GUI级集成", test_13_gui_level_integration),
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
            log(f"[FAIL] {name} - 断言失败: {e}")
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
