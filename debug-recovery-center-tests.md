# 恢复中心测试补全 - 调试会话记录

**Session ID**: recovery-center-tests
**Status**: [OPEN]
**Created**: 2026-06-19

## 问题描述

需要为"恢复中心"跨重启链路补充一组兜底测试，重点验证：
1. 同一导出记录再次查看时，自动快照是复用旧记录还是重复新建
2. 覆盖首次查看、关闭重开、重启应用、导入快照、撤销导入等场景
3. 分别断言快照数量、记录关联、最近查看状态和界面可见反馈
4. 复现"分析说会复用，实际却生成第二个自动快照"的回归用例
5. 边界检查：旧快照缺字段、快照顺序变化、恢复日志乱码

## 核心代码分析

### 关键问题定位

在 [review_workbench.py#L439](file:///d:/workSpace/AI__SPACE/lfc-00027/core/review_workbench.py#L439) 中：
```python
self._active_auto_snapshots: Dict[str, str] = {}
```

这个字典仅存在于内存中，用于跟踪当前会话中已创建的自动快照。

在 [auto_snapshot()](file:///d:/workSpace/AI__SPACE/lfc-00027/core/review_workbench.py#L502-L581) 方法中：
- 第509-536行：检查 `_active_auto_snapshots` 中是否已有该记录的快照
- 如果有，复用并更新；如果没有，创建新快照
- **问题**：重启后内存字典清空，不会从持久化文件中恢复这个映射

### 真实入口链

1. 导出记录中心 [export_record_dialog.py#L448-L496](file:///d:/workSpace/AI__SPACE/lfc-00027/ui/export_record_dialog.py#L448-L496):
   - `_auto_create_snapshot()` - 选中记录时自动创建快照
   - `_on_close()` - 关闭时保存状态

2. 恢复中心 [review_workbench_dialog.py](file:///d:/workSpace/AI__SPACE/lfc-00027/ui/review_workbench_dialog.py):
   - `_load_snapshot_detail()` - 加载快照详情
   - `_save_current_state()` - 保存当前状态
   - `_on_close()` - 关闭时保存

## 可证伪假设

**H1**: 同一会话内多次查看同一记录 → 正确复用自动快照（现有测试已覆盖）
**H2**: 重启后再次查看同一记录 → 错误地创建第二个自动快照（内存字典丢失导致）
**H3**: 通过 find_snapshot_by_record() 能找到已有快照，但 auto_snapshot() 不会用它
**H4**: 重启后 _active_auto_snapshots 为空，但磁盘上已有 is_auto=True 的快照
**H5**: 边界场景下（缺字段、乱码等），问题出在序列化层而非GUI层

## 测试计划

### 核心链路测试
1. test_17_recovery_full_chain - 完整链路：首次查看→关闭重开→重启→再进入
2. test_18_import_snapshot_with_recovery - 导入快照及撤销
3. test_19_gui_feedback_verification - 界面可见反馈验证

### 回归用例
4. test_20_regression_auto_snapshot_duplicate_after_restart - 复现重启后重复创建快照

### 边界检查
5. test_21_boundary_missing_fields - 旧快照缺字段
6. test_22_boundary_snapshot_order - 快照顺序变化
7. test_23_boundary_corrupted_log - 恢复日志乱码/损坏

## 测试断言维度

每个测试需要断言：
- 快照数量（是否意外增长）
- 记录关联（snapshot.record_id 正确性）
- is_auto 标记
- 最近查看状态（get_last_snapshot()）
- 界面可见反馈（状态标签、健康检查）
- 恢复日志内容

## 变更记录

- [x] 2026-06-19: 初始化调试会话，完成代码分析
- [ ] 实现测试夹具
- [ ] 实现核心链路测试
- [ ] 实现回归用例
- [ ] 实现边界检查
- [ ] 运行测试并分析结果
