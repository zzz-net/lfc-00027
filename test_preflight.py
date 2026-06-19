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
from core.importer import TaskImporter
from core.queue_manager import QueueManager
from core.preflight import (
    PreflightChecker, PreflightCategory, PreflightResult, PreflightSummary,
    ConflictType, ConflictResolution, CATEGORY_LABELS, PREVIEWS_FILE,
)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def _make_tmp_env():
    tmpdir = tempfile.mkdtemp(prefix="preflight_test_")
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir()

    import core.storage as st_mod
    import core.preflight as pf_mod

    orig = {
        "DATA_DIR": st_mod.DATA_DIR,
        "TASKS_FILE": st_mod.TASKS_FILE,
        "CONFIG_FILE": st_mod.CONFIG_FILE,
        "EXPORT_LOG_FILE": st_mod.EXPORT_LOG_FILE,
        "PREVIEWS_FILE": pf_mod.PREVIEWS_FILE,
    }

    st_mod.DATA_DIR = data_dir
    st_mod.TASKS_FILE = data_dir / "tasks.json"
    st_mod.CONFIG_FILE = data_dir / "config.json"
    st_mod.EXPORT_LOG_FILE = data_dir / "export_log.json"
    pf_mod.PREVIEWS_FILE = data_dir / "preflight_summaries.json"

    return tmpdir, orig


def _restore_env(orig):
    import core.storage as st_mod
    import core.preflight as pf_mod

    st_mod.DATA_DIR = orig["DATA_DIR"]
    st_mod.TASKS_FILE = orig["TASKS_FILE"]
    st_mod.CONFIG_FILE = orig["CONFIG_FILE"]
    st_mod.EXPORT_LOG_FILE = orig["EXPORT_LOG_FILE"]
    pf_mod.PREVIEWS_FILE = orig["PREVIEWS_FILE"]


