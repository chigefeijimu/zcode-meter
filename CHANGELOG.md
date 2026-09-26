# Changelog

## v0.4.0 (2026-09-26) — 成本估算 / 预算告警 / 燃速 / 计费块 / 多源 / 托盘

- 成本估算：内置 bigmodel 按量刊例价三档表（元/M tokens，2026-09 自官方定价文档
  转录；`GLM-5.3-Flash` 取标准牌价、分档计费模型取最高档保守估算），`zm_prices.json`
  按模型级 merge 覆盖（坏文件回退默认）。口径钉死两条：已知模型缺 `in_cache` 档
  → cache_read 按 in 全价计（宁可高估、不置 partial）；未知/非 bigmodel 模型 →
  ¥0 + partial（卡片/横条金额带 ≈，为下限）。卡片今日用量、横条、按天图表（柱身
  第二行）同步显示 ¥。今日金额/燃速口径均为 **ZCode-DB-only**（Claude 源不进金额）
- 预算告警（双轨）：quota 轨 —— GET `open.bigmodel.cn/api/monitor/usage/quota/limit`
  （只读，全程唯一外部请求端点，Authorization 放原始 key 不带 Bearer，300s 一轮，
  urllib 标准库实现），`percentage` 为已用%、取 TOKENS_LIMIT 中 `number==5` 的 5h 窗，
  低于阈值托盘气泡提醒；按量轨 —— (日预算−今日花费 ZCode 口径)/日预算。阈值默认
  剩余 20%/10% 各提醒一次，同级别同日只提醒一次、跨日自动重置（独立 `zm_alerts.json`
  持久化，守卫环境不写）；接口结构变化时该轨优雅降级为不显示，首跑失败把响应片段
  落 `zm_debug.log`（社区逆向接口，无 key 无法预联调，需真实 key 人工验证一次）
- 燃速 + 耗尽预估：trailing 60 分钟窗口计算 token/¥ 每小时燃速，`(日预算−今日
  花费)/燃速¥/h` 显示"预算还可撑 X 小时"；燃速 0 或未配日预算返回 None；活跃不足
  60 分钟时窗口未满会低估燃速（README 口径表注记）
- 5h 计费块：历史图表第三页签，按 5 小时窗聚合 query_source 全部 completed in+out
  （同今日 token 口径），当前活动块 C_WARN 高亮；quota 可用时以
  `nextResetTime−k×5h` 对齐平台真实计费窗，否则回退锚点=最早 completed started_at
  （块界为示意，页签内声明）
- 多 CLI 聚合：数据层抽象 `UsageSource` 接口（name/today_usage/daily_usage），
  `ZCodeSource` 包装现有口径为默认实现；新增 `ClaudeSource`（`~/.claude/projects`
  只读 jsonl 解析：in=input+cache_read+cache_creation 补齐 Anthropic 口径、含
  sidechain、message.id 去重但先跳过零用量/error 行防清零、UTC→local 定天界、
  (path,mtime,size) 文件缓存 + 15s 扫描 TTL）；卡片今日用量下方聚合显示各源。
  **今日用量本体 `today_tokens` 及其 SQL 保持 ZCode-DB-only 一字未动**（历史回归
  重灾区），多源只落新字段 `today_by_source`
- 托盘模式：`QSystemTrayIcon`（无托盘环境整体跳过不崩）；主窗右键「收起到托盘」，
  托盘菜单 显示/隐藏+退出，单击恢复；预算告警走托盘气泡；跨线程只传普通数据，
  Qt 调用全留 UI 线程
- 启动位置钉死（评审必改）：`QuotaMonitor`/`BudgetAlerts` 类定义在 data_engine
  （不 import UI 模块），但只由 `MeterWindow` 实例化与启动；data_engine 自备
  `_no_persist()` 守卫（`ZM_NO_STATE=='1'` 或 `--verify`，同 `_state_guard` 语义）
  拦住 zm_alerts.json 写入与 monitor 启动；`run_all.py` data 组补注入 `ZM_NO_STATE=1`
  作第二道闸（仅加环境变量，断言未动）—— 带 key 的 zm_config.json 存在时回归
  也不发真实网络请求
- UI 细节：新增 燃速/套餐剩余 两行为卡片专属 label，横条与竖条两种形态都按纪律
  显式置 None（只在一种形态置 None 会让另一形态的压力循环摸已销毁 QLabel）；
  `CARD_H` 295→336（自然高度实测 314 + 多源行余量），`_restore_state`/
  `_detach_to_pointer`/`_unset_dock` 三处几何引用常量自动跟进（ui-verify 只打印
  尺寸不校验，需人工目视确认不截断）
- 安全与配置：`zm_config.json`（quota_api_key/daily_budget_cny/alert_pct）与
  `zm_prices.json`、`zm_alerts.json` 加入 `.gitignore`（key 提交风险专查项）；
  日志/调试路径不打印 key 明文；README 新增「配置」章节与口径表五行（金额三档
  规则/燃速窗口注记/计费块对齐与示意声明/多源口径差异/quota percentage 为已用%）
- 测试：新增 8 组单测（金额三路径与分组 SQL 手算对账、燃速窗口对账与 est 纯函数、
  计费块等分/总量/当前块/quota 锚、ClaudeSource 合成 jsonl 全规则、quota 解析、
  告警去重与跨日重置与持久化、ZCode 源=今日口径），全部无网络、不依赖真实
  ~/.claude；现有 8 个测试与断言一字未改

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
