# Changelog

## v0.3.0 (2026-09-26) — 位置记忆 / 历史图表 / 手动切换 / 打包

- 位置记忆：退出与形态变化（贴边/拖离）即时保存 `x/y/贴边方向` 到 `zm_state.json`；
  启动恢复只还原位置与形态，宽高仍走常量 + `_refit_dock` 动态自愈（固定尺寸脱节的教训）；
  恢复时按"与保存点交集面积最大的屏"夹回可视区，完全离屏（改分辨率/拔显示器）回主屏默认位；
  `aboutToQuit` 兜菜单退出路径。强杀/断电丢最后纯拖动位置属可接受残余缺口
- 历史用量图表：独立窗口（普通 `Qt.Window`，带任务栏图标），按天（近 30 天，全来源
  in+out，同今日口径）/按会话（近 20 个，`main_turn`，与卡片逐字对齐）双页签 + 刷新；
  纯 QPainter 竖柱/水平条（不引 matplotlib，保 exe 体积）。两图口径不同、合计对不上账
  属预期（README 口径表已写明）
- 多会话手动切换：右键「会话」子菜单列出最近 8 个会话（part 最新写入倒序、排除
  subagent，与自动跟随同源），固定后卡片标题带 📌，「自动跟随(最近活跃)」恢复。
  重构 `_refresh_session` → `_switch_session`（自动/手动共用，序列原样保留：
  RLock→session_id→Snapshot 重建→`_last_len=None`→`_prev_max_rowid` 重置→title→
  poll→init_tps→push），并把 manual 检查与 `_latest_session` 查询整体包进
  `snap_lock`（RLock 可重入），消除「UI 固定与引擎 check-then-act」竞态
- 打包：PyInstaller 单文件命令与 frozen 注意事项写入 README「打包」章节；
  新增模块级 `app_dir()`（frozen 取 exe 目录），`zm_crash.log`/`zm_debug.log`/
  `zm_state.json` 在打包版落 exe 同目录；`faulthandler.enable` 包 try/except
  （只读目录不再启动即崩）；删除数据层遗留的 tkinter 死导入
- 测试：`ZM_NO_STATE=1` 隔离开关（ui/stress 子进程注入 + `--verify` 自检双守卫），
  回归不再改写用户真实位置记忆；新增 4 个口径单测（手动固定/恢复、最近会话排除
  subagent、按天=今日口径、按会话=main_turn 逐值对账）
- 已知边界：手动固定期间「生成中」状态/计时来自全局日志 tail 不区分会话（自动模式
  同源）；按天天界用 SQLite `localtime`，跨时区迁移时可能与 ZCode 自身统计差一天

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
