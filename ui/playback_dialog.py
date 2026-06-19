import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import time
from pathlib import Path
from typing import Optional, List

from core.models import PrintTask, CounterType, TaskStatus
from core.batch_playback import (
    BatchPlaybackManager, ImportBatch, PlaybackItem, PlaybackGroup,
    GROUP_LABELS, BatchStatus, PlaybackUIState, ConflictCandidate,
)
from core.preflight import ConflictResolution, ConflictType, RESOLUTION_LABELS


class PlaybackDialog(tk.Toplevel):
    def __init__(self, master, manager: BatchPlaybackManager,
                 queue_manager=None, operator: str = "系统"):
        super().__init__(master)
        self.title("导入决策回放台")
        self.geometry("1100x750")
        self.minsize(950, 600)
        self.transient(master)
        self.grab_set()

        self._manager = manager
        self._queue = queue_manager
        self._operator = operator
        self._current_batch: Optional[ImportBatch] = None
        self._selected_group: Optional[PlaybackGroup] = None
        self._expanded_items: set = set()
        self._selected_item_index: Optional[int] = None

        self._build_ui()
        self._load_initial_state()

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("SubTitle.TLabel", font=("Microsoft YaHei UI", 10, "bold"))

        main = ttk.Frame(self, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        self._build_header(main)
        self._build_group_filter(main)
        self._build_body(main)
        self._build_bottom_bar(main)

    def _build_header(self, parent):
        header = ttk.Frame(parent)
        header.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(header, text="📋 导入决策回放台", style="Title.TLabel").pack(side=tk.LEFT)

        btn_bar = ttk.Frame(header)
        btn_bar.pack(side=tk.RIGHT)

        ttk.Button(btn_bar, text="📂 新建导入批次", command=self._action_new_batch).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_bar, text="📤 导出审计包", command=self._action_export_audit).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_bar, text="📜 查看最近已提交", command=self._action_view_last_submitted).pack(side=tk.LEFT, padx=2)

        batch_info_bar = ttk.Frame(parent)
        batch_info_bar.pack(fill=tk.X, pady=(0, 4))
        self._batch_info_var = tk.StringVar(value="暂无批次")
        ttk.Label(batch_info_bar, textvariable=self._batch_info_var,
                  foreground="#6B7280", font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)

    def _build_group_filter(self, parent):
        filter_bar = ttk.Frame(parent)
        filter_bar.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(filter_bar, text="分组筛选:", font=("Microsoft YaHei UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 8))

        self._group_vars: dict = {}
        self._group_chips: dict = {}

        groups = [
            (None, "全部", "#374151"),
            (PlaybackGroup.SUCCESS, "正常导入", "#10B981"),
            (PlaybackGroup.AUTO_FIXABLE, "默认值兜底", "#F59E0B"),
            (PlaybackGroup.DUPLICATE_CONFLICT, "冲突待决", "#EF4444"),
            (PlaybackGroup.UNIMPORTABLE, "无法导入", "#6B7280"),
        ]

        for group, label, color in groups:
            chip = tk.Frame(filter_bar, bg=color, padx=10, pady=3, cursor="hand2")
            chip.pack(side=tk.LEFT, padx=2)
            lbl = tk.Label(chip, text=label, bg=color, fg="white",
                           font=("Microsoft YaHei UI", 9, "bold"))
            lbl.pack()
            chip.bind("<Button-1>", lambda e, g=group: self._on_group_click(g))
            lbl.bind("<Button-1>", lambda e, g=group: self._on_group_click(g))
            self._group_chips[group] = (chip, lbl)

        sep = ttk.Separator(parent, orient=tk.HORIZONTAL)
        sep.pack(fill=tk.X, pady=(4, 6))

    def _build_body(self, parent):
        paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(paned)
        paned.add(left, weight=3)

        self._build_item_tree(left)

        right = ttk.Frame(paned, width=380)
        paned.add(right, weight=2)

        self._build_detail_panel(right)
        self._build_timeline_panel(right)

    def _build_item_tree(self, parent):
        header = ttk.Frame(parent)
        header.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(header, text="📄 条目列表", style="SubTitle.TLabel").pack(side=tk.LEFT)

        btn_bar = ttk.Frame(header)
        btn_bar.pack(side=tk.RIGHT)

        ttk.Button(btn_bar, text="全选本组", command=self._action_select_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_bar, text="全不选", command=self._action_select_none).pack(side=tk.LEFT, padx=2)

        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("selected", "row", "group", "identifier", "status")
        self._tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        self._tree.heading("selected", text="导入")
        self._tree.heading("row", text="行号")
        self._tree.heading("group", text="分组")
        self._tree.heading("identifier", text="条目标识")
        self._tree.heading("status", text="状态/原因")
        self._tree.column("selected", width=50, anchor=tk.CENTER)
        self._tree.column("row", width=55, anchor=tk.CENTER)
        self._tree.column("group", width=80, anchor=tk.CENTER)
        self._tree.column("identifier", width=260, anchor=tk.W)
        self._tree.column("status", width=300, anchor=tk.W, stretch=True)

        self._tree.tag_configure("success", background="#ECFDF5")
        self._tree.tag_configure("fixable", background="#FFFBEB")
        self._tree.tag_configure("conflict", background="#FEF2F2")
        self._tree.tag_configure("unimportable", background="#F3F4F6", foreground="#6B7280")
        self._tree.tag_configure("selected", font=("Microsoft YaHei UI", 9, "bold"))

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self._tree.xview)
        self._tree.configure(xscrollcommand=hsb.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self._tree.bind("<<TreeviewSelect>>", self._on_item_select)
        self._tree.bind("<Button-1>", self._on_tree_click)
        self._tree.bind("<Double-1>", self._on_tree_double_click)

    def _build_detail_panel(self, parent):
        detail_frame = ttk.LabelFrame(parent, text=" 🔍 详情对比 ", padding=6)
        detail_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        self._detail_text = tk.Text(detail_frame, wrap=tk.WORD,
                                    font=("Consolas", 9),
                                    relief=tk.SUNKEN, bd=1, height=18)
        self._detail_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(detail_frame, command=self._detail_text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._detail_text.configure(yscrollcommand=sb.set)
        self._detail_text.configure(state=tk.DISABLED)

        action_frame = ttk.Frame(parent)
        action_frame.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(action_frame, text="⚙ 冲突决策:", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor=tk.W, pady=(2, 2))

        self._resolution_var = tk.StringVar(value="")
        resolutions = [
            (ConflictResolution.SKIP, "跳过（不导入）"),
            (ConflictResolution.KEEP_BOTH, "保留两条"),
            (ConflictResolution.OVERRIDE_PRIORITY, "覆盖优先级"),
        ]

        self._resolution_buttons: dict = {}
        res_bar = ttk.Frame(action_frame)
        res_bar.pack(fill=tk.X, pady=2)

        for i, (val, label) in enumerate(resolutions):
            rb = ttk.Radiobutton(
                res_bar, text=label, value=val.value,
                variable=self._resolution_var,
                command=self._on_resolution_change
            )
            rb.pack(side=tk.LEFT, padx=(0, 8))
            self._resolution_buttons[val] = rb

        prio_row = ttk.Frame(action_frame)
        prio_row.pack(fill=tk.X, pady=2)
        ttk.Label(prio_row, text="覆盖优先级值:").pack(side=tk.LEFT)
        self._override_prio_var = tk.IntVar(value=1)
        self._prio_spin = ttk.Spinbox(prio_row, from_=1, to=999, textvariable=self._override_prio_var, width=8,
                                       command=self._on_priority_change)
        self._prio_spin.pack(side=tk.LEFT, padx=4)
        ttk.Label(prio_row, text="（越小越优先）", foreground="#6B7280").pack(side=tk.LEFT)

        btn_row = ttk.Frame(action_frame)
        btn_row.pack(fill=tk.X, pady=4)
        ttk.Button(btn_row, text="↩ 撤销此条决策", command=self._action_undo_decision).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="☑ 切换勾选状态", command=self._action_toggle_selected).pack(side=tk.LEFT, padx=2)

    def _build_timeline_panel(self, parent):
        timeline_frame = ttk.LabelFrame(parent, text=" 🕒 操作时间线 ", padding=6)
        timeline_frame.pack(fill=tk.BOTH, expand=True)

        self._timeline_text = tk.Text(timeline_frame, wrap=tk.WORD,
                                      font=("Microsoft YaHei UI", 9),
                                      relief=tk.SUNKEN, bd=1, height=10)
        self._timeline_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(timeline_frame, command=self._timeline_text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._timeline_text.configure(yscrollcommand=sb.set)
        self._timeline_text.configure(state=tk.DISABLED)

    def _build_bottom_bar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(6, 0))

        self._count_var = tk.StringVar(value="共 0 条")
        ttk.Label(bar, textvariable=self._count_var, foreground="#6B7280").pack(side=tk.LEFT)

        ttk.Button(bar, text="关闭", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        self._submit_btn = ttk.Button(bar, text="✅ 提交批次入队", command=self._action_submit)
        self._submit_btn.pack(side=tk.RIGHT, padx=4)

    def _load_initial_state(self):
        ui_state = self._manager.load_ui_state()

        if ui_state.current_batch_id:
            batch = self._manager.load_pending_batch(ui_state.current_batch_id)
            if batch:
                self._load_batch(batch)
                self._selected_group = PlaybackGroup(ui_state.selected_group) if ui_state.selected_group else None
                self._expanded_items = set(ui_state.expanded_items)
                self._refresh_tree()
                self._update_group_chips()
                return

        pending = self._manager.load_all_pending()
        if pending:
            self._load_batch(pending[-1])
        else:
            self._update_batch_info()
            self._refresh_tree()

    def _load_batch(self, batch: ImportBatch):
        self._current_batch = batch
        self._update_batch_info()
        self._refresh_tree()
        self._update_detail_panel()
        self._update_timeline()

    def _update_batch_info(self):
        if self._current_batch is None:
            self._batch_info_var.set("暂无批次")
            return

        b = self._current_batch
        src_name = Path(b.source_file).name
        counts = b.group_counts()
        status_label = {"pending": "待提交", "submitted": "已提交", "cancelled": "已取消"}.get(b.status.value, b.status.value)
        self._batch_info_var.set(
            f"批次: {b.batch_id[:16]}... | 来源: {src_name} | 状态: {status_label} | "
            f"总数: {len(b.items)} | "
            f"正常{counts.get('success', 0)} 兜底{counts.get('auto_fixable', 0)} "
            f"冲突{counts.get('duplicate_conflict', 0)} 失败{counts.get('unimportable', 0)}"
        )

    def _on_group_click(self, group: Optional[PlaybackGroup]):
        self._selected_group = group
        self._update_group_chips()
        self._refresh_tree()
        self._save_ui_state()

    def _update_group_chips(self):
        for group, (chip, lbl) in self._group_chips.items():
            if group == self._selected_group:
                chip.configure(bg="#1F2937")
                lbl.configure(bg="#1F2937")
            else:
                color = {
                    None: "#374151",
                    PlaybackGroup.SUCCESS: "#10B981",
                    PlaybackGroup.AUTO_FIXABLE: "#F59E0B",
                    PlaybackGroup.DUPLICATE_CONFLICT: "#EF4444",
                    PlaybackGroup.UNIMPORTABLE: "#6B7280",
                }.get(group, "#374151")
                chip.configure(bg=color)
                lbl.configure(bg=color)

    def _refresh_tree(self):
        self._tree.delete(*self._tree.get_children())

        if self._current_batch is None:
            self._count_var.set("共 0 条")
            return

        items = self._current_batch.items
        if self._selected_group is not None:
            items = [it for it in items if it.group == self._selected_group]

        tag_map = {
            PlaybackGroup.SUCCESS: "success",
            PlaybackGroup.AUTO_FIXABLE: "fixable",
            PlaybackGroup.DUPLICATE_CONFLICT: "conflict",
            PlaybackGroup.UNIMPORTABLE: "unimportable",
        }

        for item in items:
            sel_char = "☑" if item.selected else "☐"
            row = item.source_row or "-"
            group_label = GROUP_LABELS.get(item.group, item.group.value)
            identifier = item.source_identifier

            status_text = ""
            if item.group == PlaybackGroup.UNIMPORTABLE:
                status_text = item.error_message
            elif item.group == PlaybackGroup.AUTO_FIXABLE:
                status_text = "; ".join(item.fallback_reasons) if item.fallback_reasons else ""
            elif item.group == PlaybackGroup.DUPLICATE_CONFLICT:
                status_text = f"[{RESOLUTION_LABELS.get(item.resolution, item.resolution.value)}] {item.conflict_message}"
            else:
                status_text = "可直接导入"

            tags = [tag_map.get(item.group, "")]
            if item.selected:
                tags.append("selected")

            self._tree.insert(
                "", tk.END, iid=str(item.item_index),
                values=(sel_char, row, group_label, identifier, status_text),
                tags=tuple(tags),
            )

        self._count_var.set(f"共 {len(items)} 条")

    def _on_tree_click(self, event):
        region = self._tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self._tree.identify_column(event.x)
        if col == "#1":
            item_iid = self._tree.identify_row(event.y)
            if item_iid:
                self._toggle_selected(int(item_iid))

    def _on_tree_double_click(self, event):
        sel = self._tree.selection()
        if sel:
            idx = int(sel[0])
            self._toggle_selected(idx)

    def _toggle_selected(self, item_index: int):
        if self._current_batch is None:
            return
        if self._current_batch.status != BatchStatus.PENDING:
            return

        item = None
        for it in self._current_batch.items:
            if it.item_index == item_index:
                item = it
                break
        if item is None:
            return

        new_val = not item.selected
        self._manager.set_selected(self._current_batch, item_index, new_val, self._operator)
        self._refresh_tree()
        self._update_detail_panel()
        self._update_timeline()

    def _on_item_select(self, event):
        sel = self._tree.selection()
        if sel:
            self._selected_item_index = int(sel[0])
            self._update_detail_panel()
            self._update_timeline()
        else:
            self._selected_item_index = None

    def _get_selected_item(self) -> Optional[PlaybackItem]:
        if self._selected_item_index is None or self._current_batch is None:
            return None
        for it in self._current_batch.items:
            if it.item_index == self._selected_item_index:
                return it
        return None

    def _update_detail_panel(self):
        try:
            self._detail_text.configure(state=tk.NORMAL)
            self._detail_text.delete("1.0", tk.END)

            item = self._get_selected_item()
            if item is None:
                self._detail_text.insert(tk.END, "请选择一个条目查看详情。\n\n")
                self._detail_text.insert(tk.END, "点击第一列可切换是否导入。\n")
                return

            raw = item.raw_fields
            task = item.parsed_task

            lines = []
            lines.append("=" * 50)
            lines.append(f"  条目 #{item.item_index}  |  源行: {item.source_row or '-'}")
            lines.append(f"  分组: {GROUP_LABELS.get(item.group, item.group.value)}")
            lines.append("=" * 50)
            lines.append("")

            lines.append("【原始字段】")
            lines.append(f"  文件名: '{raw.filename}'")
            lines.append(f"  份数: '{raw.copies}'")
            lines.append(f"  柜台: '{raw.counter}'")
            lines.append(f"  重试次数: '{raw.max_retries}'")
            lines.append(f"  优先级: '{raw.priority}'")
            if raw.extra:
                lines.append(f"  额外字段: {raw.extra}")
            lines.append("")

            if item.error_message:
                lines.append("【错误原因】")
                lines.append(f"  {item.error_message}")
                lines.append("")

            if item.fallback_reasons:
                lines.append("【默认值兜底原因】")
                for r in item.fallback_reasons:
                    lines.append(f"  ⚠ {r}")
                lines.append("")

            if item.conflict_message:
                lines.append("【冲突信息】")
                lines.append(f"  冲突类型: {item.conflict_type.value if item.conflict_type else '未知'}")
                lines.append(f"  描述: {item.conflict_message}")
                if item.conflict_candidates:
                    lines.append("  冲突候选:")
                    for c in item.conflict_candidates:
                        lines.append(f"    - {c.filename} ({c.counter}, 优先级{c.priority}, {c.status})")
                lines.append(f"  当前决策: {RESOLUTION_LABELS.get(item.resolution, item.resolution.value)}")
                if item.override_priority_value:
                    lines.append(f"  覆盖优先级值: {item.override_priority_value}")
                lines.append("")

            if task:
                lines.append("【解析结果】")
                lines.append(f"  文件名: {task.filename}")
                lines.append(f"  份数: {task.copies}")
                lines.append(f"  柜台: {task.counter.value}")
                lines.append(f"  最大重试: {task.max_retries}")
                lines.append(f"  自定义优先级: {task.priority_override if task.priority_override else '(使用柜台默认)'}")
                lines.append(f"  是否导入: {'是' if item.selected else '否'}")
                lines.append("")

            if item.submit_result:
                lines.append(f"【提交结果】{item.submit_result}")
                if item.submitted_task_id:
                    lines.append(f"  任务ID: {item.submitted_task_id}")

            self._detail_text.insert(tk.END, "\n".join(lines))

            self._update_resolution_controls(item)
        finally:
            self._detail_text.configure(state=tk.DISABLED)

    def _update_resolution_controls(self, item: PlaybackItem):
        is_conflict = item.group == PlaybackGroup.DUPLICATE_CONFLICT
        is_pending = self._current_batch and self._current_batch.status == BatchStatus.PENDING
        enabled = is_conflict and is_pending

        state = "normal" if enabled else "disabled"
        for val, rb in self._resolution_buttons.items():
            rb.configure(state=state)
        self._prio_spin.configure(state=state)

        if is_conflict:
            self._resolution_var.set(item.resolution.value)
            if item.override_priority_value is not None:
                self._override_prio_var.set(item.override_priority_value)
        else:
            self._resolution_var.set("")

    def _on_resolution_change(self):
        if self._current_batch is None or self._selected_item_index is None:
            return
        if self._current_batch.status != BatchStatus.PENDING:
            return

        try:
            res = ConflictResolution(self._resolution_var.get())
        except ValueError:
            return

        override_prio = None
        if res == ConflictResolution.OVERRIDE_PRIORITY:
            override_prio = self._override_prio_var.get()

        self._manager.set_resolution(
            self._current_batch, self._selected_item_index,
            res, override_prio, self._operator
        )
        self._refresh_tree()
        self._update_detail_panel()
        self._update_timeline()

    def _on_priority_change(self):
        if self._current_batch is None or self._selected_item_index is None:
            return
        if self._current_batch.status != BatchStatus.PENDING:
            return

        item = self._get_selected_item()
        if item is None or item.group != PlaybackGroup.DUPLICATE_CONFLICT:
            return

        if item.resolution == ConflictResolution.OVERRIDE_PRIORITY:
            self._manager.set_resolution(
                self._current_batch, self._selected_item_index,
                item.resolution, self._override_prio_var.get(), self._operator
            )
            self._refresh_tree()
            self._update_detail_panel()
            self._update_timeline()

    def _action_undo_decision(self):
        if self._current_batch is None or self._selected_item_index is None:
            return
        if self._current_batch.status != BatchStatus.PENDING:
            messagebox.showinfo("提示", "只有待提交批次才能撤销决策")
            return

        item = self._get_selected_item()
        if item is None or item.group != PlaybackGroup.DUPLICATE_CONFLICT:
            messagebox.showinfo("提示", "只有冲突待决条目才能撤销决策")
            return

        self._manager.undo_last_decision(self._current_batch, self._selected_item_index, self._operator)
        self._refresh_tree()
        self._update_detail_panel()
        self._update_timeline()

    def _action_toggle_selected(self):
        if self._current_batch is None or self._selected_item_index is None:
            return
        if self._current_batch.status != BatchStatus.PENDING:
            return
        self._toggle_selected(self._selected_item_index)

    def _action_select_all(self):
        if self._current_batch is None:
            return
        if self._current_batch.status != BatchStatus.PENDING:
            return
        if self._selected_group is None:
            for g in PlaybackGroup:
                if g != PlaybackGroup.UNIMPORTABLE:
                    self._manager.set_group_selected(self._current_batch, g, True, self._operator)
        else:
            self._manager.set_group_selected(self._current_batch, self._selected_group, True, self._operator)
        self._refresh_tree()
        self._update_timeline()

    def _action_select_none(self):
        if self._current_batch is None:
            return
        if self._current_batch.status != BatchStatus.PENDING:
            return
        if self._selected_group is None:
            for g in PlaybackGroup:
                self._manager.set_group_selected(self._current_batch, g, False, self._operator)
        else:
            self._manager.set_group_selected(self._current_batch, self._selected_group, False, self._operator)
        self._refresh_tree()
        self._update_timeline()

    def _update_timeline(self):
        try:
            self._timeline_text.configure(state=tk.NORMAL)
            self._timeline_text.delete("1.0", tk.END)

            entries = []

            if self._current_batch:
                for t in self._current_batch.timeline:
                    entries.append((t.timestamp, f"[批次] {t.action} - {t.detail}" if t.detail else f"[批次] {t.action}", t.operator))

            item = self._get_selected_item()
            if item:
                for t in item.timeline:
                    entries.append((t.timestamp, t.action + (" - " + t.detail if t.detail else ""), t.operator))

            entries.sort(key=lambda x: x[0])

            for ts, action, operator in entries:
                t_str = time.strftime("%H:%M:%S", time.localtime(ts))
                op = f"({operator})" if operator else ""
                self._timeline_text.insert(tk.END, f"[{t_str}] {action} {op}\n")

        finally:
            self._timeline_text.configure(state=tk.DISABLED)

    def _action_new_batch(self):
        path = filedialog.askopenfilename(
            title="选择导入文件",
            filetypes=[("CSV/JSON 文件", "*.csv *.json"), ("CSV 文件", "*.csv"),
                       ("JSON 文件", "*.json"), ("所有文件", "*.*")],
            initialdir=str(Path(__file__).resolve().parent.parent / "examples"),
            parent=self,
        )
        if not path:
            return

        try:
            batch = self._manager.create_batch(
                path,
                existing_tasks=self._queue.tasks if self._queue else None,
                default_max_retries=self._queue.config.max_retries_default if self._queue else 3,
                operator=self._operator,
            )
            self._load_batch(batch)
            self._selected_group = None
            self._expanded_items.clear()
            self._update_group_chips()
            self._save_ui_state()
        except (FileNotFoundError, ValueError) as e:
            messagebox.showerror("导入失败", str(e), parent=self)

    def _action_export_audit(self):
        if self._current_batch is None:
            messagebox.showinfo("提示", "请先加载或创建一个批次", parent=self)
            return

        out_dir = filedialog.askdirectory(title="选择导出目录", parent=self)
        if not out_dir:
            return

        try:
            path = self._manager.export_audit_package(
                self._current_batch, out_dir, operator=self._operator
            )
            messagebox.showinfo("导出成功", f"审计包已导出到:\n{path}", parent=self)
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self)

    def _action_view_last_submitted(self):
        last = self._manager.load_last_submitted()
        if last is None:
            messagebox.showinfo("暂无记录", "还没有已提交的批次记录", parent=self)
            return
        self._load_batch(last)
        self._save_ui_state()

    def _action_submit(self):
        if self._current_batch is None:
            messagebox.showinfo("提示", "请先创建一个批次", parent=self)
            return
        if self._current_batch.status != BatchStatus.PENDING:
            messagebox.showinfo("提示", "只有待提交批次才能提交", parent=self)
            return
        if self._queue is None:
            messagebox.showinfo("提示", "队列管理器未就绪", parent=self)
            return

        selected_count = sum(1 for it in self._current_batch.items if it.selected)
        if not messagebox.askyesno(
            "确认提交",
            f"确认提交当前批次？\n\n"
            f"已勾选: {selected_count}/{len(self._current_batch.items)} 条\n\n"
            f"提交后将加入正式打印队列，此操作可通过审计包追溯。",
            parent=self
        ):
            return

        try:
            summary = self._manager.submit_batch(
                self._current_batch, self._queue, operator=self._operator
            )

            added = summary["added_count"]
            skipped = summary["skipped_count"]
            failed = summary["failed_count"]

            messagebox.showinfo(
                "提交完成",
                f"批次已提交到打印队列！\n\n"
                f"✅ 成功入队: {added} 条\n"
                f"⏭  跳过: {skipped} 条\n"
                f"❌ 失败: {failed} 条",
                parent=self
            )

            self._update_batch_info()
            self._refresh_tree()
            self._save_ui_state()

        except Exception as e:
            messagebox.showerror("提交失败", str(e), parent=self)

    def _save_ui_state(self):
        state = PlaybackUIState(
            current_batch_id=self._current_batch.batch_id if self._current_batch else None,
            selected_group=self._selected_group.value if self._selected_group else None,
            expanded_items=list(self._expanded_items),
            filter_text="",
        )
        self._manager.save_ui_state(state)

    def destroy(self):
        self._save_ui_state()
        super().destroy()
