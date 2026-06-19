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
from core.batch_playback import (
    BatchPlaybackManager, ImportBatch, PlaybackItem, PlaybackGroup,
    GROUP_LABELS, BatchStatus, PlaybackUIState, RawFieldSnapshot,
    BATCHES_FILE, LAST_SUBMITTED_FILE, UI_STATE_FILE,
)
from core.preflight import ConflictResolution, ConflictType


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def _make_tmp_env():
    tmpdir = tempfile.mkdtemp(prefix="playback_test_")
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir()

    import core.storage as st_mod
    import core.batch_playback as pb_mod

    orig = {
        "DATA_DIR": st_mod.DATA_DIR,
        "TASKS_FILE": st_mod.TASKS_FILE,
        "CONFIG_FILE": st_mod.CONFIG_FILE,
        "EXPORT_LOG_FILE": st_mod.EXPORT_LOG_FILE,
        "BATCHES_FILE": pb_mod.BATCHES_FILE,
        "LAST_SUBMITTED_FILE": pb_mod.LAST_SUBMITTED_FILE,
        "UI_STATE_FILE": pb_mod.UI_STATE_FILE,
    }

    st_mod.DATA_DIR = data_dir
    st_mod.TASKS_FILE = data_dir / "tasks.json"
    st_mod.CONFIG_FILE = data_dir / "config.json"
    st_mod.EXPORT_LOG_FILE = data_dir / "export_log.json"
    pb_mod.BATCHES_FILE = data_dir / "playback_batches.json"
    pb_mod.LAST_SUBMITTED_FILE = data_dir / "playback_last_submitted.json"
    pb_mod.UI_STATE_FILE = data_dir / "playback_ui_state.json"

    return tmpdir, orig


def _restore_env(orig):
    import core.storage as st_mod
    import core.batch_playback as pb_mod

    st_mod.DATA_DIR = orig["DATA_DIR"]
    st_mod.TASKS_FILE = orig["TASKS_FILE"]
    st_mod.CONFIG_FILE = orig["CONFIG_FILE"]
    st_mod.EXPORT_LOG_FILE = orig["EXPORT_LOG_FILE"]
    pb_mod.BATCHES_FILE = orig["BATCHES_FILE"]
    pb_mod.LAST_SUBMITTED_FILE = orig["LAST_SUBMITTED_FILE"]
    pb_mod.UI_STATE_FILE = orig["UI_STATE_FILE"]


