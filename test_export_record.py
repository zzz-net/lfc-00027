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
from core.batch_playback import BatchPlaybackManager
from core.export_record import (
    ExportRecordManager, ExportRecord, ExportStatus, ExportTrigger,
    ExportFileEntry, ExportRecordUIState,
    ConflictHint, CONFLICT_HINT_LABELS, EXPORT_TRIGGER_LABELS,
    compute_file_hash, compute_content_hash,
    EXPORT_RECORDS_FILE, EXPORT_RECORD_UI_STATE_FILE,
)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def _make_tmp_env():
    tmpdir = tempfile.mkdtemp(prefix="export_record_test_")
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir()

    import core.storage as st_mod
    import core.export_record as er_mod

    orig = {
        "DATA_DIR": st_mod.DATA_DIR,
        "TASKS_FILE": st_mod.TASKS_FILE,
        "CONFIG_FILE": st_mod.CONFIG_FILE,
        "EXPORT_LOG_FILE": st_mod.EXPORT_LOG_FILE,
        "EXPORT_RECORDS_FILE": st_mod.EXPORT_RECORDS_FILE,
        "EXPORT_RECORD_UI_STATE_FILE": st_mod.EXPORT_RECORD_UI_STATE_FILE,
        "ER_EXPORT_RECORDS_FILE": er_mod.EXPORT_RECORDS_FILE,
        "ER_EXPORT_RECORD_UI_STATE_FILE": er_mod.EXPORT_RECORD_UI_STATE_FILE,
    }

    st_mod.DATA_DIR = data_dir
    st_mod.TASKS_FILE = data_dir / "tasks.json"
    st_mod.CONFIG_FILE = data_dir / "config.json"
    st_mod.EXPORT_LOG_FILE = data_dir / "export_log.json"
    st_mod.EXPORT_RECORDS_FILE = data_dir / "export_records.json"
    st_mod.EXPORT_RECORD_UI_STATE_FILE = data_dir / "export_record_ui_state.json"
    er_mod.EXPORT_RECORDS_FILE = data_dir / "export_records.json"
    er_mod.EXPORT_RECORD_UI_STATE_FILE = data_dir / "export_record_ui_state.json"

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
    import core.batch_playback as pb_mod

    st_mod.DATA_DIR = orig["DATA_DIR"]
    st_mod.TASKS_FILE = orig["TASKS_FILE"]
    st_mod.CONFIG_FILE = orig["CONFIG_FILE"]
    st_mod.EXPORT_LOG_FILE = orig["EXPORT_LOG_FILE"]
    st_mod.EXPORT_RECORDS_FILE = orig["EXPORT_RECORDS_FILE"]
    st_mod.EXPORT_RECORD_UI_STATE_FILE = orig["EXPORT_RECORD_UI_STATE_FILE"]
    er_mod.EXPORT_RECORDS_FILE = orig["ER_EXPORT_RECORDS_FILE"]
    er_mod.EXPORT_RECORD_UI_STATE_FILE = orig["ER_EXPORT_RECORD_UI_STATE_FILE"]
    pb_mod.BATCHES_FILE = orig["BATCHES_FILE"]
    pb_mod.LAST_SUBMITTED_FILE = orig["LAST_SUBMITTED_FILE"]
    pb_mod.UI_STATE_FILE = orig["UI_STATE_FILE"]


