import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import time
import os
from pathlib import Path
from typing import Optional, List

from core.review_workbench import (
    ReviewWorkbenchManager, ReviewSnapshot, DetailTabState,
    SnapshotStatus, SNAPSHOT_STATUS_LABELS, ImportResult,
)
from core.export_record import (
    ExportRecordManager, ExportRecord, ExportFileEntry,
    ExportStatus, ExportTrigger, EXPORT_TRIGGER_LABELS,
    ConflictHint, CONFLICT_HINT_LABELS,
)


DETAIL_TABS = ["详情概览", "文件清单", "操作日志", "冲突检查"]


class ReviewWorkbenchDialog(tk.Toplevel):
    def __init__(self, master, manager: ReviewWorkbenchManager,
                 record_manager: Optional[ExportRecordManager] = None,
                 operator: str = "系统", initial_snapshot_id: Optional[str] = None):
        super().__init__(master)
        self.title("导出回看工作台")
        self.geometry("1200x750")
        self.minsize(1000, 600)
        self.transient(master)
        self.grab_set()

        self._manager = manager
        self._record_manager = record_manager or ExportRecordManager()
        self._operator = operator
        self._snapshots: List[ReviewSnapshot] = []
        self._current_snapshot: Optional[ReviewSnapshot] = None
        self._current_record: Optional[ExportRecord] = None
        self._auto_save_enabled = True

        self._build_ui()
        self._refresh_snapshot_list()

        if initial_snapshot_id:
            self._select_snapshot_by_id(initial_snapshot_id)
        else:
            last = self._manager.get_last_snapshot()
            if last:
                self._select_snapshot_by_id(last.snapshot_id)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 9, "bold"))

        main = ttk.Frame(self, padding=6)
        main.pack(fill=tk.BOTH, expand=True)

        self._build_toolbar(main)
        self._build_body(main)
        self._build_status_bar(main)

    def _build_toolbar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(bar, text="🔍 导出回看工作台", style="Title.TLabel").pack(side=tk.LEFT)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Button(bar, text="📌 置顶", command=self._action_pin).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="✏ 重命名", command=self._action_rename).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="🗑 删除", command=self._action_delete).pack(side=tk.LEFT, padx=2)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Button(bar, text="⬅ 上一条", command=self._action_prev).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="➡ 下一条", command=self._action_next).pack(side=tk.LEFT, padx=2)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Button(bar, text="📤 导出快照", command=self._action_export).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="📥 导入快照", command=self._action_import).pack(side=tk.LEFT, padx=2)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Button(bar, text="🔄 刷新", command=self._refresh_snapshot_list).pack(side=tk.LEFT, padx=2)

        ttk.Button(bar, text="关闭", command=self._on_close).pack(side=tk.RIGHT, padx=4)

        self._auto_save_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动保存状态", variable=self._auto_save_var,
                        command=self._on_auto_save_toggle).pack(side=tk.RIGHT, padx=8)

    def _build_body(self, parent):
        paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(paned, width=260)
        paned.add(left, weight=1)
        self._build_snapshot_list(left)

        right = ttk.Frame(paned)
        paned.add(right, weight=3)
        self._build_detail_panel(right)

    def _build_snapshot_list(self, parent):
        list_frame = ttk.LabelFrame(parent, text=" 📋 最近查看快照 ", padding=4)
        list_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("title", "status", "time")
        self._snapshot_tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="browse"
        )
        self._snapshot_tree.heading("title", text="标题")
        self._snapshot_tree.heading("status", text="状态")
        self._snapshot_tree.heading("time", text="查看时间")
        self._snapshot_tree.column("title", width=140, anchor=tk.W)
        self._snapshot_tree.column("status", width=60, anchor=tk.CENTER)
        self._snapshot_tree.column("time", width=100, anchor=tk.W)

        self._snapshot_tree.tag_configure("pinned", background="#FEF3C7")
        self._snapshot_tree.tag_configure("normal", background="white")
        self._snapshot_tree.tag_configure("warning", foreground="#B45309")
        self._snapshot_tree.tag_configure("error", foreground="#DC2626")

        vsb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self._snapshot_tree.yview)
        self._snapshot_tree.configure(yscrollcommand=vsb.set)

        self._snapshot_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        self._snapshot_tree.bind("<<TreeviewSelect>>", self._on_snapshot_select)
        self._snapshot_tree.bind("<Double-1>", self._on_snapshot_double_click)

        batch_frame = ttk.LabelFrame(parent, text=" 🔗 同批次快照 ", padding=4)
        batch_frame.pack(fill=tk.X, pady=(6, 0))

        self._batch_list = tk.Listbox(batch_frame, height=4, font=("Microsoft YaHei UI", 9))
        self._batch_list.pack(fill=tk.X)
        self._batch_list.bind("<<ListboxSelect>>", self._on_batch_select)

    def _build_detail_panel(self, parent):
        header = ttk.Frame(parent)
        header.pack(fill=tk.X, pady=(0, 4))

        self._snapshot_title_var = tk.StringVar(value="请选择一个快照")
        ttk.Label(header, textvariable=self._snapshot_title_var,
                  style="Title.TLabel").pack(side=tk.LEFT)

        status_frame = ttk.Frame(header)
        status_frame.pack(side=tk.RIGHT)
        self._snapshot_status_var = tk.StringVar(value="")
        self._status_label = ttk.Label(status_frame, textvariable=self._snapshot_status_var,
                                       foreground="#6B7280")
        self._status_label.pack()

        self._detail_notebook = ttk.Notebook(parent)
        self._detail_notebook.pack(fill=tk.BOTH, expand=True)
        self._detail_notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._tab_frames = {}
        self._tab_texts = {}

        for i, tab_name in enumerate(DETAIL_TABS):
            frame = ttk.Frame(self._detail_notebook, padding=6)
            self._detail_notebook.add(frame, text=f"  {tab_name}  ")
            self._tab_frames[tab_name] = frame

            text_widget = tk.Text(frame, wrap=tk.WORD,
                                  font=("Microsoft YaHei UI", 9),
                                  relief=tk.SUNKEN, bd=1)
            text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            sb = ttk.Scrollbar(frame, command=text_widget.yview)
            sb.pack(side=tk.RIGHT, fill=tk.Y)
            text_widget.configure(yscrollcommand=sb.set)
            text_widget.configure(state=tk.DISABLED)
            text_widget.bind("<MouseWheel>", self._on_detail_scroll)
            text_widget.bind("<KeyRelease>", self._on_detail_scroll)
            self._tab_texts[tab_name] = text_widget

        self._health_frame = ttk.LabelFrame(parent, text=" ⚠ 快照健康检查 ", padding=4)
        self._health_frame.pack(fill=tk.X, pady=(6, 0))
        self._health_text = tk.Text(self._health_frame, wrap=tk.WORD,
                                    font=("Microsoft YaHei UI", 9),
                                    relief=tk.SUNKEN, bd=1, height=3,
                                    foreground="#B45309")
        self._health_text.pack(fill=tk.X)
        self._health_text.configure(state=tk.DISABLED)

        file_frame = ttk.LabelFrame(parent, text=" 📄 关联文件预览 ", padding=4)
        file_frame.pack(fill=tk.X, pady=(6, 0))

        file_bar = ttk.Frame(file_frame)
        file_bar.pack(fill=tk.X)
        ttk.Label(file_bar, text="文件:").pack(side=tk.LEFT)
        self._file_combo = ttk.Combobox(file_bar, state="readonly", width=40)
        self._file_combo.pack(side=tk.LEFT, padx=4)
        self._file_combo.bind("<<ComboboxSelected>>", self._on_file_select)
        ttk.Button(file_bar, text="打开文件", command=self._action_open_file).pack(side=tk.LEFT, padx=4)

        self._file_preview = tk.Text(file_frame, wrap=tk.NONE,
                                     font=("Consolas", 9),
                                     relief=tk.SUNKEN, bd=1, height=6,
                                     bg="#0F172A", fg="#E2E8F0")
        self._file_preview.pack(fill=tk.X, pady=(4, 0))
        self._file_preview.configure(state=tk.DISABLED)

    def _build_status_bar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(4, 0))

        self._count_var = tk.StringVar(value="共 0 个快照")
        ttk.Label(bar, textvariable=self._count_var, foreground="#6B7280").pack(side=tk.LEFT)

        self._nav_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._nav_var, foreground="#6B7280").pack(side=tk.RIGHT)

    def _refresh_snapshot_list(self):
        self._snapshot_tree.delete(*self._snapshot_tree.get_children())

        self._snapshots = self._manager.list_snapshots()

        for s in self._snapshots:
            t_str = time.strftime("%m-%d %H:%M", time.localtime(s.updated_at))
            status_label = SNAPSHOT_STATUS_LABELS.get(s.status, "")

            tags = []
            if s.is_pinned:
                tags.append("pinned")
            else:
                tags.append("normal")

            if s.status in (SnapshotStatus.FILE_MISSING, SnapshotStatus.RECORD_GONE):
                tags.append("error")
            elif s.status in (SnapshotStatus.CONTENT_CHANGED, SnapshotStatus.PERMISSION_DENIED):
                tags.append("warning")

            title = f"📌 {s.title}" if s.is_pinned else s.title
            if len(title) > 28:
                title = title[:26] + "..."

            self._snapshot_tree.insert(
                "", tk.END, iid=s.snapshot_id,
                values=(title, status_label, t_str),
                tags=tuple(tags),
            )

        self._count_var.set(f"共 {len(self._snapshots)} 个快照")

    def _select_snapshot_by_id(self, snapshot_id: str):
        if snapshot_id in self._snapshot_tree.get_children():
            self._snapshot_tree.selection_set(snapshot_id)
            self._snapshot_tree.see(snapshot_id)
            self._load_snapshot_detail(snapshot_id)

    def _on_snapshot_select(self, event):
        sel = self._snapshot_tree.selection()
        if not sel:
            return
        self._save_current_state()
        self._load_snapshot_detail(sel[0])

    def _on_snapshot_double_click(self, event):
        sel = self._snapshot_tree.selection()
        if not sel:
            return
        snapshot = self._manager.get_snapshot(sel[0])
        if snapshot:
            self._manager.set_last_snapshot(sel[0])

    def _load_snapshot_detail(self, snapshot_id: str):
        snapshot = self._manager.get_snapshot(snapshot_id)
        if not snapshot:
            return

        self._current_snapshot = snapshot
        self._current_record = self._manager.get_snapshot_record(snapshot)

        self._snapshot_title_var.set(
            f"{'📌 ' if snapshot.is_pinned else ''}{snapshot.title}"
        )
        status_text = SNAPSHOT_STATUS_LABELS.get(snapshot.status, "")
        self._snapshot_status_var.set(f"状态: {status_text}")

        if snapshot.status == SnapshotStatus.NORMAL:
            self._status_label.configure(foreground="#10B981")
        elif snapshot.status == SnapshotStatus.CONTENT_CHANGED:
            self._status_label.configure(foreground="#F59E0B")
        else:
            self._status_label.configure(foreground="#EF4444")

        self._populate_detail_tabs()
        self._populate_health_panel()
        self._populate_file_list()
        self._populate_batch_list()
        self._restore_detail_state()

        self._manager.set_last_snapshot(snapshot_id)
        self._update_nav_info()

    def _populate_detail_tabs(self):
        record = self._current_record
        if not record:
            for text in self._tab_texts.values():
                text.configure(state=tk.NORMAL)
                text.delete("1.0", tk.END)
                text.insert(tk.END, "无记录数据")
                text.configure(state=tk.DISABLED)
            return

        t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.exported_at))
        status_label = {"success": "✅ 成功", "failed": "❌ 失败", "partial": "⚠ 部分成功"}[record.status.value]
        trigger_label = EXPORT_TRIGGER_LABELS.get(record.trigger, record.trigger.value)

        overview_lines = [
            f"记录ID: {record.record_id}",
            f"导出时间: {t_str}",
            f"触发入口: {trigger_label}",
            f"状态: {status_label}",
            f"操作者: {record.operator}",
            f"结果消息: {record.result_message}",
            f"失败原因: {record.failure_reason or '(无)'}",
            "",
            f"内容哈希: {record.content_hash[:32]}..." if record.content_hash else "",
            f"版本标记: {record.version_tag}" if record.version_tag else "",
            "",
        ]

        if record.filter_snapshot:
            overview_lines.append("【筛选条件快照】")
            for k, v in record.filter_snapshot.items():
                overview_lines.append(f"  {k}: {v}")
            overview_lines.append("")

        if record.statistics:
            overview_lines.append("【关键统计】")
            for k, v in record.statistics.items():
                overview_lines.append(f"  {k}: {v}")
            overview_lines.append("")

        if record.batch_summary:
            overview_lines.append("【批次摘要】")
            for k, v in record.batch_summary.items():
                if isinstance(v, dict):
                    overview_lines.append(f"  {k}:")
                    for sk, sv in v.items():
                        overview_lines.append(f"    {sk}: {sv}")
                else:
                    overview_lines.append(f"  {k}: {v}")

        self._set_tab_text("详情概览", "\n".join(overview_lines))

        file_lines = []
        for i, f in enumerate(record.files):
            file_lines.append(f"[{i+1}] {f.filename}")
            file_lines.append(f"    路径: {f.file_path}")
            file_lines.append(f"    大小: {f.file_size} 字节")
            file_lines.append(f"    行数: {f.row_count}")
            if f.content_hash:
                file_lines.append(f"    哈希: {f.content_hash[:24]}...")
            file_lines.append("")
        if not record.files:
            file_lines.append("(无文件)")
        self._set_tab_text("文件清单", "\n".join(file_lines))

        log_lines = []
        if record.log_entries:
            for entry in record.log_entries:
                log_lines.append(f"  {entry}")
        else:
            log_lines.append("(无操作日志)")
        self._set_tab_text("操作日志", "\n".join(log_lines))

        conflict_lines = []
        if record.conflict_hint != ConflictHint.NONE:
            conflict_lines.append(
                f"冲突类型: {CONFLICT_HINT_LABELS.get(record.conflict_hint, '未知')}"
            )
            conflict_lines.append(f"详情: {record.conflict_detail}")
            conflict_lines.append("")

        conflicts = self._record_manager.check_file_conflicts(record)
        if conflicts:
            conflict_lines.append("【文件冲突检查】")
            for c in conflicts:
                for issue in c["issues"]:
                    conflict_lines.append(f"  ⚠ {c['filename']}: {issue}")
        else:
            conflict_lines.append("未检测到文件冲突。")
        self._set_tab_text("冲突检查", "\n".join(conflict_lines))

    def _set_tab_text(self, tab_name: str, text: str):
        if tab_name in self._tab_texts:
            widget = self._tab_texts[tab_name]
            widget.configure(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.insert(tk.END, text)
            widget.configure(state=tk.DISABLED)

    def _populate_health_panel(self):
        snapshot = self._current_snapshot
        if not snapshot:
            return

        health = self._manager.check_snapshot_health(snapshot)

        self._health_text.configure(state=tk.NORMAL)
        self._health_text.delete("1.0", tk.END)

        issues = []
        for issue in health.get("issues", []):
            icon = "⚠" if issue.get("severity") == "warning" else "ℹ"
            issues.append(f"{icon} {issue['message']}")

        for fissue in health.get("file_issues", []):
            for issue in fissue["issues"]:
                issues.append(f"📄 {fissue['filename']}: {issue}")

        if issues:
            self._health_text.insert(tk.END, "\n".join(issues))
            self._health_text.configure(foreground="#B45309")
        else:
            self._health_text.insert(tk.END, "✅ 快照状态正常，所有源文件均可访问")
            self._health_text.configure(foreground="#10B981")

        self._health_text.configure(state=tk.DISABLED)

    def _populate_file_list(self):
        record = self._current_record
        if not record or not record.files:
            self._file_combo["values"] = []
            self._file_combo.set("")
            self._file_preview.configure(state=tk.NORMAL)
            self._file_preview.delete("1.0", tk.END)
            self._file_preview.configure(state=tk.DISABLED)
            return

        filenames = [f.filename for f in record.files]
        self._file_combo["values"] = filenames

        snapshot = self._current_snapshot
        if snapshot and snapshot.detail_state.selected_file_index < len(record.files):
            idx = snapshot.detail_state.selected_file_index
        else:
            idx = 0

        if idx < len(filenames):
            self._file_combo.current(idx)
            self._update_file_preview(idx)

    def _on_file_select(self, event):
        idx = self._file_combo.current()
        if idx >= 0:
            self._update_file_preview(idx)
            self._save_current_state()

    def _update_file_preview(self, file_index: int):
        record = self._current_record
        if not record or file_index >= len(record.files):
            return

        file_entry = record.files[file_index]
        file_path = file_entry.file_path

        self._file_preview.configure(state=tk.NORMAL)
        self._file_preview.delete("1.0", tk.END)

        if not file_path or not Path(file_path).exists():
            self._file_preview.insert(tk.END, "文件不存在，无法预览")
            self._file_preview.configure(state=tk.DISABLED)
            return

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                preview = f.read(2000)
                if len(preview) >= 2000:
                    preview += "\n... (仅显示前2000字符)"
            self._file_preview.insert(tk.END, preview)
        except PermissionError:
            self._file_preview.insert(tk.END, "权限不足，无法预览")
        except Exception as e:
            self._file_preview.insert(tk.END, f"读取失败: {e}")

        self._file_preview.configure(state=tk.DISABLED)

    def _populate_batch_list(self):
        self._batch_list.delete(0, tk.END)

        snapshot = self._current_snapshot
        if not snapshot:
            return

        batch_snaps = self._manager.get_batch_context_snapshots(snapshot)
        for bs in batch_snaps:
            label = f"{'📌 ' if bs.is_pinned else ''}{bs.title[:30]}"
            self._batch_list.insert(tk.END, label)

        if not batch_snaps:
            self._batch_list.insert(tk.END, "(无同批次快照)")

    def _on_batch_select(self, event):
        sel = self._batch_list.curselection()
        if not sel or not self._current_snapshot:
            return

        batch_snaps = self._manager.get_batch_context_snapshots(self._current_snapshot)
        idx = sel[0]
        if idx < len(batch_snaps):
            self._select_snapshot_by_id(batch_snaps[idx].snapshot_id)

    def _restore_detail_state(self):
        snapshot = self._current_snapshot
        if not snapshot:
            return

        state = snapshot.detail_state

        if 0 <= state.tab_index < len(DETAIL_TABS):
            self._detail_notebook.select(state.tab_index)

        current_tab = DETAIL_TABS[state.tab_index] if state.tab_index < len(DETAIL_TABS) else DETAIL_TABS[0]
        if current_tab in self._tab_texts:
            text_widget = self._tab_texts[current_tab]
            try:
                text_widget.yview_moveto(state.scroll_position)
            except tk.TclError:
                pass

    def _save_current_state(self):
        if not self._auto_save_enabled or not self._current_snapshot:
            return

        tab_index = self._detail_notebook.index(self._detail_notebook.select())
        current_tab = DETAIL_TABS[tab_index] if tab_index < len(DETAIL_TABS) else DETAIL_TABS[0]

        scroll_pos = 0.0
        if current_tab in self._tab_texts:
            try:
                scroll_pos = self._tab_texts[current_tab].yview()[0]
            except (tk.TclError, IndexError):
                pass

        file_index = self._file_combo.current()
        if file_index < 0:
            file_index = 0

        state = DetailTabState(
            tab_index=tab_index,
            scroll_position=scroll_pos,
            selected_file_index=file_index,
        )

        self._manager.update_snapshot(
            self._current_snapshot.snapshot_id,
            detail_state=state,
        )

    def _on_tab_changed(self, event):
        self._save_current_state()

    def _on_detail_scroll(self, event):
        if self._auto_save_enabled and self._current_snapshot:
            self.after(300, self._save_current_state)

    def _on_auto_save_toggle(self):
        self._auto_save_enabled = self._auto_save_var.get()

    def _update_nav_info(self):
        if not self._current_snapshot:
            self._nav_var.set("")
            return

        prev_s, next_s = self._manager.get_adjacent_snapshots(self._current_snapshot.snapshot_id)
        parts = []
        if prev_s:
            parts.append(f"⬅ {prev_s.title[:15]}...")
        if next_s:
            parts.append(f"{next_s.title[:15]}... ➡")
        self._nav_var.set("  |  ".join(parts) if parts else "")

    def _action_pin(self):
        if not self._current_snapshot:
            messagebox.showinfo("提示", "请先选择一个快照", parent=self)
            return

        new_pinned = not self._current_snapshot.is_pinned
        self._manager.pin_snapshot(self._current_snapshot.snapshot_id, new_pinned)
        self._current_snapshot.is_pinned = new_pinned
        self._refresh_snapshot_list()
        self._select_snapshot_by_id(self._current_snapshot.snapshot_id)

    def _action_rename(self):
        if not self._current_snapshot:
            messagebox.showinfo("提示", "请先选择一个快照", parent=self)
            return

        new_title = simpledialog.askstring(
            "重命名快照", "请输入新标题:",
            initialvalue=self._current_snapshot.title,
            parent=self
        )
        if new_title and new_title.strip():
            self._manager.update_snapshot(
                self._current_snapshot.snapshot_id,
                title=new_title.strip(),
            )
            self._current_snapshot.title = new_title.strip()
            self._refresh_snapshot_list()
            self._select_snapshot_by_id(self._current_snapshot.snapshot_id)

    def _action_delete(self):
        if not self._current_snapshot:
            messagebox.showinfo("提示", "请先选择一个快照", parent=self)
            return

        if not messagebox.askyesno(
            "确认删除",
            f"确认删除快照 '{self._current_snapshot.title}'？\n\n此操作不可恢复。",
            parent=self
        ):
            return

        snapshot_id = self._current_snapshot.snapshot_id
        self._manager.delete_snapshot(snapshot_id)
        self._current_snapshot = None
        self._current_record = None
        self._refresh_snapshot_list()

        remaining = self._snapshots
        if remaining:
            self._select_snapshot_by_id(remaining[0].snapshot_id)
        else:
            self._snapshot_title_var.set("请选择一个快照")
            self._snapshot_status_var.set("")

    def _action_prev(self):
        if not self._current_snapshot:
            return
        prev_s, _ = self._manager.get_adjacent_snapshots(self._current_snapshot.snapshot_id)
        if prev_s:
            self._select_snapshot_by_id(prev_s.snapshot_id)
        else:
            messagebox.showinfo("提示", "已经是第一条快照了", parent=self)

    def _action_next(self):
        if not self._current_snapshot:
            return
        _, next_s = self._manager.get_adjacent_snapshots(self._current_snapshot.snapshot_id)
        if next_s:
            self._select_snapshot_by_id(next_s.snapshot_id)
        else:
            messagebox.showinfo("提示", "已经是最后一条快照了", parent=self)

    def _action_export(self):
        path = filedialog.asksaveasfilename(
            title="导出快照",
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
            initialfile=f"review_snapshots_{time.strftime('%Y%m%d_%H%M%S')}.json",
            parent=self,
        )
        if not path:
            return

        success, msg = self._manager.export_snapshots(path)
        if success:
            messagebox.showinfo("导出成功", msg, parent=self)
        else:
            messagebox.showerror("导出失败", msg, parent=self)

    def _action_import(self):
        path = filedialog.askopenfilename(
            title="导入快照",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
            parent=self,
        )
        if not path:
            return

        strategy = messagebox.askquestion(
            "冲突处理",
            "遇到同名快照时如何处理？\n\n"
            "是 = 跳过（保留现有）\n"
            "否 = 覆盖（用导入的替换）\n"
            "取消 = 重命名后导入",
            icon="question",
            parent=self,
        )

        if strategy == "yes":
            conflict_strategy = "skip"
        elif strategy == "no":
            conflict_strategy = "overwrite"
        else:
            conflict_strategy = "rename"

        result = self._manager.import_snapshots(path, conflict_strategy)

        if result.success:
            msg_lines = result.messages[-3:]
            messagebox.showinfo(
                "导入完成",
                f"成功导入 {result.imported_count} 个快照\n\n" + "\n".join(msg_lines),
                parent=self,
            )
            self._refresh_snapshot_list()
        else:
            messagebox.showerror(
                "导入失败",
                "\n".join(result.messages),
                parent=self,
            )

    def _action_open_file(self):
        record = self._current_record
        if not record or not record.files:
            messagebox.showinfo("提示", "当前记录没有关联文件", parent=self)
            return

        idx = self._file_combo.current()
        if idx < 0 or idx >= len(record.files):
            idx = 0

        file_path = record.files[idx].file_path
        if not file_path or not Path(file_path).exists():
            messagebox.showwarning("文件不存在", "该文件已不存在或路径无效", parent=self)
            return

        try:
            os.startfile(file_path)
        except OSError as e:
            messagebox.showerror("无法打开", f"无法打开文件: {e}", parent=self)

    def _on_close(self):
        self._save_current_state()
        self.destroy()
