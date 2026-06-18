import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import threading
import time
from pathlib import Path
from typing import Optional, List

from core.models import PrintTask, TaskStatus, AppConfig, CounterType
from core.storage import Storage
from core.importer import TaskImporter, ImportResult
from core.queue_manager import QueueManager
from core.exporter import HistoryExporter, ExportResult


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


STATUS_COLORS = {
    TaskStatus.WAITING: "#3B82F6",
    TaskStatus.PRINTING: "#F59E0B",
    TaskStatus.FAILED: "#EF4444",
    TaskStatus.MANUAL: "#A855F7",
    TaskStatus.CANCELLED: "#6B7280",
    TaskStatus.COMPLETED: "#10B981",
}

STATUS_TAB_ORDER = [
    ("全部", None),
    (TaskStatus.WAITING.value, TaskStatus.WAITING),
    (TaskStatus.PRINTING.value, TaskStatus.PRINTING),
    (TaskStatus.FAILED.value, TaskStatus.FAILED),
    (TaskStatus.MANUAL.value, TaskStatus.MANUAL),
    (TaskStatus.CANCELLED.value, TaskStatus.CANCELLED),
    (TaskStatus.COMPLETED.value, TaskStatus.COMPLETED),
]


class PrintQueueApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("本地打印任务恢复系统 v1.0")
        self.root.geometry("1280x800")
        self.root.minsize(1024, 680)

        self.storage = Storage()
        self.config = self.storage.load_config()
        self.exporter = HistoryExporter(self.storage)
        self.queue = QueueManager(
            self.storage, self.config,
            on_tasks_changed=self._on_tasks_changed_ui,
            on_log=self._append_log_ui,
        )

        self._selected_task_ids: set = set()
        self._current_tab_status: Optional[TaskStatus] = None
        self._log_buffered: List[str] = []
        self._refresh_needed = False
        self._pending_logs: List[str] = []

        self._build_ui()
        self._refresh_all()
        self._start_ui_poller()
        self.queue.start_worker()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 12, "bold"))

        main = ttk.Frame(self.root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        self._build_toolbar(main)
        self._build_stats_bar(main)
        self._build_body(main)
        self._build_statusbar(main)

    def _build_toolbar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(0, 6))

        ttk.Button(bar, text="📂 导入任务", command=self._action_import).pack(side=tk.LEFT, padx=3)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        ttk.Button(bar, text="⏸ 暂停", command=self._action_pause).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="▶ 继续", command=self._action_resume).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="🔄 重试", command=self._action_retry).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="✖ 取消", command=self._action_cancel).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="↩ 撤销取消", command=self._action_uncancel).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="✓ 人工完成", command=self._action_manual_done).pack(side=tk.LEFT, padx=2)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        self._global_pause_btn = ttk.Button(bar, text="⏸ 全局暂停", command=self._action_global_toggle)
        self._global_pause_btn.pack(side=tk.LEFT, padx=2)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(bar, text="📤 导出历史", command=self._action_export_history).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="📤 导出全部", command=self._action_export_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="🗑 清除已完成", command=self._action_clear_history).pack(side=tk.LEFT, padx=2)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(bar, text="⚙ 设置", command=self._action_settings).pack(side=tk.LEFT, padx=2)

        ttk.Label(bar, text="  操作者:").pack(side=tk.RIGHT, padx=(10, 2))
        self._operator_var = tk.StringVar(value=self.config.operator_name)
        op_entry = ttk.Entry(bar, textvariable=self._operator_var, width=12)
        op_entry.pack(side=tk.RIGHT)
        op_entry.bind("<Return>", lambda e: self._update_operator())

    def _build_stats_bar(self, parent):
        self._stats_vars: dict = {}
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(0, 6))

        for s in TaskStatus:
            frame = tk.Frame(bar, bg=STATUS_COLORS[s], padx=12, pady=4)
            frame.pack(side=tk.LEFT, padx=3)
            tk.Label(frame, text=s.value, bg=STATUS_COLORS[s], fg="white",
                     font=("Microsoft YaHei UI", 9, "bold")).pack()
            cnt_var = tk.StringVar(value="0")
            tk.Label(frame, textvariable=cnt_var, bg=STATUS_COLORS[s], fg="white",
                     font=("Microsoft YaHei UI", 14, "bold")).pack()
            self._stats_vars[s.value] = cnt_var

        total_frame = tk.Frame(bar, bg="#1F2937", padx=12, pady=4)
        total_frame.pack(side=tk.LEFT, padx=3)
        tk.Label(total_frame, text="总任务", bg="#1F2937", fg="white",
                 font=("Microsoft YaHei UI", 9, "bold")).pack()
        self._total_var = tk.StringVar(value="0")
        tk.Label(total_frame, textvariable=self._total_var, bg="#1F2937", fg="white",
                 font=("Microsoft YaHei UI", 14, "bold")).pack()

        paused_frame = tk.Frame(bar, bg="#6366F1", padx=12, pady=4)
        paused_frame.pack(side=tk.LEFT, padx=3)
        tk.Label(paused_frame, text="已暂停", bg="#6366F1", fg="white",
                 font=("Microsoft YaHei UI", 9, "bold")).pack()
        self._paused_var = tk.StringVar(value="0")
        tk.Label(paused_frame, textvariable=self._paused_var, bg="#6366F1", fg="white",
                 font=("Microsoft YaHei UI", 14, "bold")).pack()

    def _build_body(self, parent):
        paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(paned)
        paned.add(left, weight=3)

        self._tab_control = ttk.Notebook(left)
        self._tab_control.pack(fill=tk.BOTH, expand=True)
        self._tab_control.bind("<<NotebookTabChanged>>", lambda e: self._on_tab_changed())

        self._tree_map: dict = {}
        for tab_name, status in STATUS_TAB_ORDER:
            tab_frame = ttk.Frame(self._tab_control)
            self._tab_control.add(tab_frame, text=f"  {tab_name}  ")
            tree = self._build_task_tree(tab_frame, tab_name)
            self._tree_map[tab_name] = (status, tree)

        right = ttk.Frame(paned, width=340)
        paned.add(right, weight=1)

        ttk.Label(right, text="📝 操作日志", style="Title.TLabel").pack(anchor=tk.W, padx=6, pady=(4, 2))
        log_frame = ttk.Frame(right)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._log_text = tk.Text(log_frame, wrap=tk.WORD, font=("Consolas", 9),
                                 bg="#0F172A", fg="#E2E8F0", insertbackground="white",
                                 relief=tk.SUNKEN, bd=1)
        self._log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_sb = ttk.Scrollbar(log_frame, command=self._log_text.yview)
        log_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_text.configure(yscrollcommand=log_sb.set)
        self._log_text.configure(state=tk.DISABLED)

        ttk.Label(right, text="ℹ 任务详情", style="Title.TLabel").pack(anchor=tk.W, padx=6, pady=(8, 2))
        detail_frame = ttk.Frame(right)
        detail_frame.pack(fill=tk.BOTH, padx=4, pady=4)
        self._detail_text = tk.Text(detail_frame, wrap=tk.WORD, font=("Microsoft YaHei UI", 9),
                                    relief=tk.SUNKEN, bd=1, height=10)
        self._detail_text.pack(fill=tk.BOTH, expand=True)
        self._detail_text.configure(state=tk.DISABLED)

    def _build_task_tree(self, parent, tab_name: str) -> ttk.Treeview:
        container = ttk.Frame(parent)
        container.pack(fill=tk.BOTH, expand=True)

        columns = ("prio", "filename", "copies", "counter", "status", "retry",
                   "paused", "fail_reason", "operator", "updated")
        tree = ttk.Treeview(container, columns=columns, show="headings", selectmode="extended")

        headers = [
            ("prio", "优先级", 60),
            ("filename", "文件名", 260),
            ("copies", "份数", 60),
            ("counter", "柜台", 90),
            ("status", "状态", 80),
            ("retry", "重试/最大", 80),
            ("paused", "暂停", 50),
            ("fail_reason", "失败原因", 220),
            ("operator", "操作者", 90),
            ("updated", "更新时间", 140),
        ]
        for col, text, width in headers:
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor=tk.W, stretch=(col in ("filename", "fail_reason")))

        tree.tag_configure("printing", background="#FEF3C7")
        tree.tag_configure("failed", background="#FEE2E2")
        tree.tag_configure("manual", background="#F3E8FF")
        tree.tag_configure("cancelled", background="#F3F4F6", foreground="#6B7280")
        tree.tag_configure("completed", background="#D1FAE5")
        tree.tag_configure("paused", font=("Microsoft YaHei UI", 9, "italic"))

        vsb = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(container, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        tree.bind("<<TreeviewSelect>>", lambda e: self._on_tree_select(tree))
        tree.bind("<Double-1>", lambda e: self._on_tree_double_click(tree))

        return tree

    def _build_statusbar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(4, 0))

        self._status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self._status_var, anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._worker_status_var = tk.StringVar(value="调度器: 运行中")
        ttk.Label(bar, textvariable=self._worker_status_var, anchor=tk.E).pack(side=tk.RIGHT)

        self._failure_hint = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._failure_hint, foreground="#DC2626",
                  font=("Microsoft YaHei UI", 9, "bold")).pack(side=tk.RIGHT, padx=10)

    def _start_ui_poller(self):
        self.root.after(200, self._ui_poll)

    def _ui_poll(self):
        if self._refresh_needed:
            self._refresh_needed = False
            self._refresh_all_data()
        if self._pending_logs:
            logs = self._pending_logs
            self._pending_logs = []
            self._append_log_raw(logs)
        self._update_worker_status_hint()
        self.root.after(200, self._ui_poll)

    def _on_tasks_changed_ui(self):
        self._refresh_needed = True

    def _append_log_ui(self, msg: str):
        self._pending_logs.append(msg)

    def _append_log_raw(self, msgs: List[str]):
        try:
            self._log_text.configure(state=tk.NORMAL)
            for msg in msgs:
                self._log_text.insert(tk.END, msg + "\n")
            self._log_text.see(tk.END)
            max_lines = 500
            lines = int(self._log_text.index("end-1c").split(".")[0])
            if lines > max_lines:
                self._log_text.delete("1.0", f"{lines - max_lines}.0")
        finally:
            self._log_text.configure(state=tk.DISABLED)

    def _update_operator(self):
        name = self._operator_var.get().strip()
        if not name:
            name = "系统管理员"
            self._operator_var.set(name)
        self.config.operator_name = name
        self.storage.save_config(self.config)
        self.queue.update_config(self.config)
        self._set_status(f"操作者已更新为: {name}")

    def _set_status(self, text: str):
        self._status_var.set(text)

    def _update_worker_status_hint(self):
        running = self.queue.is_worker_running
        global_paused = self.queue.global_paused
        parts = []
        if running:
            if global_paused:
                parts.append("调度器: 已暂停(全局)")
            else:
                parts.append("调度器: 运行中")
        else:
            parts.append("调度器: 未启动")
        if self._printer.is_busy if hasattr(self, '_printer') else False:
            parts.append("| 打印机: 忙碌")
        self._worker_status_var.set(" ".join(parts))

        hint = ""
        if self.config.simulate_failure_enabled and self.config.simulate_failure_rate > 0:
            rate_pct = int(self.config.simulate_failure_rate * 100)
            hint = f"⚠ 模拟失败开启 ({rate_pct}%)"
        self._failure_hint.set(hint)

        btn_text = "▶ 全局继续" if self.queue.global_paused else "⏸ 全局暂停"
        self._global_pause_btn.configure(text=btn_text)

    def _refresh_all(self):
        self._refresh_all_data()
        self._update_worker_status_hint()
        self._set_status(f"系统就绪 | 数据目录: {DATA_DIR}")

    def _refresh_all_data(self):
        tasks = self.queue.tasks
        stats = self.queue.get_statistics()
        for s in TaskStatus:
            self._stats_vars[s.value].set(str(stats.get(s.value, 0)))
        self._total_var.set(str(stats.get("总任务数", 0)))
        self._paused_var.set(str(stats.get("总暂停数", 0)))

        selected_ids = set(self._selected_task_ids)
        for tab_name, (status, tree) in self._tree_map.items():
            self._populate_tree(tree, tasks, status)
            self._restore_tree_selection(tree, selected_ids)

        self._update_detail_panel()

    def _populate_tree(self, tree: ttk.Treeview, tasks: List[PrintTask],
                       filter_status: Optional[TaskStatus]):
        tree.delete(*tree.get_children())
        priorities = self.config.counter_priorities

        filtered = tasks
        if filter_status is not None:
            filtered = [t for t in tasks if t.status == filter_status]

        for t in filtered:
            prio_val = t.priority_override if t.priority_override is not None \
                else priorities.get(t.counter.value, "-")
            tags = []
            if t.status == TaskStatus.PRINTING:
                tags.append("printing")
            elif t.status == TaskStatus.FAILED:
                tags.append("failed")
            elif t.status == TaskStatus.MANUAL:
                tags.append("manual")
            elif t.status == TaskStatus.CANCELLED:
                tags.append("cancelled")
            elif t.status == TaskStatus.COMPLETED:
                tags.append("completed")
            if t.paused:
                tags.append("paused")

            paused_str = "⏸" if t.paused else ""
            retry_str = f"{t.retry_count}/{t.max_retries}"
            updated = time.strftime("%m-%d %H:%M:%S", time.localtime(t.updated_at))

            tree.insert("", tk.END, iid=t.id, values=(
                prio_val,
                t.filename,
                t.copies,
                t.counter.value,
                t.status.value,
                retry_str,
                paused_str,
                t.fail_reason or "",
                t.operator or "",
                updated,
            ), tags=tuple(tags))

    def _restore_tree_selection(self, tree: ttk.Treeview, selected_ids: set):
        for iid in tree.get_children():
            if iid in selected_ids:
                tree.selection_add(iid)

    def _current_tree(self) -> Optional[ttk.Treeview]:
        current_tab_idx = self._tab_control.index(self._tab_control.select())
        if 0 <= current_tab_idx < len(STATUS_TAB_ORDER):
            tab_name, _ = STATUS_TAB_ORDER[current_tab_idx]
            return self._tree_map.get(tab_name, (None, None))[1]
        return None

    def _on_tab_changed(self):
        self._selected_task_ids.clear()
        tree = self._current_tree()
        if tree:
            for iid in tree.selection():
                self._selected_task_ids.add(iid)
        self._update_detail_panel()

    def _on_tree_select(self, tree: ttk.Treeview):
        self._selected_task_ids = set(tree.selection())
        self._update_detail_panel()

    def _on_tree_double_click(self, tree: ttk.Treeview):
        sel = tree.selection()
        if not sel:
            return
        task_id = sel[0]
        task = self.queue.get_task(task_id)
        if not task:
            return
        if task.status == TaskStatus.MANUAL:
            if messagebox.askyesno("人工完成", f"确认将 '{task.filename}' 标记为人工处理完成？"):
                self.queue.mark_manual_done(task_id, self._get_operator())
        elif task.status == TaskStatus.CANCELLED:
            if messagebox.askyesno("撤销取消", f"确认撤销取消 '{task.filename}'？"):
                self.queue.uncancel_task(task_id, self._get_operator())

    def _update_detail_panel(self):
        try:
            self._detail_text.configure(state=tk.NORMAL)
            self._detail_text.delete("1.0", tk.END)

            ids = list(self._selected_task_ids)
            if not ids:
                self._detail_text.insert(tk.END, "请从上方列表选择任务查看详情。\n\n"
                                                 "支持多选后使用工具栏按钮批量操作。\n\n"
                                                 "双击任务可快捷操作：\n"
                                                 "• 需人工处理 → 人工完成\n"
                                                 "• 已取消 → 撤销取消")
                return
            if len(ids) > 1:
                self._detail_text.insert(tk.END, f"已选择 {len(ids)} 个任务。\n\n"
                                                 "可通过上方工具栏按钮进行批量操作。")
                return
            task = self.queue.get_task(ids[0])
            if not task:
                return
            lines = [
                f"任务ID: {task.id}",
                f"文件名: {task.filename}",
                f"份数: {task.copies}",
                f"所属柜台: {task.counter.value}",
                f"状态: {task.status.value}",
                f"重试次数: {task.retry_count} / {task.max_retries}",
                f"暂停: {'是' if task.paused else '否'}",
                f"失败原因: {task.fail_reason or '(无)'}",
                f"最后操作者: {task.operator or '(无)'}",
                f"自定义优先级: {task.priority_override if task.priority_override is not None else '(未设置)'}",
                f"创建时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(task.created_at))}",
                f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(task.started_at)) if task.started_at else '(未开始)'}",
                f"完成时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(task.completed_at)) if task.completed_at else '(未完成)'}",
                f"更新时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(task.updated_at))}",
            ]
            self._detail_text.insert(tk.END, "\n".join(lines))
        finally:
            self._detail_text.configure(state=tk.DISABLED)

    def _get_operator(self) -> str:
        self._update_operator()
        return self._operator_var.get().strip() or "系统管理员"

    def _get_selected_ids(self) -> List[str]:
        return list(self._selected_task_ids)

    def _action_import(self):
        path = filedialog.askopenfilename(
            title="选择任务列表文件",
            filetypes=[("CSV/JSON 任务文件", "*.csv *.json"), ("CSV 文件", "*.csv"),
                       ("JSON 文件", "*.json"), ("所有文件", "*.*")],
            initialdir=str(PROJECT_ROOT),
        )
        if not path:
            return
        try:
            result: ImportResult = TaskImporter.import_file(
                path, default_max_retries=self.config.max_retries_default)
        except (FileNotFoundError, ValueError) as e:
            messagebox.showerror("导入失败", str(e))
            return

        total = len(result.success) + len(result.errors) + len(result.skipped)
        added = 0
        if result.success:
            added = self.queue.add_tasks(result.success)

        msg_lines = [
            f"共读取 {total} 条记录",
            f"✅ 成功导入并添加: {added} 条",
            f"⚠ 跳过（字段异常，已用默认值）: {len(result.skipped)} 条",
            f"❌ 失败（严重错误，未导入）: {len(result.errors)} 条",
        ]

        if result.skipped:
            msg_lines.append("\n--- 跳过记录详情 ---")
            for row_num, row_id, reason in result.skipped[:20]:
                msg_lines.append(f"  [#{row_num}] {row_id} -> {reason}")
            if len(result.skipped) > 20:
                msg_lines.append(f"  ... 另外 {len(result.skipped) - 20} 条已省略")

        if result.errors:
            msg_lines.append("\n--- 失败记录详情 ---")
            for row_num, row_id, reason in result.errors[:20]:
                msg_lines.append(f"  [#{row_num}] {row_id} -> {reason}")
            if len(result.errors) > 20:
                msg_lines.append(f"  ... 另外 {len(result.errors) - 20} 条已省略")

        title = "导入成功" if added > 0 or not result.errors else "导入部分失败"
        messagebox.showinfo(title, "\n".join(msg_lines))
        self._set_status(f"导入完成: 成功{added}条, 跳过{len(result.skipped)}条, 失败{len(result.errors)}条")

    def _action_pause(self):
        ids = self._get_selected_ids()
        if not ids:
            messagebox.showwarning("无选择", "请先在任务列表中选择要暂停的任务")
            return
        cnt = 0
        for tid in ids:
            if self.queue.pause_task(tid, self._get_operator()):
                cnt += 1
        self._set_status(f"已暂停 {cnt}/{len(ids)} 个任务")

    def _action_resume(self):
        ids = self._get_selected_ids()
        if not ids:
            messagebox.showwarning("无选择", "请先在任务列表中选择要继续的任务")
            return
        cnt = 0
        for tid in ids:
            if self.queue.resume_task(tid, self._get_operator()):
                cnt += 1
        self._set_status(f"已继续 {cnt}/{len(ids)} 个任务")

    def _action_retry(self):
        ids = self._get_selected_ids()
        if not ids:
            messagebox.showwarning("无选择", "请先在任务列表中选择要重试的任务")
            return
        cnt = 0
        for tid in ids:
            if self.queue.retry_task(tid, self._get_operator()):
                cnt += 1
        self._set_status(f"已重试 {cnt}/{len(ids)} 个任务")

    def _action_cancel(self):
        ids = self._get_selected_ids()
        if not ids:
            messagebox.showwarning("无选择", "请先在任务列表中选择要取消的任务")
            return
        if not messagebox.askyesno("确认取消", f"确认取消选中的 {len(ids)} 个任务？此操作可通过'撤销取消'恢复。"):
            return
        cnt = 0
        for tid in ids:
            if self.queue.cancel_task(tid, self._get_operator()):
                cnt += 1
        self._set_status(f"已取消 {cnt}/{len(ids)} 个任务")

    def _action_uncancel(self):
        ids = self._get_selected_ids()
        if not ids:
            messagebox.showwarning("无选择", "请先在任务列表中选择要撤销取消的任务")
            return
        cnt = 0
        for tid in ids:
            if self.queue.uncancel_task(tid, self._get_operator()):
                cnt += 1
        if cnt == 0:
            messagebox.showinfo("无操作",
                                "没有任何任务被撤销取消。只有'已取消'状态的任务才能执行撤销取消操作。")
        self._set_status(f"已撤销取消 {cnt}/{len(ids)} 个任务")

    def _action_manual_done(self):
        ids = self._get_selected_ids()
        if not ids:
            messagebox.showwarning("无选择", "请先在任务列表中选择要标记人工完成的任务")
            return
        manual_ids = [tid for tid in ids if self.queue.get_task(tid)
                      and self.queue.get_task(tid).status == TaskStatus.MANUAL]
        if not manual_ids:
            messagebox.showinfo("无操作",
                                "选中任务中没有处于'需人工处理'状态的任务，无法执行人工完成操作。")
            return
        if not messagebox.askyesno("确认", f"确认将 {len(manual_ids)} 个需人工处理任务标记为完成？"):
            return
        cnt = 0
        for tid in manual_ids:
            if self.queue.mark_manual_done(tid, self._get_operator()):
                cnt += 1
        self._set_status(f"人工完成 {cnt}/{len(manual_ids)} 个任务")

    def _action_global_toggle(self):
        op = self._get_operator()
        new_paused = not self.queue.global_paused
        self.queue.set_global_paused(new_paused, operator=op)
        self._set_status(f"全局{'暂停' if new_paused else '继续'}生效")

    def _action_export_history(self):
        self._do_export("history")

    def _action_export_all(self):
        self._do_export("all")

    def _do_export(self, mode: str):
        out_dir = filedialog.askdirectory(title="选择导出目录", initialdir=str(PROJECT_ROOT))
        if not out_dir:
            return
        fmt = messagebox.askquestion("导出格式",
                                     "是 = 导出 CSV (Excel可直接打开)\n否 = 导出 JSON",
                                     icon="question")
        ext = "csv" if fmt == "yes" else "json"

        op = self._get_operator()
        tasks = self.queue.tasks
        if mode == "history":
            result = self.exporter.export_history(tasks, out_dir, op, ext)
        else:
            result = self.exporter.export_all(tasks, out_dir, op, ext)

        if result.success:
            self.config.last_export_record = {
                "file": result.file_path,
                "time": time.time(),
                "count": result.count,
                "operator": op,
            }
            self.storage.save_config(self.config)
            messagebox.showinfo("导出成功", f"{result.message}\n\n共 {result.count} 条记录。")
        else:
            messagebox.showerror("导出失败", result.message)

    def _action_clear_history(self):
        if not messagebox.askyesno("确认", "确认删除所有'已完成'和'已取消'的任务记录？此操作不可恢复！"):
            return
        cnt = self.queue.clear_history(self._get_operator())
        self._set_status(f"已清除 {cnt} 条历史记录")

    def _action_settings(self):
        dlg = SettingsDialog(self.root, self.config)
        self.root.wait_window(dlg)
        if dlg.result is not None:
            self.config = dlg.result
            self.storage.save_config(self.config)
            self.queue.update_config(self.config)
            self._refresh_all_data()
            self._set_status("设置已更新并保存")

    def _on_close(self):
        try:
            self.queue.stop_worker()
        except Exception:
            pass
        self.root.destroy()