def test_1_export_success_record():
    """1. 导出成功 - 记录创建与字段验证"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        queue = QueueManager(storage, config)

        tasks = [
            PrintTask.create(filename="成功1.pdf", copies=2, counter=CounterType.A),
            PrintTask.create(filename="成功2.pdf", copies=3, counter=CounterType.B),
        ]
        queue.add_tasks(tasks)
        for t in queue.tasks:
            t.status = TaskStatus.COMPLETED

        exporter = HistoryExporter(storage)
        out_dir = Path(tmpdir) / "exports"
        out_dir.mkdir()

        result = exporter.export_history(queue.tasks, str(out_dir), operator="测试员", fmt="csv")

        log("=== 1. 导出成功记录 ===")

        assert result.success, f"导出应成功: {result.message}"
        assert result.count == 2
        log(f"  导出结果: {result.message}")

        manager = ExportRecordManager(storage)
        records = manager.load_all_records()

        assert len(records) >= 1, "应有至少1条导出记录"
        record = records[-1]

        assert record.status == ExportStatus.SUCCESS, f"状态应为成功: {record.status.value}"
        assert record.trigger == ExportTrigger.MANUAL_HISTORY, f"触发类型应为手动历史: {record.trigger.value}"
        assert record.operator == "测试员", f"操作者应为测试员: {record.operator}"
        assert record.result_message != "", "应有结果消息"
        log(f"  记录ID: {record.record_id}")
        log(f"  状态: {record.status.value}")
        log(f"  触发: {EXPORT_TRIGGER_LABELS.get(record.trigger, record.trigger.value)}")
        log(f"  操作者: {record.operator}")
        log(f"  文件数: {len(record.files)}")

        assert len(record.files) >= 1, "应有文件条目"
        file_entry = record.files[0]
        assert file_entry.filename != "", "文件名不应为空"
        assert file_entry.file_path != "", "文件路径不应为空"
        assert file_entry.file_size > 0, "文件大小应大于0"
        assert file_entry.row_count == 2, f"行数应为2: {file_entry.row_count}"
        assert file_entry.content_hash != "", "应有内容哈希"
        log(f"  文件: {file_entry.filename} ({file_entry.file_size}B)")

        assert record.filter_snapshot != {}, "应有筛选条件快照"
        assert record.content_hash != "", "应有内容哈希"
        assert record.version_tag != "", "应有版本标记"
        log("  筛选快照、哈希、版本标记 OK")

        log("  [OK] 导出成功记录测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_2_export_failure_record():
    """2. 导出失败 - 权限不足记录"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)

        manager = ExportRecordManager(storage)

        record = manager.create_record(
            trigger=ExportTrigger.MANUAL_HISTORY,
            status=ExportStatus.FAILED,
            operator="失败测试员",
            filter_snapshot={"tag": "历史记录", "format": "csv"},
            failure_reason="权限不足: [Errno 13] Permission denied",
            result_message="导出失败：权限不足",
        )

        log("=== 2. 导出失败记录 ===")

        assert record.status == ExportStatus.FAILED
        assert record.failure_reason != "", "应有失败原因"
        assert "权限" in record.failure_reason, "失败原因应包含权限"
        log(f"  失败原因: {record.failure_reason}")

        loaded = manager.load_record(record.record_id)
        assert loaded is not None, "应能加载失败记录"
        assert loaded.status == ExportStatus.FAILED
        assert loaded.failure_reason == record.failure_reason
        log("  失败记录持久化与加载 OK")

        log("  [OK] 导出失败记录测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_3_duplicate_export_conflict():
    """3. 重复导出 - 同批次重复导出冲突检测"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        queue = QueueManager(storage, config)

        tasks = [
            PrintTask.create(filename="重复.pdf", copies=1, counter=CounterType.A),
        ]
        queue.add_tasks(tasks)
        for t in queue.tasks:
            t.status = TaskStatus.COMPLETED

        manager = BatchPlaybackManager(storage)

        csv_content = "filename,copies,counter\n冲突文件.pdf,1,A类柜台\n"
        test_csv = Path(tmpdir) / "dup_test.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")
        batch = manager.create_batch(str(test_csv), operator="重复导出测试员")

        out_dir = Path(tmpdir) / "exports1"
        out_dir.mkdir()
        audit_path1 = manager.export_audit_package(batch, str(out_dir), operator="导出员1")
        assert Path(audit_path1).exists()
        log(f"  第一次审计包导出: {Path(audit_path1).name}")

        out_dir2 = Path(tmpdir) / "exports2"
        out_dir2.mkdir()
        audit_path2 = manager.export_audit_package(batch, str(out_dir2), operator="导出员2")
        assert Path(audit_path2).exists()
        log(f"  第二次审计包导出: {Path(audit_path2).name}")

        log("=== 3. 重复导出冲突检测 ===")

        record_manager = ExportRecordManager(storage)
        records = record_manager.load_all_records()
        audit_records = [r for r in records if r.trigger == ExportTrigger.AUDIT_PACKAGE]

        assert len(audit_records) >= 2, "应有至少2条审计包导出记录"

        second_record = audit_records[-1]
        assert second_record.conflict_hint == ConflictHint.DUPLICATE_BATCH, \
            f"第二次导出应检测到同批次重复: {second_record.conflict_hint.value}"
        assert second_record.batch_summary.get("batch_id") is not None, \
            "批次摘要应包含batch_id"
        log(f"  冲突检测: {CONFLICT_HINT_LABELS[second_record.conflict_hint]}")
        log(f"  冲突详情: {second_record.conflict_detail}")

        assert len(second_record.log_entries) >= 1, "应有冲突日志"
        assert any("冲突" in e for e in second_record.log_entries), "日志应包含冲突信息"
        log("  冲突日志记录 OK")

        log("  [OK] 重复导出冲突检测测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_4_permission_error_record():
    """4. 权限不足导出 - 报错记录与可读日志"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)

        manager = ExportRecordManager(storage)

        record = manager.create_record(
            trigger=ExportTrigger.MANUAL_ALL,
            status=ExportStatus.FAILED,
            operator="权限测试员",
            filter_snapshot={"tag": "全部任务", "format": "csv"},
            failure_reason="权限不足: [Errno 13] Permission denied: '/readonly/output.csv'",
            result_message="导出失败：权限不足",
        )

        log("=== 4. 权限不足导出 ===")

        assert record.status == ExportStatus.FAILED
        assert "权限" in record.failure_reason
        log(f"  失败原因: {record.failure_reason}")
        log(f"  结果消息: {record.result_message}")

        loaded = manager.load_record(record.record_id)
        assert loaded is not None
        assert loaded.failure_reason == record.failure_reason
        assert loaded.result_message == record.result_message

        records = manager.query_records(status_filter=ExportStatus.FAILED)
        assert len(records) >= 1, "按失败状态筛选应有结果"
        assert all(r.status == ExportStatus.FAILED for r in records)
        log("  按状态筛选失败记录 OK")

        search_results = manager.query_records(search_text="权限")
        assert len(search_results) >= 1, "搜索'权限'应有结果"
        log("  搜索'权限' OK")

        log("  [OK] 权限不足导出测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_5_restart_recovery():
    """5. 重启恢复 - 导出记录、UI状态、筛选条件恢复"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        queue = QueueManager(storage, config)

        tasks = [
            PrintTask.create(filename="恢复测试.pdf", copies=1, counter=CounterType.C),
        ]
        queue.add_tasks(tasks)
        for t in queue.tasks:
            t.status = TaskStatus.COMPLETED

        exporter = HistoryExporter(storage)
        out_dir = Path(tmpdir) / "exports"
        out_dir.mkdir()
        exporter.export_history(queue.tasks, str(out_dir), operator="重启测试员", fmt="json")

        manager1 = ExportRecordManager(storage)
        ui_state = ExportRecordUIState(
            selected_status_filter="success",
            selected_trigger_filter="manual_history",
            search_text="恢复测试",
            last_viewed_record_id="test_id_placeholder",
            scroll_position=42,
        )
        manager1.save_ui_state(ui_state)

        log("=== 5. 重启恢复 ===")

        records_before = manager1.load_all_records()
        assert len(records_before) >= 1
        log(f"  重启前记录数: {len(records_before)}")

        manager2 = ExportRecordManager(storage)
        records_after = manager2.load_all_records()
        assert len(records_after) == len(records_before), "重启后记录数应相同"
        log(f"  重启后记录数: {len(records_after)}")

        for rb, ra in zip(records_before, records_after):
            assert rb.record_id == ra.record_id
            assert rb.status == ra.status
            assert rb.trigger == ra.trigger
            assert rb.operator == ra.operator
            assert rb.result_message == ra.result_message
            assert rb.content_hash == ra.content_hash
            assert rb.version_tag == ra.version_tag
        log("  导出记录恢复完整 OK")

        restored_ui = manager2.load_ui_state()
        assert restored_ui.selected_status_filter == "success", \
            f"状态筛选应恢复: {restored_ui.selected_status_filter}"
        assert restored_ui.selected_trigger_filter == "manual_history", \
            f"类型筛选应恢复: {restored_ui.selected_trigger_filter}"
        assert restored_ui.search_text == "恢复测试", \
            f"搜索文本应恢复: {restored_ui.search_text}"
        assert restored_ui.scroll_position == 42, \
            f"滚动位置应恢复: {restored_ui.scroll_position}"
        log("  UI状态恢复 OK")

        success_records = manager2.query_records(status_filter=ExportStatus.SUCCESS)
        assert len(success_records) >= 1
        log(f"  按成功状态筛选: {len(success_records)} 条")

        history_records = manager2.query_records(trigger_filter=ExportTrigger.MANUAL_HISTORY)
        assert len(history_records) >= 1
        log(f"  按手动历史类型筛选: {len(history_records)} 条")

        search_records = manager2.query_records(search_text="重启")
        assert len(search_records) >= 1
        log(f"  搜索'重启': {len(search_records)} 条")

        log("  [OK] 重启恢复测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_6_file_deleted_conflict():
    """6. 文件删除冲突 - 原文件被删后检测"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)

        manager = ExportRecordManager(storage)

        dummy_file = Path(tmpdir) / "deleted_test.csv"
        dummy_file.write_text("test,data\n1,2", encoding="utf-8")
        file_hash = compute_file_hash(str(dummy_file))

        file_entry = ExportFileEntry(
            filename="deleted_test.csv",
            file_path=str(dummy_file),
            file_size=100,
            row_count=1,
            content_hash=file_hash,
        )

        record = manager.create_record(
            trigger=ExportTrigger.MANUAL_HISTORY,
            status=ExportStatus.SUCCESS,
            operator="删除测试员",
            files=[file_entry],
            result_message="导出1条记录",
        )

        assert record.status == ExportStatus.SUCCESS
        log("  导出记录创建 OK（文件还在）")

        dummy_file.unlink()
        log("  模拟删除导出文件")

        loaded = manager.load_record(record.record_id)
        conflicts = manager.check_file_conflicts(loaded)
        assert len(conflicts) >= 1, "应有冲突"
        assert any("删除" in issue or "不存在" in issue for c in conflicts for issue in c["issues"]), \
            "应有文件已删除提示"
        log(f"  冲突检测: {conflicts[0]['issues']}")

        log("=== 6. 文件删除冲突 ===")
        log("  [OK] 文件删除冲突测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_7_content_changed_conflict():
    """7. 文件内容变更冲突 - 导出后文件被修改"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)

        manager = ExportRecordManager(storage)

        changed_file = Path(tmpdir) / "changed_test.csv"
        changed_file.write_text("original,data\n1,2", encoding="utf-8")
        file_hash = compute_file_hash(str(changed_file))

        file_entry = ExportFileEntry(
            filename="changed_test.csv",
            file_path=str(changed_file),
            file_size=100,
            row_count=1,
            content_hash=file_hash,
        )

        record = manager.create_record(
            trigger=ExportTrigger.MANUAL_ALL,
            status=ExportStatus.SUCCESS,
            operator="变更测试员",
            files=[file_entry],
            result_message="导出1条记录",
        )

        assert record.conflict_hint == ConflictHint.NONE
        log("  初始无冲突 OK")

        time.sleep(0.1)
        changed_file.write_text("modified,data\n3,4\n5,6", encoding="utf-8")

        conflicts = manager.check_file_conflicts(record)
        assert len(conflicts) >= 1, "应有冲突"
        assert any("变更" in issue for c in conflicts for issue in c["issues"]), \
            "应有内容变更提示"
        log(f"  变更冲突: {conflicts[0]['issues']}")

        log("=== 7. 文件内容变更冲突 ===")
        log("  [OK] 文件内容变更冲突测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_8_audit_package_record():
    """8. 审计包导出 - 完整记录验证"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        queue = QueueManager(storage, config)

        manager = BatchPlaybackManager(storage)

        csv_content = (
            "filename,copies,counter,max_retries,priority\n"
            "审计文件1.pdf,2,A类柜台,3,\n"
            "审计文件2.pdf,1,B类柜台,99,\n"
            ",,C类柜台,3,\n"
        )
        test_csv = Path(tmpdir) / "audit_test.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        batch = manager.create_batch(str(test_csv), operator="审计测试员")

        out_dir = Path(tmpdir) / "audit_exports"
        out_dir.mkdir()
        audit_path = manager.export_audit_package(batch, str(out_dir), operator="审计导出员")

        log("=== 8. 审计包导出记录 ===")

        assert Path(audit_path).exists()
        log(f"  审计包: {Path(audit_path).name}")

        record_manager = ExportRecordManager(storage)
        records = record_manager.load_all_records()
        audit_records = [r for r in records if r.trigger == ExportTrigger.AUDIT_PACKAGE]

        assert len(audit_records) >= 1, "应有审计包导出记录"
        record = audit_records[0]

        assert record.status == ExportStatus.SUCCESS
        assert record.operator == "审计导出员"
        assert record.batch_summary is not None, "应有批次摘要"
        assert record.batch_summary.get("batch_id") == batch.batch_id
        assert "total_items" in record.batch_summary
        assert "group_counts" in record.batch_summary
        log(f"  批次摘要: 总{record.batch_summary['total_items']}条")

        assert record.filter_snapshot.get("batch_id") == batch.batch_id
        assert record.filter_snapshot.get("source_file") is not None
        log(f"  筛选条件快照: batch_id={record.filter_snapshot['batch_id'][:16]}...")

        assert record.statistics.get("total_items") == len(batch.items)
        log(f"  统计: {record.statistics}")

        assert len(record.files) >= 1
        assert record.files[0].content_hash != "", "审计包文件应有哈希"
        assert record.version_tag != ""
        log(f"  版本标记: {record.version_tag}")

        with open(audit_path, "r", encoding="utf-8") as f:
            audit_data = json.load(f)
        assert audit_data["export_type"] == "playback_audit_package"
        log("  审计包内容验证 OK")

        log("  [OK] 审计包导出记录测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_9_query_and_search():
    """9. 查询与搜索 - 多条件组合筛选"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)

        manager = ExportRecordManager(storage)

        manager.create_record(
            trigger=ExportTrigger.MANUAL_HISTORY,
            status=ExportStatus.SUCCESS,
            operator="张三",
            result_message="导出历史10条",
        )
        manager.create_record(
            trigger=ExportTrigger.MANUAL_ALL,
            status=ExportStatus.SUCCESS,
            operator="李四",
            result_message="导出全部20条",
        )
        manager.create_record(
            trigger=ExportTrigger.AUDIT_PACKAGE,
            status=ExportStatus.FAILED,
            operator="张三",
            failure_reason="权限不足",
            result_message="审计包导出失败",
        )
        manager.create_record(
            trigger=ExportTrigger.MANUAL_HISTORY,
            status=ExportStatus.FAILED,
            operator="王五",
            failure_reason="IO错误",
            result_message="导出失败",
        )

        log("=== 9. 查询与搜索 ===")

        all_records = manager.query_records()
        assert len(all_records) >= 4
        log(f"  全部记录: {len(all_records)} 条")

        success_records = manager.query_records(status_filter=ExportStatus.SUCCESS)
        assert len(success_records) >= 2
        log(f"  成功记录: {len(success_records)} 条")

        failed_records = manager.query_records(status_filter=ExportStatus.FAILED)
        assert len(failed_records) >= 2
        log(f"  失败记录: {len(failed_records)} 条")

        history_records = manager.query_records(trigger_filter=ExportTrigger.MANUAL_HISTORY)
        assert len(history_records) >= 2
        log(f"  历史导出: {len(history_records)} 条")

        combined = manager.query_records(
            status_filter=ExportStatus.FAILED,
            trigger_filter=ExportTrigger.MANUAL_HISTORY,
        )
        assert len(combined) >= 1
        log(f"  失败+历史组合: {len(combined)} 条")

        search_zhang = manager.query_records(search_text="张三")
        assert len(search_zhang) >= 2
        log(f"  搜索'张三': {len(search_zhang)} 条")

        search_perm = manager.query_records(search_text="权限")
        assert len(search_perm) >= 1
        log(f"  搜索'权限': {len(search_perm)} 条")

        log("  [OK] 查询与搜索测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_10_exporter_integration():
    """10. HistoryExporter 集成 - CSV/JSON导出均生成记录"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        queue = QueueManager(storage, config)

        tasks = [
            PrintTask.create(filename="集成1.pdf", copies=1, counter=CounterType.A),
            PrintTask.create(filename="集成2.pdf", copies=2, counter=CounterType.B),
        ]
        queue.add_tasks(tasks)
        for t in queue.tasks:
            t.status = TaskStatus.COMPLETED

        exporter = HistoryExporter(storage)
        out_dir = Path(tmpdir) / "int_exports"
        out_dir.mkdir()

        log("=== 10. HistoryExporter 集成 ===")

        csv_result = exporter.export_history(queue.tasks, str(out_dir), operator="CSV导出员", fmt="csv")
        assert csv_result.success
        log(f"  CSV导出: {csv_result.message}")

        json_result = exporter.export_all(queue.tasks, str(out_dir), operator="JSON导出员", fmt="json")
        assert json_result.success
        log(f"  JSON导出: {json_result.message}")

        record_manager = ExportRecordManager(storage)
        records = record_manager.load_all_records()

        csv_records = [r for r in records if r.trigger == ExportTrigger.MANUAL_HISTORY]
        json_records = [r for r in records if r.trigger == ExportTrigger.MANUAL_ALL]

        assert len(csv_records) >= 1, "应有CSV导出记录"
        assert len(json_records) >= 1, "应有JSON导出记录"

        csv_rec = csv_records[0]
        assert csv_rec.status == ExportStatus.SUCCESS
        assert csv_rec.filter_snapshot.get("format") == "csv"
        assert len(csv_rec.files) >= 1
        assert csv_rec.files[0].content_hash != ""
        log(f"  CSV记录: {csv_rec.record_id[:16]}... hash={csv_rec.files[0].content_hash[:12]}...")

        json_rec = json_records[0]
        assert json_rec.status == ExportStatus.SUCCESS
        assert json_rec.filter_snapshot.get("format") == "json"
        assert len(json_rec.files) >= 1
        log(f"  JSON记录: {json_rec.record_id[:16]}... hash={json_rec.files[0].content_hash[:12]}...")

        log("  [OK] HistoryExporter 集成测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_11_record_unique_id_and_version():
    """11. 记录唯一性与版本标记"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)

        manager = ExportRecordManager(storage)

        r1 = manager.create_record(
            trigger=ExportTrigger.MANUAL_HISTORY,
            status=ExportStatus.SUCCESS,
            operator="唯一性测试",
            version_tag="v1_test1",
            result_message="导出1",
        )
        r2 = manager.create_record(
            trigger=ExportTrigger.MANUAL_ALL,
            status=ExportStatus.SUCCESS,
            operator="唯一性测试",
            version_tag="v1_test2",
            result_message="导出2",
        )

        log("=== 11. 唯一性与版本标记 ===")

        assert r1.record_id != r2.record_id, "每条记录ID应唯一"
        assert r1.record_id.startswith("exp_"), "记录ID应以exp_开头"
        assert r2.record_id.startswith("exp_")
        log(f"  记录1 ID: {r1.record_id}")
        log(f"  记录2 ID: {r2.record_id}")

        assert r1.version_tag == "v1_test1"
        assert r2.version_tag == "v1_test2"
        log(f"  版本标记1: {r1.version_tag}")
        log(f"  版本标记2: {r2.version_tag}")

        loaded1 = manager.load_record(r1.record_id)
        loaded2 = manager.load_record(r2.record_id)
        assert loaded1 is not None and loaded2 is not None
        assert loaded1.record_id != loaded2.record_id
        log("  唯一性加载验证 OK")

        log("  [OK] 唯一性与版本标记测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_12_hash_functions():
    """12. 哈希函数验证"""
    tmpdir, orig = _make_tmp_env()
    try:
        test_file = Path(tmpdir) / "hash_test.txt"
        test_file.write_text("hello world", encoding="utf-8")

        log("=== 12. 哈希函数 ===")

        file_hash1 = compute_file_hash(str(test_file))
        file_hash2 = compute_file_hash(str(test_file))
        assert file_hash1 == file_hash2, "同一文件哈希应一致"
        assert len(file_hash1) == 64, "SHA256哈希应为64字符"
        log(f"  文件哈希: {file_hash1[:16]}...")

        content_hash1 = compute_content_hash({"key": "value", "num": 42})
        content_hash2 = compute_content_hash({"num": 42, "key": "value"})
        assert content_hash1 == content_hash2, "相同内容（不同key顺序）哈希应一致"
        log(f"  内容哈希: {content_hash1[:16]}... (排序一致性OK)")

        test_file.write_text("modified content", encoding="utf-8")
        file_hash3 = compute_file_hash(str(test_file))
        assert file_hash3 != file_hash1, "修改后哈希应不同"
        log(f"  修改后哈希: {file_hash3[:16]}... (变更检测OK)")

        nonexistent_hash = compute_file_hash(str(Path(tmpdir) / "nonexistent.txt"))
        assert nonexistent_hash == "", "不存在文件哈希应为空字符串"
        log("  不存在文件哈希为空 OK")

        log("  [OK] 哈希函数验证通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_all():
    tests = [
        ("导出成功记录", test_1_export_success_record),
        ("导出失败记录", test_2_export_failure_record),
        ("重复导出冲突", test_3_duplicate_export_conflict),
        ("权限不足导出", test_4_permission_error_record),
        ("重启恢复", test_5_restart_recovery),
        ("文件删除冲突", test_6_file_deleted_conflict),
        ("文件内容变更冲突", test_7_content_changed_conflict),
        ("审计包导出记录", test_8_audit_package_record),
        ("查询与搜索", test_9_query_and_search),
        ("HistoryExporter集成", test_10_exporter_integration),
        ("唯一性与版本标记", test_11_record_unique_id_and_version),
        ("哈希函数验证", test_12_hash_functions),
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