def test_1_csv_preview_categories():
    """1. CSV预检 - 四类分组正确性"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)

        checker = PreflightChecker(storage)
        csv_path = Path(__file__).resolve().parent / "examples" / "sample_tasks.csv"
        preview = checker.run_preview(str(csv_path), operator="测试员甲")

        groups = preview.groups()
        counts = preview.category_counts()

        log(f"=== 1. CSV预检 分组 ===")
        for c, label in CATEGORY_LABELS.items():
            log(f"  {label}: {counts[c.value]} 条")

        assert counts[PreflightCategory.UNIMPORTABLE.value] > 0, "应存在无法导入的条目"
        assert counts[PreflightCategory.SUCCESS.value] > 0, "应存在成功条目"
        assert preview.source_format == "csv", "source_format 应为 csv"
        assert preview.operator == "测试员甲", "operator 应被记录"
        assert len(preview.items) == (
            counts["success"] + counts["auto_fixable"]
            + counts["duplicate_conflict"] + counts["unimportable"]
        ), "总数应等于各组之和"

        unimportable = groups[PreflightCategory.UNIMPORTABLE]
        has_filename_missing = any("文件名" in it.error_message for it in unimportable)
        has_bad_copies = any("份数非法" in it.error_message or "份数格式" in it.error_message
                             for it in unimportable)
        assert has_filename_missing, "CSV预检应捕获文件名为空的错误"
        assert has_bad_copies, "CSV预检应捕获份数非法的错误"
        log("  CSV预检分组验证 OK")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_2_json_preview_categories():
    """2. JSON预检 - 四类分组正确性"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        checker = PreflightChecker(storage)
        json_path = Path(__file__).resolve().parent / "examples" / "sample_tasks.json"
        preview = checker.run_preview(str(json_path))

        groups = preview.groups()
        counts = preview.category_counts()

        log(f"=== 2. JSON预检 分组 ===")
        for c, label in CATEGORY_LABELS.items():
            log(f"  {label}: {counts[c.value]} 条")

        assert preview.source_format == "json"
        assert counts[PreflightCategory.UNIMPORTABLE.value] > 0, "JSON中应有无法导入的"
        unimportable = groups[PreflightCategory.UNIMPORTABLE]
        has_counter_bad = any("未知柜台" in it.error_message or "柜台非法" in it.error_message
                              for it in unimportable)
        assert has_counter_bad, "JSON预检应捕获未知柜台类型"
        log("  JSON预检分组验证 OK")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_3_duplicate_conflict_detection():
    """3. 重复/冲突检测 - 文件名、柜台+优先级、已完成的不被视为冲突"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)

        existing_tasks = [
            PrintTask.create(filename="重复文件.pdf", copies=2, counter=CounterType.A),
            PrintTask.create(filename="同柜台同优先级.pdf", copies=1, counter=CounterType.B),
            PrintTask.create(filename="已完成不冲突.pdf", copies=3, counter=CounterType.C),
        ]
        existing_tasks[2].status = TaskStatus.COMPLETED
        existing_tasks[2].completed_at = time.time()
        storage.save_tasks(existing_tasks)

        csv_content = (
            "filename,copies,counter\n"
            "重复文件.pdf,2,A类柜台\n"
            "同柜台同优先级.pdf,1,B类柜台\n"
            "已完成不冲突.pdf,3,C类柜台\n"
            "新文件.pdf,1,D类柜台\n"
        )
        test_csv = Path(tmpdir) / "dup_test.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        checker = PreflightChecker(storage)
        preview = checker.run_preview(str(test_csv), existing_tasks=existing_tasks)
        groups = preview.groups()

        log(f"=== 3. 重复/冲突检测 ===")
        log(f"  成功: {len(groups[PreflightCategory.SUCCESS])}")
        log(f"  冲突: {len(groups[PreflightCategory.DUPLICATE_CONFLICT])}")

        dup_items = groups[PreflightCategory.DUPLICATE_CONFLICT]
        success_items = groups[PreflightCategory.SUCCESS]

        assert len(dup_items) == 2, f"应有2条冲突，实际{len(dup_items)}"
        dup_filenames = {it.task.filename for it in dup_items if it.task}
        assert "重复文件.pdf" in dup_filenames, "重复文件名应被检测"
        assert "同柜台同优先级.pdf" in dup_filenames, "同柜台同优先级应被检测"

        dup_file_item = [i for i in dup_items if i.task and i.task.filename == "重复文件.pdf"][0]
        assert dup_file_item.conflict_info is not None
        assert dup_file_item.conflict_info.conflict_type in (
            ConflictType.DUPLICATE_FILENAME, ConflictType.BOTH
        )
        assert dup_file_item.conflict_info.existing_task_id == existing_tasks[0].id

        success_names = {it.task.filename for it in success_items if it.task}
        assert "已完成不冲突.pdf" in success_names, "已完成任务不应作为重复"
        assert "新文件.pdf" in success_names, "新文件应被归为成功"

        log("  重复/冲突检测 OK")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_4_conflict_resolutions_and_logs():
    """4. 冲突处理三种策略 + 操作日志记录"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        logs = []
        queue = QueueManager(storage, config, on_log=lambda m: logs.append(m))

        existing_a = PrintTask.create(filename="冲突A.pdf", copies=1, counter=CounterType.A)
        existing_b = PrintTask.create(filename="已存在B柜台.pdf", copies=1, counter=CounterType.B)
        queue.add_tasks([existing_a, existing_b])

        csv_content = (
            "filename,copies,counter,priority\n"
            "冲突A.pdf,1,A类柜台\n"
            "冲突B.pdf,2,B类柜台,2\n"
            "冲突C.pdf,3,C类柜台\n"
        )
        test_csv = Path(tmpdir) / "conflict.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        checker = PreflightChecker(storage)
        preview = checker.run_preview(str(test_csv), existing_tasks=queue.tasks, operator="测A")

        dup_items = [it for it in preview.items
                     if it.category == PreflightCategory.DUPLICATE_CONFLICT]

        if len(dup_items) >= 1:
            dup_items[0].resolution = ConflictResolution.SKIP
            dup_items[0].selected = True

        success_items = [it for it in preview.items
                         if it.category == PreflightCategory.SUCCESS]
        if len(success_items) >= 1:
            success_items[0].resolution = ConflictResolution.KEEP_BOTH
            success_items[0].selected = True

        for it in preview.items:
            if (it.category == PreflightCategory.DUPLICATE_CONFLICT
                    and it.task and it.task.filename == "冲突B.pdf"):
                it.resolution = ConflictResolution.OVERRIDE_PRIORITY
                it.override_priority_value = 5
                it.selected = True

        apply_result = checker.apply_preview(preview, queue, operator="测A")

        log(f"=== 4. 冲突处理 ===")
        log(f"  加入: {apply_result['added_count']}, "
            f"跳过: {apply_result['skipped_count']}, "
            f"失败: {apply_result['failed_count']}")

        filenames_in_queue = {t.filename for t in queue.tasks}
        assert "冲突A.pdf" in filenames_in_queue, "原任务应在队列"
        dup_a_count = sum(1 for t in queue.tasks if t.filename == "冲突A.pdf")
        assert dup_a_count == 1, "SKIP后不应有第二条冲突A.pdf"

        if "冲突B.pdf" in filenames_in_queue:
            b_tasks = [t for t in queue.tasks if t.filename == "冲突B.pdf"]
            assert any(t.priority_override == 5 for t in b_tasks), "OVERRIDE_PRIORITY应生效"

        apply_logs = apply_result["log_entries"]
        has_skip_log = any(e.get("action") == "skipped_conflict" for e in apply_logs)
        has_override_log = any(e.get("action") == "added_override" for e in apply_logs)
        assert has_skip_log, "应有跳过冲突的日志"
        if "冲突B.pdf" in filenames_in_queue:
            assert has_override_log, "应有覆盖优先级的日志"

        stored_logs = checker.load_apply_logs()
        assert len(stored_logs) >= 1, "apply记录应被持久化"
        last_apply = stored_logs[-1]
        assert last_apply["preview_id"] == preview.preview_id
        assert last_apply["operator"] == "测A"
        log("  冲突处理 + 操作日志 OK")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_5_partial_selection_only_checked():
    """5. 只导入勾选条目"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        queue = QueueManager(storage, config)

        csv_content = (
            "filename,copies,counter\n"
            "选中A.pdf,1,A类柜台\n"
            "未选中B.pdf,2,B类柜台\n"
            "选中C.pdf,3,C类柜台\n"
            "无法导入空文件名,,D类柜台\n"
        )
        test_csv = Path(tmpdir) / "select.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        checker = PreflightChecker(storage)
        preview = checker.run_preview(str(test_csv))

        for it in preview.items:
            if it.task and it.task.filename == "未选中B.pdf":
                it.selected = False

        apply_result = checker.apply_preview(preview, queue, operator="选择测试")
        filenames = {t.filename for t in queue.tasks}

        log(f"=== 5. 部分勾选 ===")
        log(f"  勾选后: 加入{apply_result['added_count']} "
            f"跳过{apply_result['skipped_count']} 失败{apply_result['failed_count']}")

        assert "选中A.pdf" in filenames
        assert "选中C.pdf" in filenames
        assert "未选中B.pdf" not in filenames, "未勾选的不应入队"
        log("  部分勾选导入 OK")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_6_persistence_and_restart_recovery():
    """6. 预检摘要持久化 & 重启恢复"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)

        csv_path = Path(__file__).resolve().parent / "examples" / "sample_tasks.csv"
        checker1 = PreflightChecker(storage)
        preview1 = checker1.run_preview(str(csv_path), operator="预检员A")

        last_before = checker1.load_last_summary()
        assert last_before is not None, "预检后应能读到摘要"
        assert last_before.preview_id == preview1.preview_id
        assert last_before.total_count == len(preview1.items)
        assert last_before.operator == "预检员A"
        log(f"  首次预检摘要已持久化: {last_before.total_count}条")

        import core.storage as st_mod
        import core.preflight as pf_mod
        Storage._instance = None

        checker2 = PreflightChecker()
        last_after = checker2.load_last_summary()

        assert last_after is not None, "重启(新Storage/Checker)后应仍能读到摘要"
        assert last_after.preview_id == preview1.preview_id, "重启后摘要ID应一致"
        assert last_after.category_counts == preview1.category_counts(), "分类计数一致"
        log(f"=== 6. 重启恢复 ===")
        log(f"  重启后最近摘要ID一致: {last_after.preview_id}")
        log("  预检摘要持久化 + 重启恢复 OK")
    finally:
        Storage._instance = None
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_7_export_preview_and_apply_logs():
    """7. 预检记录 & 操作日志 JSON导出"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        queue = QueueManager(storage, config)

        csv_path = Path(__file__).resolve().parent / "examples" / "sample_tasks.csv"
        checker = PreflightChecker(storage)
        preview = checker.run_preview(str(csv_path), operator="导出员")
        apply_result = checker.apply_preview(preview, queue, operator="导出员")

        out_dir = Path(tmpdir) / "exports"
        record_path = checker.export_preview_record(
            preview, str(out_dir), apply_result=apply_result, operator="导出员X"
        )
        logs_path = checker.export_apply_logs(str(out_dir), operator="导出员X")

        log(f"=== 7. JSON导出 ===")
        log(f"  预检记录: {Path(record_path).name}")
        log(f"  操作日志: {Path(logs_path).name}")

        assert Path(record_path).exists() and os.path.getsize(record_path) > 0
        assert Path(logs_path).exists() and os.path.getsize(logs_path) > 0

        with open(record_path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        assert rec["export_type"] == "preflight_record"
        assert rec["exported_by"] == "导出员X"
        assert rec["preview"]["preview_id"] == preview.preview_id
        assert "summary" in rec
        assert "apply_result" in rec
        assert rec["apply_result"]["added_count"] == apply_result["added_count"]

        with open(logs_path, "r", encoding="utf-8") as f:
            lg = json.load(f)
        assert lg["export_type"] == "preflight_apply_logs"
        assert len(lg["logs"]) >= 1

        log("  预检记录与操作日志导出 OK")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_8_auto_fixable_category():
    """8. 可自动修正分组 - 字段被默认值兜底的情况"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)

        csv_content = (
            "filename,copies,counter,max_retries,priority\n"
            "重试越界.pdf,1,A类柜台,99,\n"
            "优先级越界.pdf,1,B类柜台,3,99999\n"
            "优先级格式错.pdf,1,C类柜台,,abc\n"
            "完全正常.pdf,1,D类柜台,3,\n"
        )
        test_csv = Path(tmpdir) / "fixable.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        checker = PreflightChecker(storage)
        preview = checker.run_preview(str(test_csv))
        groups = preview.groups()

        log(f"=== 8. 可自动修正分组 ===")
        log(f"  成功: {len(groups[PreflightCategory.SUCCESS])}")
        log(f"  可自动修正: {len(groups[PreflightCategory.AUTO_FIXABLE])}")

        fixable = groups[PreflightCategory.AUTO_FIXABLE]
        success = groups[PreflightCategory.SUCCESS]

        assert len(fixable) == 3, f"应有3条可自动修正, 实际{len(fixable)}"
        assert len(success) == 1, f"应有1条完全成功, 实际{len(success)}"

        all_warnings = []
        for it in fixable:
            all_warnings.extend(it.skipped_warnings)
        has_retries_msg = any("重试次数" in w and "超出范围" in w for w in all_warnings)
        has_priority_range = any("优先级" in w and "超出范围" in w for w in all_warnings)
        has_priority_fmt = any("优先级" in w and "格式错误" in w for w in all_warnings)
        assert has_retries_msg, "应提示重试次数越界"
        assert has_priority_range, "应提示优先级越界"
        assert has_priority_fmt, "应提示优先级格式错误"

        assert all(it.selected for it in fixable), "可自动修正默认应勾选"
        log("  可自动修正分组与警告 OK")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_9_keep_both_resolution():
    """9. 保留两条 - 同文件也可选择同时入队"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        queue = QueueManager(storage, config)

        existing = PrintTask.create(filename="保留.pdf", copies=1, counter=CounterType.A)
        queue.add_tasks([existing])

        csv_content = "filename,copies,counter\n保留.pdf,1,A类柜台\n"
        test_csv = Path(tmpdir) / "keep.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        checker = PreflightChecker(storage)
        preview = checker.run_preview(str(test_csv), existing_tasks=queue.tasks)

        dup = [it for it in preview.items
               if it.category == PreflightCategory.DUPLICATE_CONFLICT]
        assert len(dup) >= 1
        dup[0].resolution = ConflictResolution.KEEP_BOTH
        dup[0].selected = True

        apply_result = checker.apply_preview(preview, queue)
        keep_count = sum(1 for t in queue.tasks if t.filename == "保留.pdf")

        log(f"=== 9. 保留两条 ===")
        log(f"  保留后同名文件数: {keep_count}")
        assert keep_count == 2, "KEEP_BOTH应加入第二条"
        assert apply_result["added_count"] == 1
        log("  KEEP_BOTH策略 OK")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_10_roundtrip_serialization():
    """10. PreflightResult序列化/反序列化往返"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        json_path = Path(__file__).resolve().parent / "examples" / "sample_tasks.json"
        checker = PreflightChecker(storage)
        preview = checker.run_preview(str(json_path), operator="序列化测试")

        d = preview.to_dict()
        restored = PreflightResult.from_dict(d)

        log(f"=== 10. 序列化往返 ===")
        assert restored.preview_id == preview.preview_id
        assert restored.source_format == preview.source_format
        assert len(restored.items) == len(preview.items)
        for a, b in zip(preview.items, restored.items):
            assert a.item_index == b.item_index
            assert a.category == b.category
            assert a.source_identifier == b.source_identifier
            if a.conflict_info and b.conflict_info:
                assert a.conflict_info.conflict_type == b.conflict_info.conflict_type
        summary_d = preview.summary().to_dict()
        summary_r = PreflightSummary.from_dict(summary_d)
        assert summary_r.total_count == preview.summary().total_count
        log(f"  往返 OK, items={len(restored.items)}")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_all():
    tests = [
        ("CSV预检分组", test_1_csv_preview_categories),
        ("JSON预检分组", test_2_json_preview_categories),
        ("重复/冲突检测", test_3_duplicate_conflict_detection),
        ("冲突处理+操作日志", test_4_conflict_resolutions_and_logs),
        ("部分勾选导入", test_5_partial_selection_only_checked),
        ("持久化+重启恢复", test_6_persistence_and_restart_recovery),
        ("JSON导出", test_7_export_preview_and_apply_logs),
        ("可自动修正分组", test_8_auto_fixable_category),
        ("KEEP_BOTH策略", test_9_keep_both_resolution),
        ("序列化往返", test_10_roundtrip_serialization),
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
            import traceback; traceback.print_exc()
        except Exception as e:
            failed.append((name, f"{type(e).__name__}: {e}"))
            log(f"[FAIL] {name} - {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()

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