class SettingsDialog(tk.Toplevel):
    def __init__(self, master, config: AppConfig):
        super().__init__(master)
        self.title("系统设置")
        self.geometry("560x560")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.result: Optional[AppConfig] = None
        self._config = AppConfig.from_dict(config.to_dict())
        self._build()

    def _build(self):
        pad = {"padx": 12, "pady": 6}
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frm, text="柜台优先级（数值越小越优先，范围 1-999）",
                  style="Title.TLabel").pack(anchor=tk.W, **pad)

        self._prio_vars: dict = {}
        for ct in CounterType:
            row = ttk.Frame(frm)
            row.pack(fill=tk.X, **pad)
            ttk.Label(row, text=f"{ct.value}:", width=12).pack(side=tk.LEFT)
            val = self._config.counter_priorities.get(ct.value, 1)
            v = tk.IntVar(value=val)
            sp = ttk.Spinbox(row, from_=1, to=999, textvariable=v, width=8)
            sp.pack(side=tk.LEFT)
            self._prio_vars[ct.value] = v

        sep = ttk.Separator(frm)
        sep.pack(fill=tk.X, pady=10)

        ttk.Label(frm, text="任务默认配置", style="Title.TLabel").pack(anchor=tk.W, **pad)

        row1 = ttk.Frame(frm)
        row1.pack(fill=tk.X, **pad)
        ttk.Label(row1, text="默认最大重试次数:", width=18).pack(side=tk.LEFT)
        self._max_retries_var = tk.IntVar(value=self._config.max_retries_default)
        ttk.Spinbox(row1, from_=0, to=20, textvariable=self._max_retries_var, width=8).pack(side=tk.LEFT)
        ttk.Label(row1, text="（0表示不自动重试，直接进入人工处理）").pack(side=tk.LEFT, padx=8)

        row2 = ttk.Frame(frm)
        row2.pack(fill=tk.X, **pad)
        ttk.Label(row2, text="单次模拟打印耗时(毫秒):", width=18).pack(side=tk.LEFT)
        self._duration_var = tk.IntVar(value=self._config.print_duration_ms)
        ttk.Spinbox(row2, from_=100, to=60000, increment=100,
                    textvariable=self._duration_var, width=10).pack(side=tk.LEFT)

        sep2 = ttk.Separator(frm)
        sep2.pack(fill=tk.X, pady=10)

        ttk.Label(frm, text="模拟失败开关（用于测试重试与人工处理逻辑）",
                  style="Title.TLabel").pack(anchor=tk.W, **pad)

        row3 = ttk.Frame(frm)
        row3.pack(fill=tk.X, **pad)
        self._fail_enabled_var = tk.BooleanVar(value=self._config.simulate_failure_enabled)
        ttk.Checkbutton(row3, text="启用模拟失败（随机触发卡纸/缺墨/脱机等错误）",
                        variable=self._fail_enabled_var).pack(side=tk.LEFT)

        row4 = ttk.Frame(frm)
        row4.pack(fill=tk.X, **pad)
        ttk.Label(row4, text="模拟失败概率:", width=18).pack(side=tk.LEFT)
        self._fail_rate_var = tk.DoubleVar(value=self._config.simulate_failure_rate)
        rate_scale = ttk.Scale(row4, from_=0.0, to=1.0, variable=self._fail_rate_var,
                               orient=tk.HORIZONTAL, length=200, command=self._on_rate_change)
        rate_scale.pack(side=tk.LEFT, padx=4)
        self._rate_label = ttk.Label(row4, text=self._fmt_rate(self._fail_rate_var.get()))
        self._rate_label.pack(side=tk.LEFT, padx=6)

        hint = ("说明：\n"
                "• 0%: 所有打印任务正常完成\n"
                "• 30%: 每个打印任务约有30%概率随机失败\n"
                "• 100%: 每个任务都会失败，用于压测重试和人工处理\n\n"
                "注意：关闭窗口时设置会自动保存并生效。")
        hl = ttk.Label(frm, text=hint, foreground="#475569", justify=tk.LEFT)
        hl.pack(anchor=tk.W, padx=12, pady=(6, 12))

        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=8)
        ttk.Button(btns, text="保存并应用", command=self._save).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side=tk.RIGHT, padx=4)

    def _fmt_rate(self, v):
        return f"{int(v * 100)}%"

    def _on_rate_change(self, v):
        try:
            fv = float(v)
            self._rate_label.configure(text=self._fmt_rate(fv))
        except (TypeError, ValueError):
            pass

    def _save(self):
        for ct, var in self._prio_vars.items():
            try:
                v = int(var.get())
                v = max(1, min(999, v))
                self._config.counter_priorities[ct] = v
            except (tk.TclError, ValueError):
                pass

        try:
            self._config.max_retries_default = max(0, min(20, int(self._max_retries_var.get())))
        except (tk.TclError, ValueError):
            pass

        try:
            self._config.print_duration_ms = max(100, min(60000, int(self._duration_var.get())))
        except (tk.TclError, ValueError):
            pass

        self._config.simulate_failure_enabled = bool(self._fail_enabled_var.get())
        try:
            self._config.simulate_failure_rate = max(0.0, min(1.0, float(self._fail_rate_var.get())))
        except (tk.TclError, ValueError):
            self._config.simulate_failure_rate = 0.0

        self.result = self._config
        self.destroy()


def main():
    root = tk.Tk()
    try:
        root.iconbitmap(default="")
    except tk.TclError:
        pass
    app = PrintQueueApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
