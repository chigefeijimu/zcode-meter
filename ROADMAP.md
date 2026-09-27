# zcode-meter 优化与扩展清单

> 来源：2026-09-27 对标调研（CodexBar 22k⭐ / ccusage ~15k⭐ / Claude Code Usage Monitor / GLM Monitor）
> 每项标注：来源项目、投入档位（★轻=纯展示/计算层 / ★★中=结构改动 / ★★★重=跨平台级）、建议批次
> 完成后打勾并在括号里记版本号

---

## 第一波：纯展示/计算层（数据现成，1-2 轮工作流迭代可全部完成）

- [ ] **托盘快捷面板**（CodexBar 下拉面板）★★轻
  托盘图标左键点击弹出今日概览迷你面板：今日 ¥ / 套餐剩余% / 燃速 三行 + 「打开历史图表」入口
  现状痛点：历史图表藏在右键二级菜单，高频信息需一键直达

- [ ] **导出与分享**（CodexBar Settings→Usage & Spend 的 export）★轻
  ① `zm_usage_export.csv`：按天/按模型明细导出；② Ctrl+C 或菜单项复制今日摘要文本（今日 token/¥/剩余%，发群/贴 issue 用）
  数据全在本地 db，一个查询 + csv 标准库

- [ ] **消耗趋势外推**（Usage Monitor / claudetop 的月度预测）★轻
  历史图表按天页签加"趋势线"：按近 7 天日均外推"本月预计 ¥X"；卡片燃速旁加"今晚 24 点预计 ¥Y"
  已有数据：按天用量 + 金额。比"还可撑 X 小时"更直观的预算视角

## 第二波：数据层增强（已拍板规格，待启动）

- [ ] **重置通知区分窗口类型**（已确认可行，规格已给）★轻
  `parse_quota_payload` 保留 `limits[].type/number`（TOKENS_LIMIT&number==5 → 5h 窗；其他 type → weekly/月度），
  `nextResetTime` 间隔模式做二重校验；重置成功时托盘气泡区分文案（"5h 额度已刷新"/"本周额度已重置"），
  去重状态进 `zm_alerts.json`（5h 彩带当天最多一次）；quota 不可用 → 不通知
  彩带动效（自绘小窗）暂缓，先用系统气泡验证价值

## 第三波：结构与生态（决定项目天花板）

- [ ] **Provider 配置化**（CodexBar "Open to new providers" 模式）★★中
  `UsageSource` 接口已就绪，下一步：新增一个源 = 放一个 Python 文件进 `sources/` 目录自动发现，
  不必改核心代码；README 写"如何贡献一个源"指南。让社区帮我们长出 Codex/Cursor/Qwen 源

- [ ] **查询窗口限制**（CodexBar 读 Codex WAL "上限 25000 条"的防御）★轻
  `_poll_stats`/图表查询目前无 LIMIT，一年后数据量增长会拖慢轮询与历史图表；
  给长时窗查询加时间窗/条数上限（今日轮询不受影响，只限历史聚合）

- [ ] **CLI 伴侣**（CodexBar `codexbar cost` / ccusage 整体形态）★★中
  `python -m zcode_meter cost --days 30` 输出文本报表；不开 GUI 也能查账，
  cron/CI 场景的钩子。复用数据层全部查询函数

- [ ] **刷新档位显式化**（CodexBar Adaptive/1m/5m/30m 设置）★轻
  事件驱动机制保留，把"活跃期 3min/静默暂停"做成设置界面可调项（power user 的控制感）

## 第四波：UX 打磨（性价比小项，可穿插做）

- [ ] **数据新鲜度视觉化**（CodexBar "数据过期图标变暗"）★轻
  贴边胶囊条：quota 数据静默期冻结时整体降透明度 20%，一眼区分实时/陈旧数据
  （已有"· N分钟前"文字标注，这是它的视觉版）

- [ ] **重置彩带动效**（CodexBar weekly-reset confetti）★★中
  自绘无边框小窗播放 2 秒彩带；前提：第二波的重置通知验证有用户价值后再做

- [ ] **README 隐私声明**（CodexBar 范式："Reuses existing sessions, no passwords stored / 只读已知位置不扫描全盘"）★轻
  我们做过完整的安全验证（单端点/key 三层防护/git 零残留）但没写进 README —— 对开源信任度影响大

## 远期方向（有需求再启动，不排期）

- [ ] **Linux 移植**（★★★）：PySide6 本身跨平台，Win32 调用集中在贴边/DWM 两处需抽象；
  CodexBar 生态证明了 Linux 桌面 widget 的需求存在（Waybar/KDE 插件）
- [ ] **国际化 i18n**（★★）：CodexBar 23 种语言；现在中文硬编码，架构上避免新增硬编码拼接即可低成本留后路

---

## 执行建议

| 批次 | 内容 | 方式 |
|---|---|---|
| 下轮工作流 | 第一波 3 项 + 第二波重置通知（天然 4 张独立 ticket，正好检验 v2 工作流的并行路径） | `zcode-meter-iterate` 一轮 |
| 再下轮 | 第三波的 provider 配置化 + 查询窗口限制 + README 隐私声明 | `zcode-meter-iterate` 一轮 |
| 穿插 | 第四波小项随其他迭代顺手带，或攒一批 | 单独小轮 |
