import sys
import os
import tempfile
import shutil
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.models import PrintTask, TaskStatus, AppConfig, CounterType
from core.storage import Storage
from core.importer import TaskImporter
from core.queue_manager import QueueManager
from core.exporter import HistoryExporter


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def test_full_chain():
    tmpdir = tempfile.mkdtemp(prefix="printq_test_")
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir()

    import core.storage as st_mod
    orig_data = st_mod.DATA_DIR
    orig_tasks = st_mod.TASKS_FILE
    orig_cfg = st_mod.CONFIG_FILE
    orig_log = st_mod.EXPORT_LOG_FILE
    try:
        st_mod.DATA_DIR = data_dir
        st_mod.TASKS_FILE = data_dir / "tasks.json"
        st_mod.CONFIG_FILE = data_dir / "config.json"
        st_mod.EXPORT_LOG_FILE = data_dir / "export_log.json"

        storage = Storage()
        config = storage.load_config()
        config.print_duration_ms = 200
        config.simulate_failure_enabled = False
        storage.save_config(config)

        logs = []
        queue = QueueManager(storage, config, on_log=lambda m: logs.append(m))

        log("=== 1. 导入示例 CSV ===")
        csv_path = Path(__file__).resolve().parent / "examples" / "sample_tasks.csv"
        imp = TaskImporter.import_file(str(csv_path), default_max_retries=3)
        added = queue.add_tasks(imp.success)
        log(f"导入结果: 成功{added} 跳过{len(imp.skipped)} 失败{len(imp.errors)}")
        assert added > 0, "应该能导入至少一条"
        assert len(imp.errors) > 0, "示例文件应该有错误条目"
        has_filename_missing = any("文件名" in err[2] for err in imp.errors)
        assert has_filename_missing, "应该有文件名缺失错误"
        has_bad_copies = any("份数非法" in err[2] for err in imp.errors)
        assert has_bad_copies, "应该有份数非法错误"
        log(f"  - 错误类型验证通过: 文件名校验={has_filename_missing}, 份数校验={has_bad_copies}")

        log("=== 2. 队列顺序验证 ===")
        tasks = queue.tasks
        order_ok = True
        last_prio = 0
        priorities = config.counter_priorities
        for t in tasks:
            p = t.priority_override or priorities.get(t.counter.value, 999)
            if p < last_prio:
                order_ok = False
                break
            last_prio = p
        log(f"  - 按柜台/自定义优先级排序: {'OK' if order_ok else 'FAIL'}")

        log("=== 3. 暂停/继续测试 ===")
        first = tasks[0].id
        ok = queue.pause_task(first, operator="测试员A")
        assert ok, "暂停应成功"
        t = queue.get_task(first)
        assert t.paused and t.operator == "测试员A"
        log(f"  - 暂停任务 OK，操作者={t.operator}")
        queue.resume_task(first, operator="测试员A")
        t = queue.get_task(first)
        assert not t.paused
        log("  - 继续任务 OK")

        log("=== 4. 取消/撤销取消测试 ===")
        tid = tasks[1].id
        queue.cancel_task(tid, operator="测试员B")
        t = queue.get_task(tid)
        assert t.status == TaskStatus.CANCELLED
        log(f"  - 取消任务 OK，状态={t.status.value}")
        res = queue.resume_task(tid, operator="测试员B")
        assert not res, "已取消任务不能直接继续"
        log("  - 已取消直接继续被正确拒绝 OK")
        res = queue.uncancel_task(tid, operator="测试员B")
        assert res, "撤销取消应成功"
        t = queue.get_task(tid)
        assert t.status == TaskStatus.WAITING
        log(f"  - 撤销取消 OK，状态恢复为={t.status.value}")

        log("=== 5. 启动调度器，等待一条任务完成 ===")
        queue.start_worker()
        deadline = time.time() + 10
        completed = False
        while time.time() < deadline:
            stats = queue.get_statistics()
            if stats[TaskStatus.COMPLETED.value] >= 1:
                completed = True
                break
            time.sleep(0.2)
        assert completed, "超时无任务完成"
        stats = queue.get_statistics()
        log(f"  - 完成! 统计: 等待{stats['等待中']} 打印中{stats['打印中']} 完成{stats['已完成']}")

        log("=== 6. 模拟失败 + 重试 + 人工处理 ===")
        queue.stop_worker()
        # 仅清理仍在活跃的任务，保留已完成/已取消供后续导出测试
        active_ids = [t.id for t in queue.tasks
                      if t.status in (TaskStatus.WAITING, TaskStatus.PRINTING,
                                      TaskStatus.FAILED, TaskStatus.MANUAL)]
        for tid in active_ids:
            queue.remove_task(tid, operator="测试清理")
        cfg2 = queue.config
        cfg2.simulate_failure_enabled = True
        cfg2.simulate_failure_rate = 1.0
        cfg2.max_retries_default = 2
        cfg2.print_duration_ms = 100
        queue.update_config(cfg2)
        single = PrintTask.create(filename="必败测试.pdf", copies=1,
                                  counter=CounterType.A, max_retries=2)
        queue.add_tasks([single])
        sid = single.id
        queue.start_worker()
        deadline = time.time() + 15
        manual = False
        last_status = None
        while time.time() < deadline:
            t = queue.get_task(sid)
            if t:
                last_status = t.status.value
                if t.status == TaskStatus.MANUAL:
                    manual = True
                    break
            time.sleep(0.2)
        assert manual, f"失败+重试链路未进入人工(当前状态={last_status})"
        t = queue.get_task(sid)
        log(f"  - 重试{t.retry_count}次后转为人工处理 OK，原因={t.fail_reason[:20]}...")
        queue.mark_manual_done(sid, operator="人工处理员")
        assert queue.get_task(sid).status == TaskStatus.COMPLETED
        log("  - 人工处理完成 OK")

        log("=== 7. 持久化与重启一致性 ===")
        queue.stop_worker()
        before_tasks = storage.load_tasks()
        before_cfg = storage.load_config()
        before_stats = {
            (t.id, t.status.value, t.retry_count, t.paused, t.fail_reason or "", t.operator or "")
            for t in before_tasks
        }
        storage2 = Storage()
        after_tasks = storage2.load_tasks()
        after_stats = {
            (t.id, t.status.value, t.retry_count, t.paused, t.fail_reason or "", t.operator or "")
            for t in after_tasks
        }
        assert before_stats == after_stats, "重启前后数据不一致"
        log(f"  - 重启一致性 OK（{len(after_tasks)}条任务状态/重试/暂停/失败原因/操作者均一致）")

        log("=== 8. 历史导出 ===")
        exporter = HistoryExporter(storage)
        out = Path(tmpdir) / "export"
        result = exporter.export_history(after_tasks, str(out), operator="测试员", fmt="csv")
        assert result.success, f"导出失败: {result.message}"
        assert Path(result.file_path).exists() and os.path.getsize(result.file_path) > 0
        log(f"  - CSV导出 OK: {result.count}条 -> {Path(result.file_path).name}")
        result_json = exporter.export_all(after_tasks, str(out), operator="测试员", fmt="json")
        assert result_json.success
        log(f"  - JSON导出 OK: {result_json.count}条")
        exp_logs = storage.load_export_logs()
        assert len(exp_logs) >= 2, "导出记录应写入"
        log(f"  - 导出日志 OK: 共{len(exp_logs)}条记录")

        log("")
        log("=" * 60)
        log("[OK]  ALL TESTS PASSED  [OK]")
        log("=" * 60)
        return 0

    finally:
        st_mod.DATA_DIR = orig_data
        st_mod.TASKS_FILE = orig_tasks
        st_mod.CONFIG_FILE = orig_cfg
        st_mod.EXPORT_LOG_FILE = orig_log
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    try:
        rc = test_full_chain()
        sys.exit(rc)
    except AssertionError as e:
        print(f"\n[FAIL] 断言失败: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] 异常: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
