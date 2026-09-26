# Changelog

## v0.2.0 (2026-09-26) — PySide6 版

- 架构迁移：tkinter → PySide6（`startSystemMove` 系统原生拖动，弃用全部 ctypes 拖动路径）
- 贴边胶囊条：宽度/高度按内容动态自适应（`_refit_dock` 周期校验，分辨率/DPI 变化自愈）
- 禁用 Aero Snap，防系统贴靠与变条逻辑打架
- 双速率：实时（流式估算+完成精确）+ 会话平均（按提供商+模型分组）
- 新增：首字等待(TTFT)、整体耗时、今日总用量、会话标题跟随
- 口径修正：
  - `input_tokens` 已含 `cache_read`（缓存率 = cache/in，不再双计）
  - 今日用量含全部来源（main_turn + subagent），与 ZCode 自身统计一致
  - 主力模型按 token 用量取（弃用字典序 MAX）
  - 活跃会话判定改 part 表最新写入行（rollout mtime 滞后一整轮）
- 会话切换：排除 subagent 劫持 + RLock 消除统计线程竞态
- 崩溃追踪：faulthandler → zm_crash.log；ZM_DEBUG=1 → zm_debug.log

## v0.1.0 (2026-09-25) — tkinter 版（留档）

- 首版：数据层（日志 tail + SQLite 轮询 + part 流式估算）、贴边条、DPI 适配
- 已知不可修的架构问题（触发迁移）：拖动迟滞、静置后吞点击、Win32 互操作崩溃
