import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import time
import os
from pathlib import Path
from typing import Optional, List

from core.export_record import (
    ExportRecordManager, ExportRecord, ExportStatus, ExportTrigger,
    ExportFileEntry, ExportRecordUIState,
    ConflictHint, CONFLICT_HINT_LABELS, EXPORT_TRIGGER_LABELS,
    compute_file_hash,
)
from core.review_workbench import ReviewWorkbenchManager, DetailTabState


class ExportRecordDialog(tk.Toplevel):
    def __init__(self, master, manager: ExportRecordManager, operator: str = "系统"):
        super().__init__(master)
        self.title("导出记录中心")
        self.geometry("1050x700")
        self.minsize(900, 600)
        self.transient(master)
        self.grab_set()

        self._manager = manager
        self._operator = operator
        self._records: List[ExportRecord] = []
        self._selected_record: Optional[ExportRecord] = None
        self._review_manager = ReviewWorkbenchManager()

        self._build_ui()
        self._load_initial_state()
        self._refresh_list()

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 11, "bold"))

        main = ttk.Frame(self, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        self._build_header(main)
        self._build_filters(main)
        self._build_body(main)
        self._build_bottom_bar(main)

    def _build_header(self, parent):
        header = ttk.Frame(parent)
        header.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(header, text="🗄 导出记录中心", style="Title.TLabel").pack(side=tk.LEFT)

        btn_bar = ttk.Frame(header)
        btn_bar.pack(side=tk.RIGHT)

        ttk.Button(btn_bar, text="🔄 刷新", command=self._refresh_list).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_bar, text="📂 打开文件位置", command=self._action_open_file_location).pack(side=tk.LEFT, padx=2)

    def _build_filters(self, parent):
        filter_bar = ttk.Frame(parent)
        filter_bar.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(filter_bar, text="状态:", font=("Microsoft YaHei UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 4))
        self._status_filter_var = tk.StringVar(value="全部")
        status_options = ["全部", "成功", "失败", "部分成功"]
        status_combo = ttk.Combobox(filter_bar, textvariable=self._status_filter_var,
                                    values=status_options, state="readonly", width=10)
        status_combo.pack(side=tk.LEFT, padx=(0, 12))
        status_combo.bind("<<ComboboxSelected>>", lambda e: self._on_filter_change())

        ttk.Label(filter_bar, text="类型:", font=("Microsoft YaHei UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 4))
        self._trigger_filter_var = tk.StringVar(value="全部")
        trigger_options = ["全部"] + [EXPORT_TRIGGER_LABELS[t] for t in ExportTrigger]
        trigger_combo = ttk.Combobox(filter_bar, textvariable=self._trigger_filter_var,
                                     values=trigger_options, state="readonly", width=16)
        trigger_combo.pack(side=tk.LEFT, padx=(0, 12))
        trigger_combo.bind("<<ComboboxSelected>>", lambda e: self._on_filter_change())

        ttk.Label(filter_bar, text="搜索:", font=("Microsoft YaHei UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 4))
        self._search_var = tk.StringVar()
        search_entry = ttk.Entry(filter_bar, textvariable=self._search_var, width=20)
        search_entry.pack(side=tk.LEFT, padx=(0, 4))
        search_entry.bind("<Return>", lambda e: self._on_filter_change())
        ttk.Button(filter_bar, text="🔍", command=self._on_filter_change, width=3).pack(side=tk.LEFT)

    def _build_body(self, parent):
        paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        self._build_record_list(left)

        right = ttk.Frame(paned, width=380)
        paned.add(right, weight=2)
        self._build_detail_panel(right)
        self._build_conflict_panel(right)

    def _build_record_list(self, parent):
        columns = ("time", "status", "trigger", "operator", "message", "conflict")
        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        self._tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        self._tree.heading("time", text="导出时间")
        self._tree.heading("status", text="状态")
        self._tree.heading("trigger", text="触发入口")
        self._tree.heading("operator", text="操作者")
        self._tree.heading("message", text="结果摘要")
        self._tree.heading("conflict", text="冲突")
        self._tree.column("time", width=140, anchor=tk.W)
        self._tree.column("status", width=60, anchor=tk.CENTER)
        self._tree.column("trigger", width=120, anchor=tk.W)
        self._tree.column("operator", width=70, anchor=tk.CENTER)
        self._tree.column("message", width=280, anchor=tk.W, stretch=True)
        self._tree.column("conflict", width=80, anchor=tk.CENTER)

        self._tree.tag_configure("success", background="#ECFDF5")
        self._tree.tag_configure("failed", background="#FEE2E2")
        self._tree.tag_configure("partial", background="#FEF3C7")
        self._tree.tag_configure("conflict_row", foreground="#B45309")

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self._tree.bind("<<TreeviewSelect>>", self._on_record_select)
        self._tree.bind("<Double-1>", self._on_record_double_click)

    def _build_detail_panel(self, parent):
        detail_frame = ttk.LabelFrame(parent, text=" 📋 导出详情 ", padding=6)
        detail_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        self._detail_text = tk.Text(detail_frame, wrap=tk.WORD,
                                    font=("Microsoft YaHei UI", 9),
                                    relief=tk.SUNKEN, bd=1, height=22)
        self._detail_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(detail_frame, command=self._detail_text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._detail_text.configure(yscrollcommand=sb.set)
        self._detail_text.configure(state=tk.DISABLED)

    def _build_conflict_panel(self, parent):
        conflict_frame = ttk.LabelFrame(parent, text=" ⚠ 冲突与警告 ", padding=6)
        conflict_frame.pack(fill=tk.X, pady=(0, 4))

        self._conflict_text = tk.Text(conflict_frame, wrap=tk.WORD,
                                      font=("Microsoft YaHei UI", 9),
                                      relief=tk.SUNKEN, bd=1, height=6,
                                      foreground="#B45309")
        self._conflict_text.pack(fill=tk.X)
        self._conflict_text.configure(state=tk.DISABLED)

    def _build_bottom_bar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(6, 0))

        self._count_var = tk.StringVar(value="共 0 条记录")
        ttk.Label(bar, textvariable=self._count_var, foreground="#6B7280").pack(side=tk.LEFT)

        self._auto_snap_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._auto_snap_var, foreground="#10B981",
                  font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT, padx=12)

        ttk.Button(bar, text="关闭", command=self._on_close).pack(side=tk.RIGHT, padx=4)
        ttk.Button(bar, text="🗑 删除选中记录", command=self._action_delete_record).pack(side=tk.RIGHT, padx=4)
        ttk.Button(bar, text="🔍 恢复中心", command=self._action_open_workbench).pack(side=tk.RIGHT, padx=4)

    def _load_initial_state(self):
        ui_state = self._manager.load_ui_state()
        if ui_state.selected_status_filter:
            status_map = {"success": "成功", "failed": "失败", "partial": "部分成功"}
            self._status_filter_var.set(status_map.get(ui_state.selected_status_filter, "全部"))
        if ui_state.selected_trigger_filter:
            try:
                trigger = ExportTrigger(ui_state.selected_trigger_filter)
                self._trigger_filter_var.set(EXPORT_TRIGGER_LABELS.get(trigger, "全部"))
            except ValueError:
                pass
        if ui_state.search_text:
            self._search_var.set(ui_state.search_text)

    def _get_status_filter(self) -> Optional[ExportStatus]:
        val = self._status_filter_var.get()
        if val == "全部":
            return None
        elif val == "成功":
            return ExportStatus.SUCCESS
        elif val == "失败":
            return ExportStatus.FAILED
        elif val == "部分成功":
            return ExportStatus.PARTIAL
        return None

    def _get_trigger_filter(self) -> Optional[ExportTrigger]:
        val = self._trigger_filter_var.get()
        if val == "全部":
            return None
        for trigger, label in EXPORT_TRIGGER_LABELS.items():
            if label == val:
                return trigger
        return None

    def _on_filter_change(self):
        self._save_ui_state()
        self._refresh_list()

    def _refresh_list(self):
        self._tree.delete(*self._tree.get_children())

        status_filter = self._get_status_filter()
        trigger_filter = self._get_trigger_filter()
        search_text = self._search_var.get().strip()

        self._records = self._manager.query_records(
            status_filter=status_filter,
            trigger_filter=trigger_filter,
            search_text=search_text,
        )

        for record in self._records:
            t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.exported_at))
            status_label = {"success": "成功", "failed": "失败", "partial": "部分"}[record.status.value]
            trigger_label = EXPORT_TRIGGER_LABELS.get(record.trigger, record.trigger.value)
            conflict_label = CONFLICT_HINT_LABELS.get(record.conflict_hint, "") if record.conflict_hint != ConflictHint.NONE else ""

            tags = []
            if record.status == ExportStatus.SUCCESS:
                tags.append("success")
            elif record.status == ExportStatus.FAILED:
                tags.append("failed")
            elif record.status == ExportStatus.PARTIAL:
                tags.append("partial")
            if record.conflict_hint != ConflictHint.NONE:
                tags.append("conflict_row")

            self._tree.insert(
                "", tk.END, iid=record.record_id,
                values=(t_str, status_label, trigger_label, record.operator,
                        record.result_message, conflict_label),
                tags=tuple(tags),
            )

        self._count_var.set(f"共 {len(self._records)} 条记录")

    def _on_record_select(self, event):
        sel = self._tree.selection()
        if not sel:
            self._selected_record = None
            self._update_detail_panel()
            self._auto_snap_var.set("")
            return

        record_id = sel[0]
        self._selected_record = self._manager.load_record(record_id)
        self._update_detail_panel()
        self._update_conflict_panel()
        self._save_ui_state()
        self._auto_create_snapshot()

    def _on_record_double_click(self, event):
        sel = self._tree.selection()
        if not sel:
            return
        record_id = sel[0]
        record = self._manager.load_record(record_id)
        if record and record.files:
            file_path = record.files[0].file_path
            if file_path and Path(file_path).exists():
                self._open_file(file_path)
            else:
                messagebox.showwarning("文件不存在", "该导出文件已不存在或路径无效", parent=self)

    def _update_detail_panel(self):
        try:
            self._detail_text.configure(state=tk.NORMAL)
            self._detail_text.delete("1.0", tk.END)

            record = self._selected_record
            if record is None:
                self._detail_text.insert(tk.END, "请从左侧列表选择一条导出记录查看详情。\n\n")
                self._detail_text.insert(tk.END, "双击记录可直接打开导出文件。")
                return

            t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.exported_at))
            status_label = {"success": "✅ 成功", "failed": "❌ 失败", "partial": "⚠ 部分成功"}[record.status.value]
            trigger_label = EXPORT_TRIGGER_LABELS.get(record.trigger, record.trigger.value)

            lines = [
                f"记录ID: {record.record_id}",
                f"导出时间: {t_str}",
                f"触发入口: {trigger_label}",
                f"状态: {status_label}",
                f"操作者: {record.operator}",
                "",
            ]

            if record.filter_snapshot:
                lines.append("【筛选条件快照】")
                for k, v in record.filter_snapshot.items():
                    lines.append(f"  {k}: {v}")
                lines.append("")

            if record.batch_summary:
                lines.append("【批次摘要】")
                for k, v in record.batch_summary.items():
                    if isinstance(v, dict):
                        lines.append(f"  {k}:")
                        for sk, sv in v.items():
                            lines.append(f"    {sk}: {sv}")
                    else:
                        lines.append(f"  {k}: {v}")
                lines.append("")

            if record.files:
                lines.append("【文件清单】")
                for f in record.files:
                    lines.append(f"  文件名: {f.filename}")
                    lines.append(f"  路径: {f.file_path}")
                    lines.append(f"  大小: {f.file_size} 字节")
                    lines.append(f"  行数: {f.row_count}")
                    if f.content_hash:
                        lines.append(f"  哈希: {f.content_hash[:16]}...")
                    lines.append("")

            if record.statistics:
                lines.append("【关键统计】")
                for k, v in record.statistics.items():
                    lines.append(f"  {k}: {v}")
                lines.append("")

            if record.content_hash:
                lines.append(f"内容哈希: {record.content_hash[:16]}...")
            if record.version_tag:
                lines.append(f"版本标记: {record.version_tag}")
            lines.append("")

            if record.result_message:
                lines.append(f"结果: {record.result_message}")
            if record.failure_reason:
                lines.append(f"失败原因: {record.failure_reason}")

            if record.log_entries:
                lines.append("")
                lines.append("【操作日志】")
                for entry in record.log_entries:
                    lines.append(f"  {entry}")

            self._detail_text.insert(tk.END, "\n".join(lines))

        finally:
            self._detail_text.configure(state=tk.DISABLED)

    def _update_conflict_panel(self):
        try:
            self._conflict_text.configure(state=tk.NORMAL)
            self._conflict_text.delete("1.0", tk.END)

            record = self._selected_record
            if record is None:
                return

            conflicts = self._manager.check_file_conflicts(record)

            if record.conflict_hint != ConflictHint.NONE:
                label = CONFLICT_HINT_LABELS.get(record.conflict_hint, "未知")
                self._conflict_text.insert(tk.END, f"[{label}] {record.conflict_detail}\n")

            if conflicts:
                for c in conflicts:
                    for issue in c["issues"]:
                        self._conflict_text.insert(tk.END, f"⚠ {c['filename']}: {issue}\n")
            elif record.conflict_hint == ConflictHint.NONE and not conflicts:
                self._conflict_text.insert(tk.END, "无冲突或异常。")

        finally:
            self._conflict_text.configure(state=tk.DISABLED)

    def _action_open_file_location(self):
        if self._selected_record is None or not self._selected_record.files:
            messagebox.showinfo("提示", "请先选择一条包含文件的导出记录", parent=self)
            return

        file_path = self._selected_record.files[0].file_path
        if not file_path or not Path(file_path).exists():
            messagebox.showwarning("文件不存在", "该导出文件已不存在", parent=self)
            return

        dir_path = str(Path(file_path).parent)
        try:
            os.startfile(dir_path)
        except OSError as e:
            messagebox.showerror("无法打开", f"无法打开文件位置: {e}", parent=self)

    def _action_delete_record(self):
        if self._selected_record is None:
            messagebox.showinfo("提示", "请先选择一条记录", parent=self)
            return

        if not messagebox.askyesno(
            "确认删除",
            f"确认删除导出记录 '{self._selected_record.record_id}'？\n\n此操作仅删除记录，不影响已导出的文件。",
            parent=self
        ):
            return

        records = self._manager.load_all_records()
        records = [r for r in records if r.record_id != self._selected_record.record_id]
        from core.export_record import EXPORT_RECORDS_FILE
        EXPORT_RECORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = EXPORT_RECORDS_FILE.with_suffix(".json.tmp")
        import json
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in records], f, ensure_ascii=False, indent=2)
        os.replace(tmp, EXPORT_RECORDS_FILE)

        self._selected_record = None
        self._refresh_list()
        self._update_detail_panel()
        self._update_conflict_panel()

    def _open_file(self, file_path: str):
        try:
            os.startfile(file_path)
        except OSError as e:
            messagebox.showerror("无法打开", f"无法打开文件: {e}", parent=self)

    def _save_ui_state(self):
        state = ExportRecordUIState(
            selected_status_filter=self._get_status_filter().value if self._get_status_filter() else None,
            selected_trigger_filter=self._get_trigger_filter().value if self._get_trigger_filter() else None,
            search_text=self._search_var.get().strip(),
            last_viewed_record_id=self._selected_record.record_id if self._selected_record else None,
        )
        self._manager.save_ui_state(state)

    def _auto_create_snapshot(self):
        if self._selected_record is None:
            return

        detail_state = DetailTabState(
            tab_index=0,
            scroll_position=0.0,
            expanded_sections=[],
        )

        try:
            scroll_pos = self._detail_text.yview()[0]
            detail_state.scroll_position = scroll_pos
        except (tk.TclError, IndexError):
            pass

        filter_snapshot = {
            "status_filter": self._get_status_filter().value if self._get_status_filter() else None,
            "trigger_filter": self._get_trigger_filter().value if self._get_trigger_filter() else None,
            "search_text": self._search_var.get().strip(),
        }

        detail_state.filter_conditions = filter_snapshot

        batch_context = None
        if self._selected_record.batch_summary:
            batch_context = dict(self._selected_record.batch_summary)

        snapshot = self._review_manager.auto_snapshot(
            record_id=self._selected_record.record_id,
            detail_state=detail_state,
            filter_snapshot=filter_snapshot,
            batch_context=batch_context,
        )

        self._auto_snap_var.set(f"✅ 已自动保存查看现场: {snapshot.title[:25]}")

    def _action_open_workbench(self):
        from ui.review_workbench_dialog import ReviewWorkbenchDialog
        dlg = ReviewWorkbenchDialog(
            self, self._review_manager,
            record_manager=self._manager,
            operator=self._operator,
        )
        self.wait_window(dlg)
        self._refresh_list()

    def _on_close(self):
        if self._selected_record:
            self._auto_create_snapshot()
        self._save_ui_state()
        self.destroy()