def test_1_batch_creation_and_groups():
    """1. 批次创建与分组 - 混排非法项与兜底项"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)

        manager = BatchPlaybackManager(storage)

        csv_content = (
            "filename,copies,counter,max_retries,priority\n"
            "正常文件.pdf,2,A类柜台,3,\n"
            "重试越界.pdf,1,B类柜台,99,\n"
            "优先级越界.pdf,1,C类柜台,3,99999\n"
            "优先级格式错.pdf,1,D类柜台,,abc\n"
            ",,A类柜台,3,\n"
            "份数为零.pdf,0,B类柜台,3,\n"
            "未知柜台.pdf,2,X类柜台,3,\n"
            "正常文件2.pdf,5,D类柜台,3,2\n"
        )
        test_csv = Path(tmpdir) / "mixed_test.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        batch = manager.create_batch(str(test_csv), operator="测试员")

        groups = batch.groups()
        counts = batch.group_counts()

        log(f"=== 1. 批次创建与分组 ===")
        for g, label in GROUP_LABELS.items():
            log(f"  {label}: {counts.get(g.value, 0)} 条")

        assert batch.status == BatchStatus.PENDING, "批次状态应为待提交"
        assert batch.source_format == "csv"
        assert batch.operator == "测试员"
        assert len(batch.items) > 0

        success_count = len(groups[PlaybackGroup.SUCCESS])
        fixable_count = len(groups[PlaybackGroup.AUTO_FIXABLE])
        unimportable_count = len(groups[PlaybackGroup.UNIMPORTABLE])

        assert success_count >= 1, "应有正常导入的条目"
        assert fixable_count >= 2, "应有默认值兜底的条目（重试越界、优先级越界/格式错）"
        assert unimportable_count >= 3, "应有无法导入的条目（空文件名、份数为零、未知柜台）"

        log(f"  正常导入: {success_count}")
        log(f"  默认值兜底: {fixable_count}")
        log(f"  无法导入: {unimportable_count}")

        fixable_items = groups[PlaybackGroup.AUTO_FIXABLE]
        has_retry_msg = any(
            any("重试次数" in r and "超出范围" in r for r in it.fallback_reasons)
            for it in fixable_items
        )
        has_priority_msg = any(
            any("优先级" in r and "超出范围" in r for r in it.fallback_reasons)
            or any("优先级" in r and "格式错误" in r for r in it.fallback_reasons)
            for it in fixable_items
        )
        assert has_retry_msg, "应有重试次数越界的兜底原因"
        assert has_priority_msg, "应有优先级越界/格式错的兜底原因"
        log("  兜底原因验证 OK")

        unimportable_items = groups[PlaybackGroup.UNIMPORTABLE]
        has_filename_missing = any("文件名为空" in it.error_message for it in unimportable_items)
        has_bad_copies = any("份数非法" in it.error_message or "份数为零" in it.error_message
                             for it in unimportable_items)
        has_unknown_counter = any("未知柜台" in it.error_message for it in unimportable_items)
        assert has_filename_missing, "应有文件名为空的错误"
        assert has_bad_copies, "应有份数非法的错误"
        assert has_unknown_counter, "应有未知柜台的错误"
        log("  非法项原因验证 OK")

        log("  [OK] 混排非法项与兜底项测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_2_raw_field_snapshot():
    """2. 原始字段快照保存"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        manager = BatchPlaybackManager(storage)

        csv_content = (
            "filename,copies,counter,max_retries,priority,custom_field\n"
            "测试文件.pdf,3,A类柜台,5,2,自定义值\n"
            ",0,B类柜台,3,,空名零份\n"
        )
        test_csv = Path(tmpdir) / "raw_test.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        batch = manager.create_batch(str(test_csv), operator="快照测试")

        log(f"=== 2. 原始字段快照 ===")

        success_items = [it for it in batch.items if it.group == PlaybackGroup.SUCCESS]
        assert len(success_items) >= 1
        item = success_items[0]
        raw = item.raw_fields

        assert raw.filename == "测试文件.pdf", f"文件名字段不对: '{raw.filename}'"
        assert raw.copies == "3", f"份数字段不对: '{raw.copies}'"
        assert raw.counter == "A类柜台", f"柜台字段不对: '{raw.counter}'"
        assert raw.max_retries == "5", f"重试次数字段不对: '{raw.max_retries}'"
        assert raw.priority == "2", f"优先级字段不对: '{raw.priority}'"
        assert "custom_field" in raw.extra, "应保留额外字段"
        assert raw.extra["custom_field"] == "自定义值", "额外字段值不对"
        log("  成功条目原始字段快照 OK")

        error_items = [it for it in batch.items if it.group == PlaybackGroup.UNIMPORTABLE]
        assert len(error_items) >= 1
        err_item = error_items[0]
        err_raw = err_item.raw_fields
        assert err_raw.filename == "", "错误条目文件名字段应为空字符串"
        assert err_raw.copies == "0", f"错误条目份数字段不对: '{err_raw.copies}'"
        log("  失败条目原始字段快照 OK")

        log("  [OK] 原始字段快照测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_3_conflict_detection_and_candidates():
    """3. 冲突检测与冲突候选列表"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        queue = QueueManager(storage, config)

        existing = [
            PrintTask.create(filename="重复文件.pdf", copies=2, counter=CounterType.A),
            PrintTask.create(filename="同柜台同优先级.pdf", copies=1, counter=CounterType.B),
        ]
        queue.add_tasks(existing)

        manager = BatchPlaybackManager(storage)

        csv_content = (
            "filename,copies,counter\n"
            "重复文件.pdf,2,A类柜台\n"
            "同柜台同优先级.pdf,1,B类柜台\n"
            "新文件.pdf,1,C类柜台\n"
        )
        test_csv = Path(tmpdir) / "conflict_test.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        batch = manager.create_batch(
            str(test_csv),
            existing_tasks=queue.tasks,
            operator="冲突测试员"
        )

        log(f"=== 3. 冲突检测 ===")

        conflict_items = [it for it in batch.items if it.group == PlaybackGroup.DUPLICATE_CONFLICT]
        log(f"  冲突条目: {len(conflict_items)}")

        assert len(conflict_items) >= 2, "应有至少2条冲突"

        dup_file_item = [it for it in conflict_items if it.parsed_task and it.parsed_task.filename == "重复文件.pdf"][0]
        assert dup_file_item.conflict_type is not None
        assert len(dup_file_item.conflict_candidates) >= 1, "应有冲突候选"
        candidate = dup_file_item.conflict_candidates[0]
        assert candidate.filename == "重复文件.pdf"
        assert candidate.task_id == existing[0].id
        log("  文件名冲突 + 候选任务 OK")

        same_prio_item = [it for it in conflict_items if it.parsed_task and it.parsed_task.filename == "同柜台同优先级.pdf"][0]
        assert same_prio_item.conflict_type is not None
        assert len(same_prio_item.conflict_candidates) >= 1
        log("  同柜台同优先级冲突 + 候选 OK")

        log("  [OK] 冲突检测与候选测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_4_resolution_and_undo():
    """4. 冲突决策与撤销 - 撤销后再提交"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        queue = QueueManager(storage, config)

        existing = PrintTask.create(filename="冲突文件.pdf", copies=1, counter=CounterType.A)
        queue.add_tasks([existing])

        manager = BatchPlaybackManager(storage)

        csv_content = "filename,copies,counter\n冲突文件.pdf,2,A类柜台\n"
        test_csv = Path(tmpdir) / "resolution_test.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        batch = manager.create_batch(
            str(test_csv),
            existing_tasks=queue.tasks,
            operator="决策测试员"
        )

        log(f"=== 4. 冲突决策与撤销 ===")

        conflict_items = [it for it in batch.items if it.group == PlaybackGroup.DUPLICATE_CONFLICT]
        assert len(conflict_items) >= 1
        item_idx = conflict_items[0].item_index

        original_res = conflict_items[0].resolution
        log(f"  初始决策: {original_res.value}")

        manager.set_resolution(batch, item_idx, ConflictResolution.SKIP, operator="测试员")
        batch = manager.load_pending_batch(batch.batch_id)
        item = [it for it in batch.items if it.item_index == item_idx][0]
        assert item.resolution == ConflictResolution.SKIP, "设置跳过后应生效"
        log("  设置 SKIP 决策 OK")

        timeline_actions = [t.action for t in item.timeline]
        assert "冲突决策" in timeline_actions, "应记录决策时间线"
        assert any(ConflictResolution.SKIP.value in t.detail for t in item.timeline), "时间线应包含决策详情"
        log("  决策时间线记录 OK")

        result = manager.undo_last_decision(batch, item_idx, operator="测试员")
        assert result, "撤销应成功"
        batch = manager.load_pending_batch(batch.batch_id)
        item = [it for it in batch.items if it.item_index == item_idx][0]
        assert item.resolution == original_res, "撤销后决策应恢复到初始默认值"
        log("  撤销决策 OK")

        manager.set_resolution(batch, item_idx, ConflictResolution.OVERRIDE_PRIORITY,
                               override_priority=5, operator="测试员")
        batch = manager.load_pending_batch(batch.batch_id)
        item = [it for it in batch.items if it.item_index == item_idx][0]
        assert item.resolution == ConflictResolution.OVERRIDE_PRIORITY
        assert item.override_priority_value == 5
        log("  设置 OVERRIDE_PRIORITY + 优先级值 OK")

        manager.undo_last_decision(batch, item_idx, operator="测试员")
        batch = manager.load_pending_batch(batch.batch_id)
        item = [it for it in batch.items if it.item_index == item_idx][0]
        assert item.override_priority_value is None, "撤销后覆盖优先级应清空"
        log("  撤销后覆盖优先级清空 OK")

        summary = manager.submit_batch(batch, queue, operator="测试员")
        log(f"  提交结果: 加入{summary['added_count']} 跳过{summary['skipped_count']}")

        count_in_queue = sum(1 for t in queue.tasks if t.filename == "冲突文件.pdf")
        default_resolved_count = 1 if original_res == ConflictResolution.KEEP_BOTH else 0
        assert count_in_queue == 1 + default_resolved_count, f"撤销后按默认决策提交，队列中应有{1 + default_resolved_count}个同名文件"
        log("  撤销后再提交验证 OK")

        log("  [OK] 冲突决策与撤销测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_5_keep_both_and_export():
    """5. 保留两条策略 + 审计包导出内容"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        queue = QueueManager(storage, config)

        existing = PrintTask.create(filename="保留测试.pdf", copies=1, counter=CounterType.A)
        queue.add_tasks([existing])

        manager = BatchPlaybackManager(storage)

        csv_content = "filename,copies,counter\n保留测试.pdf,2,A类柜台\n"
        test_csv = Path(tmpdir) / "keep_test.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        batch = manager.create_batch(
            str(test_csv),
            existing_tasks=queue.tasks,
            operator="保留测试员"
        )

        log(f"=== 5. 保留两条 + 审计导出 ===")

        conflict_items = [it for it in batch.items if it.group == PlaybackGroup.DUPLICATE_CONFLICT]
        assert len(conflict_items) >= 1
        item_idx = conflict_items[0].item_index

        manager.set_resolution(batch, item_idx, ConflictResolution.KEEP_BOTH, operator="测试员")
        batch = manager.load_pending_batch(batch.batch_id)

        out_dir = Path(tmpdir) / "exports"
        out_dir.mkdir()
        audit_path = manager.export_audit_package(batch, str(out_dir), operator="导出员")

        assert Path(audit_path).exists() and os.path.getsize(audit_path) > 0
        log(f"  审计包导出: {Path(audit_path).name}")

        with open(audit_path, "r", encoding="utf-8") as f:
            audit_data = json.load(f)

        assert audit_data["export_type"] == "playback_audit_package"
        assert audit_data["exported_by"] == "导出员"
        assert "batch" in audit_data
        assert audit_data["batch"]["batch_id"] == batch.batch_id
        log("  审计包元数据 OK")

        items_data = audit_data["batch"]["items"]
        assert len(items_data) >= 1
        conflict_item_data = [it for it in items_data if it["group"] == "duplicate_conflict"][0]
        assert conflict_item_data["resolution"] == ConflictResolution.KEEP_BOTH.value
        assert "raw_fields" in conflict_item_data
        assert "parsed_task" in conflict_item_data
        assert "timeline" in conflict_item_data
        assert "conflict_candidates" in conflict_item_data
        log("  导出内容包含原始字段、解析结果、时间线、冲突候选 OK")

        assert len(conflict_item_data["timeline"]) >= 2
        timeline_actions = [t["action"] for t in conflict_item_data["timeline"]]
        assert "解析完成" in timeline_actions
        assert "冲突决策" in timeline_actions
        log("  导出的时间线完整 OK")

        summary = manager.submit_batch(batch, queue, operator="提交员")
        assert summary["added_count"] == 1
        count_in_queue = sum(1 for t in queue.tasks if t.filename == "保留测试.pdf")
        assert count_in_queue == 2, "保留两条策略下队列中应有2个同名文件"
        log("  保留两条提交后队列验证 OK")

        log("  [OK] 保留两条与审计包导出测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_6_persistence_and_restart():
    """6. 持久化与重启恢复 - 未提交批次、已提交批次、UI状态"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        queue = QueueManager(storage, config)

        manager1 = BatchPlaybackManager(storage)

        csv_content = (
            "filename,copies,counter,max_retries\n"
            "重启测试1.pdf,2,A类柜台,3\n"
            "重启测试2.pdf,5,B类柜台,99\n"
            "空文件名,,C类柜台,3\n"
        )
        test_csv = Path(tmpdir) / "restart_test.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        batch1 = manager1.create_batch(str(test_csv), operator="创建员A")

        conflict_items = [it for it in batch1.items if it.group == PlaybackGroup.AUTO_FIXABLE]
        if conflict_items:
            manager1.set_selected(batch1, conflict_items[0].item_index, False, operator="创建员A")

        ui_state = PlaybackUIState(
            current_batch_id=batch1.batch_id,
            selected_group=PlaybackGroup.AUTO_FIXABLE.value,
            expanded_items=[0, 1],
            filter_text="",
        )
        manager1.save_ui_state(ui_state)

        log(f"=== 6. 持久化与重启恢复 ===")

        existing_count_before = len(manager1.load_all_pending())
        log(f"  重启前待提交批次: {existing_count_before}")

        manager2 = BatchPlaybackManager(storage)

        pending_batches = manager2.load_all_pending()
        assert len(pending_batches) >= 1
        log(f"  重启后待提交批次: {len(pending_batches)}")

        restored_batch = manager2.load_pending_batch(batch1.batch_id)
        assert restored_batch is not None
        assert restored_batch.batch_id == batch1.batch_id
        assert restored_batch.status == BatchStatus.PENDING
        assert len(restored_batch.items) == len(batch1.items)
        assert restored_batch.operator == "创建员A"
        log("  未提交批次重启恢复 OK")

        restored_items = {it.item_index: it for it in restored_batch.items}
        original_items = {it.item_index: it for it in batch1.items}
        for idx in original_items:
            assert restored_items[idx].group == original_items[idx].group
            assert restored_items[idx].source_row == original_items[idx].source_row
            assert restored_items[idx].selected == original_items[idx].selected
        log("  条目分组与勾选状态恢复 OK")

        restored_ui = manager2.load_ui_state()
        assert restored_ui.current_batch_id == batch1.batch_id
        assert restored_ui.selected_group == PlaybackGroup.AUTO_FIXABLE.value
        assert 0 in restored_ui.expanded_items
        assert 1 in restored_ui.expanded_items
        log("  UI筛选状态恢复 OK")

        submitted_batch = manager1.load_pending_batch(batch1.batch_id)
        manager1.submit_batch(submitted_batch, queue, operator="提交员B")

        last_submitted1 = manager1.load_last_submitted()
        assert last_submitted1 is not None
        assert last_submitted1.status == BatchStatus.SUBMITTED
        assert last_submitted1.submit_summary is not None
        log("  最近已提交批次持久化 OK")

        manager3 = BatchPlaybackManager(storage)
        last_submitted2 = manager3.load_last_submitted()
        assert last_submitted2 is not None
        assert last_submitted2.batch_id == last_submitted1.batch_id
        assert last_submitted2.status == BatchStatus.SUBMITTED
        assert last_submitted2.submit_summary["added_count"] == last_submitted1.submit_summary["added_count"]
        log("  最近已提交批次重启恢复 OK")

        log("  [OK] 持久化与重启恢复测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_7_submit_integration():
    """7. 提交集成 - 提交后走现有入队链路"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        config.print_duration_ms = 100
        storage.save_config(config)
        queue = QueueManager(storage, config)

        manager = BatchPlaybackManager(storage)

        csv_content = (
            "filename,copies,counter\n"
            "入队测试1.pdf,2,A类柜台\n"
            "入队测试2.pdf,3,B类柜台\n"
            ",,C类柜台\n"
        )
        test_csv = Path(tmpdir) / "submit_test.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        batch = manager.create_batch(
            str(test_csv),
            existing_tasks=queue.tasks,
            operator="入队测试员"
        )

        log(f"=== 7. 提交入队集成 ===")

        unimportable_items = [it for it in batch.items if it.group == PlaybackGroup.UNIMPORTABLE]
        if unimportable_items:
            manager.set_selected(batch, unimportable_items[0].item_index, True, operator="测试员")

        before_count = len(queue.tasks)
        log(f"  提交前队列任务数: {before_count}")

        summary = manager.submit_batch(batch, queue, operator="提交员")

        log(f"  提交结果: 加入{summary['added_count']} "
            f"跳过{summary['skipped_count']} 失败{summary['failed_count']}")

        after_count = len(queue.tasks)
        log(f"  提交后队列任务数: {after_count}")

        assert summary["added_count"] >= 2, "应成功加入至少2条"
        assert summary["failed_count"] >= 1, "应有至少1条失败"
        assert after_count == before_count + summary["added_count"], "队列任务数应增加"

        last = manager.load_last_submitted()
        assert last is not None
        assert last.status == BatchStatus.SUBMITTED
        assert last.submitted_at is not None
        assert len(last.submit_summary["added_ids"]) == summary["added_count"]
        log("  提交后批次状态与摘要 OK")

        queue.start_worker()
        deadline = time.time() + 10
        completed = False
        while time.time() < deadline:
            stats = queue.get_statistics()
            if stats[TaskStatus.COMPLETED.value] >= summary["added_count"]:
                completed = True
                break
            time.sleep(0.2)
        assert completed, "提交的任务应能正常调度完成"
        queue.stop_worker()
        log("  提交后任务可正常调度完成 OK")

        log("  [OK] 提交入队集成测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_8_json_format_batch():
    """8. JSON格式批次创建"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        manager = BatchPlaybackManager(storage)

        json_data = [
            {"filename": "json正常.pdf", "copies": 2, "counter": "A类柜台", "max_retries": 3},
            {"filename": "json兜底.pdf", "copies": 1, "counter": "B类柜台", "max_retries": 99},
            {"filename": "", "copies": 3, "counter": "C类柜台"},
            {"filename": "json非法柜台.pdf", "copies": 1, "counter": "XYZ"},
        ]
        test_json = Path(tmpdir) / "test_batch.json"
        with open(test_json, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False)

        batch = manager.create_batch(str(test_json), operator="JSON测试员")

        log(f"=== 8. JSON格式批次 ===")

        assert batch.source_format == "json"
        counts = batch.group_counts()
        log(f"  分组: 成功{counts.get('success', 0)} "
            f"兜底{counts.get('auto_fixable', 0)} "
            f"失败{counts.get('unimportable', 0)}")

        assert counts.get("success", 0) >= 1
        assert counts.get("auto_fixable", 0) >= 1
        assert counts.get("unimportable", 0) >= 2

        success_items = [it for it in batch.items if it.group == PlaybackGroup.SUCCESS]
        if success_items:
            raw = success_items[0].raw_fields
            assert raw.filename == "json正常.pdf"
            assert raw.copies == "2"
            assert raw.counter == "A类柜台"
            log("  JSON原始字段快照 OK")

        log("  [OK] JSON格式批次测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_9_batch_timeline():
    """9. 批次级时间线记录"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        manager = BatchPlaybackManager(storage)

        csv_content = "filename,copies,counter\n时间线测试.pdf,1,A类柜台\n"
        test_csv = Path(tmpdir) / "timeline_test.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        batch = manager.create_batch(str(test_csv), operator="时间线员")

        log(f"=== 9. 批次时间线 ===")

        create_actions = [t.action for t in batch.timeline]
        assert "批次创建" in create_actions
        log("  创建批次时间线 OK")

        for it in batch.items:
            if it.group == PlaybackGroup.SUCCESS:
                manager.set_selected(batch, it.item_index, False, operator="操作人")
                break

        batch = manager.load_pending_batch(batch.batch_id)
        batch_actions = [t.action for t in batch.timeline]
        assert "批量操作" in batch_actions or "条目决策" in batch_actions or "条目操作" in batch_actions or any("勾选" in a for a in batch_actions)
        log("  操作后批次时间线更新 OK")

        for item in batch.items:
            assert len(item.timeline) >= 1
            item_actions = [t.action for t in item.timeline]
            assert "解析完成" in item_actions
            if item.group == PlaybackGroup.SUCCESS:
                assert any("勾选" in a for a in item_actions) or "取消勾选" in item_actions
        log("  条目级时间线 OK")

        log("  [OK] 批次时间线测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_10_group_select_and_submit_mixed():
    """10. 分组批量勾选 + 混合提交结果"""
    tmpdir, orig = _make_tmp_env()
    try:
        storage = Storage()
        config = storage.load_config()
        storage.save_config(config)
        queue = QueueManager(storage, config)

        existing = PrintTask.create(filename="冲突混合.pdf", copies=1, counter=CounterType.A)
        queue.add_tasks([existing])

        manager = BatchPlaybackManager(storage)

        csv_content = (
            "filename,copies,counter,max_retries\n"
            "正常1.pdf,1,A类柜台,3\n"
            "正常2.pdf,2,B类柜台,3\n"
            "兜底1.pdf,3,C类柜台,99\n"
            "冲突混合.pdf,4,A类柜台,3\n"
            "失败空名,,D类柜台,3\n"
        )
        test_csv = Path(tmpdir) / "mixed_submit.csv"
        test_csv.write_text(csv_content, encoding="utf-8-sig")

        batch = manager.create_batch(
            str(test_csv),
            existing_tasks=queue.tasks,
            operator="混合测试员"
        )

        log(f"=== 10. 分组批量操作 + 混合提交 ===")

        counts = batch.group_counts()
        log(f"  初始分组: 成功{counts.get('success', 0)} "
            f"兜底{counts.get('auto_fixable', 0)} "
            f"冲突{counts.get('duplicate_conflict', 0)} "
            f"失败{counts.get('unimportable', 0)}")

        manager.set_group_selected(batch, PlaybackGroup.AUTO_FIXABLE, False, operator="测试员")
        batch = manager.load_pending_batch(batch.batch_id)

        fixable_items = [it for it in batch.items if it.group == PlaybackGroup.AUTO_FIXABLE]
        assert all(not it.selected for it in fixable_items), "兜底组应全部取消勾选"
        log("  分组批量取消勾选 OK")

        manager.set_group_selected(batch, PlaybackGroup.SUCCESS, True, operator="测试员")
        batch = manager.load_pending_batch(batch.batch_id)
        success_items = [it for it in batch.items if it.group == PlaybackGroup.SUCCESS]
        assert all(it.selected for it in success_items), "成功组应全部勾选"
        log("  分组批量勾选 OK")

        manager.set_group_selected(batch, PlaybackGroup.UNIMPORTABLE, True, operator="测试员")
        batch = manager.load_pending_batch(batch.batch_id)

        summary = manager.submit_batch(batch, queue, operator="提交员")
        log(f"  提交结果: 加入{summary['added_count']} "
            f"跳过{summary['skipped_count']} 失败{summary['failed_count']}")

        assert summary["added_count"] >= 2, "成功组至少2条应加入"
        assert summary["skipped_count"] >= 1, "兜底组取消勾选后应跳过"
        assert summary["failed_count"] >= 1, "失败组应有失败"
        log("  混合提交结果正确 OK")

        log("  [OK] 分组批量操作与混合提交测试通过")
    finally:
        _restore_env(orig)
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_all():
    tests = [
        ("批次创建与分组（混排非法/兜底）", test_1_batch_creation_and_groups),
        ("原始字段快照", test_2_raw_field_snapshot),
        ("冲突检测与候选列表", test_3_conflict_detection_and_candidates),
        ("冲突决策与撤销（撤销后再提交）", test_4_resolution_and_undo),
        ("保留两条 + 审计包导出", test_5_keep_both_and_export),
        ("持久化与重启恢复", test_6_persistence_and_restart),
        ("提交入队集成", test_7_submit_integration),
        ("JSON格式批次", test_8_json_format_batch),
        ("批次时间线记录", test_9_batch_timeline),
        ("分组批量操作 + 混合提交", test_10_group_select_and_submit_mixed),
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
