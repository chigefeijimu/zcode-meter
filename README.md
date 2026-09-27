# zcode-meter

ZCode token 用量与输出速度的桌面悬浮小工具。贴边变胶囊条，实时输出速度、会话统计、成本估算与今日用量一目了然。

A Windows desktop widget that floats beside your ZCode sessions, showing token usage, cache hit rate, output speed and cost estimates in real time.

## 特性

- 🚀 **实时输出速度**：生成中按流式文本增速估算（自动校准换算率），请求完成瞬间切换为精确值
- 📊 **双速率统计**：实时速度 + 会话平均速度（按 提供商×模型 分组）
- 💰 **成本估算**：内置 bigmodel 按量刊例价（三档：输入/缓存命中/输出），卡片与横条的今日用量、按天图表同步显示 ¥ 金额（`zm_prices.json` 可覆盖）
- 🔔 **预算告警（双轨）**：Coding Plan 套餐轨（quota 接口轮询）+ 按量日预算轨（本地 token×价格表），跌破阈值托盘气泡提醒（默认 20%/10% 各一次，同级别同日不重复）
- 🔥 **燃速与耗尽预估**：按最近 60 分钟窗口计算每小时燃速，配合日预算显示"还可撑 X 小时"
- 🕔 **5 小时计费块**：历史图表第三页签按 5h 窗口聚合 token，高亮当前活动块（配置 quota 后按平台真实计费窗对齐块界）
- 🧩 **多 CLI 聚合**：内置"用量源"抽象（ZCode 为默认源），另支持 Claude Code（`~/.claude/projects` 只读解析），卡片今日用量下方聚合显示各源今日用量
- 💾 **缓存命中率**：与 ZCode 计费口径一致的精确统计
- 🕐 **延迟指标**：首字等待（TTFT）与单次输出整体耗时
- 📅 **今日总用量**：跨会话、含子代理，与 ZCode 自身统计对账一致（口径保持 ZCode-DB-only，多源只做附加展示）
- 🧲 **贴边胶囊条**：拖到屏幕边缘变一条恰好包住文字的胶囊，动态尺寸随分辨率/DPI 自愈
- 🖱️ **原生拖动**：Qt `startSystemMove` 系统级接管，120Hz 高刷屏丝滑跟手
- 🔄 **会话跟随**：切换 ZCode 会话自动切换统计对象（含会话标题），也可右键手动固定（📌）
- 📍 **位置记忆**：退出保存位置与形态（卡片/贴边方向），重启恢复；分辨率变化后自动夹回可视区
- 📈 **历史用量图表**：按天（近 30 天，含 ¥）/按会话（近 20 个）/计费块（5h）三页签
- 🖥️ **托盘模式**：主窗可收起到托盘，托盘菜单 显示/隐藏、退出，单击图标恢复；预算提醒走托盘气泡

## 环境要求

- Windows 10/11（x64）
- Python 3.10+
- PySide6：`pip install PySide6`

## 快速开始

```bash
git clone git@github.com:chigefeijimu/zcode-meter.git
cd zcode-meter
pythonw zcode_meter_qt.py        # 日常使用(无控制台)
python zcode_meter_qt.py         # 调试(带控制台)
python zcode_meter_qt.py --verify    # 自检:渲染/绑定/贴边判定
```

开机自启（可选）：

```powershell
Set-ItemProperty "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" -Name zcode-meter `
  -Value '"C:\Windows\py.exe" -w "<你的路径>\zcode-meter\zcode_meter_qt.py"'
```

## 交互

| 操作 | 效果 |
|---|---|
| 任意位置按住拖动 | 移动窗口（系统原生拖动，跨显示器自由） |
| 拖到屏幕边缘松手 | 变胶囊条（宽高恰好包住内容，位置跟随松手点） |
| 按住胶囊条拖离边缘 | 恢复卡片 |
| 右键 → 贴边/恢复卡片 | 直达四边 / 恢复卡片（形态变化即时保存，重启恢复） |
| 右键 → 会话 | 列出最近 8 个会话（按 part 最新写入倒序）手动固定统计对象（卡片标题前缀 📌）；「自动跟随(最近活跃)」恢复自动 |
| 右键 → 历史用量图表 | 打开独立窗口：按天(近30天，token+¥)/按会话(近20个)/计费块(5h) 三页签，可刷新；三图均为竖柱，会话/计费块页长标签 45° 斜排并按空间省略，**悬停柱子显示完整标签与数值** |
| 右键 → 设置 | 打开设置窗：quota API Key（密码框，界面任何位置不回显明文）/日预算/告警阈值；保存即写入 `zm_config.json` 并热生效（套餐轨立即重启轮询、引擎与告警即时读新值），失败时窗内红字报错且不生效 |
| 右键 → 收起到托盘 | 主窗隐藏到系统托盘（无托盘环境不显示此项）；托盘菜单 显示/隐藏、退出；单击托盘图标恢复 |
| 右键 → 退出 | 退出（退出时保存位置与形态，下次启动恢复） |

绿色呼吸点 = 生成中；速度带 `~` = 流式估算值；标题带 📌 = 统计对象已被手动固定；金额带 `≈` = 含价格表未覆盖的模型，金额为下限估算。

## 显示指标与口径

| 指标 | 口径 |
|---|---|
| 实时速度 | 当前/最近一次主对话请求 |
| 会话平均速度 | 当前会话、按 提供商+模型 分组：Σ输出 ÷ Σ(duration−TTFT) |
| 入/出/缓存率 | 当前会话、main_turn；**input_tokens 已含 cache_read**，命中率 = cache/in |
| 首字等待/整体耗时 | 最近一次请求的 TTFT / duration |
| 今日用量 | 今天 0 点起**全部会话全部来源**（含 subagent），与 ZCode 自身口径一致；口径钉死 **ZCode-DB-only**，多源数字只做附加展示不并入 |
| 今日金额（¥） | ZCode-DB-only 按刊例价：`(in−cache_read)×in价 + cache_read×缓存价 + out×out价`，三档单价（元/M tokens）来自内置价格表。**已知模型缺 in_cache 档时 cache_read 按 in 全价计（宁可高估）；未知/非 bigmodel 模型计 ¥0 并置 partial（显示 ≈，金额为下限）**。订阅套餐内的用量实际不按量扣费，此金额是"按刊例价折算的等值成本"，用于预算感知 |
| 燃速 / 还可撑 X 小时 | 燃速 = 最近 **60 分钟**滚动窗口内的 token（及金额）之和；`还可撑 = (日预算−今日花费) ÷ 燃速¥/h`。**活跃不足 60 分钟时窗口未满、燃速被低估、预估偏大，属窗口口径而非 bug**；燃速为 0 或未配日预算时不显示 |
| 多源今日（卡片第二行） | 各源今日用量聚合：ZCode 同今日口径；Claude = `input_tokens + cache_read + cache_creation`（**Anthropic 口径 input 不含 cache，须补齐后才与 ZCode 可比**），含 sidechain 行（对齐 ZCode 含 subagent），按 message.id 去重但跳过零用量/error 行，时间戳 UTC 转本地定天界。**两源口径不同，合计对不上属预期，勿当 bug** |
| 计费块（5h 页签） | 按 5 小时窗聚合 `completed` 的 **query_source 全部** in+out（同今日 token 口径）。块界对齐：已配置 quota 时按 `nextResetTime−k×5h`（平台真实计费窗，黄色高亮=当前活动块）；未配置时回退锚点=最早 completed 请求时刻，**此时块界为示意、非平台真实计费窗** |
| 套餐剩余（quota 轨） | GET `open.bigmodel.cn/api/monitor/usage/quota/limit`（只读，**事件驱动+节流**：ZCode 有新请求完成即视为「有消耗」触发刷新，活跃期（过去 1h 内有请求）常态最长 3 分钟一次，静默期（>1h 无任何请求）暂停查询，最小间隔 60s 硬闸；启动/设置界面改 key 后立即查一次）：`percentage` 为**已用**百分比，剩余 = 100−percentage，取 TOKENS_LIMIT 中 `number==5` 的条目即 5h 计费窗。只有剩余% 需要网络刷新；重置倒计时纯本地每分钟递减、不发任何请求。旁注查询时间（如「· 3分钟前」），静默期数据冻结属预期、以新鲜度标注为准。接口为社区逆向的非公开文档接口，结构变化时该行不显示（首跑失败会把响应片段落 `zm_debug.log` 便于修） |
| 预算告警（双轨） | quota 轨 = 套餐 5h 窗剩余%；按量轨 = (日预算−今日花费 ZCode 口径) ÷ 日预算。默认阈值剩余 20% / 10% 各提醒一次，**同级别同日只提醒一次**、跨日自动重置（状态存 `zm_alerts.json`），经托盘气泡派发 |
| 会话跟随 | 按 `part` 表最新写入行判定（排除 `sess_subagent_*`）；自动（最近活跃）或手动固定 |
| 按天图表 | `completed` **全来源** in+out（同今日口径），本地午夜天界；柱身第二行为当日 ¥（按刊例价，同今日金额口径，含未知模型时为下限） |
| 按会话图表 | 该会话 `main_turn`、不含 subagent 会话（与卡片逐字对齐；**三图口径不同，合计对不上账属预期，勿当 bug**） |
| 位置记忆 | 退出/形态变化时保存 x/y/贴边方向到 `zm_state.json`；恢复时按屏幕可视区夹取（分辨率变化/拔显示器后位置失效会被夹回，完全离屏则回主屏默认位） |

注意：手动固定期间「生成中」状态与计时来自**全局日志 tail**（`model.request.started/completed` 事件不区分会话），固定会话无请求时窗口仍可能显示别的会话的生成中与计时，请求完成也会刷新精确速度——自动模式同源，非新回归。

数据源（全部只读，不干扰对应工具）：

- `~/.zcode/cli/db/db.sqlite` — `model_usage`（token/耗时）、`part`（流式文本/活跃会话）、`session`（标题）
- `~/.zcode/cli/log/zcode-<日期>.jsonl` — 模型请求起止事件（tail）
- `~/.claude/projects/**/*.jsonl` — Claude Code 用量（只读解析，仅聚合展示，不进金额/燃速口径）
- `https://open.bigmodel.cn/api/monitor/usage/quota/limit` — 全程**唯一**外部请求端点（quota 轨，只读 GET，需自行配置 key）

## 配置（可选，放本工具同目录）

### `zm_config.json` — 预算与告警

```json
{
  "quota_api_key": "你在 bigmodel 开放平台的 API Key",
  "daily_budget_cny": 20,
  "alert_pct": [20, 10]
}
```

- `quota_api_key`：Coding Plan 套餐轨用。填写后按**事件驱动+节流**查询套餐余量：ZCode 有新请求完成即触发刷新，活跃期（过去 1h 内有请求）常态最长 3 分钟一次，静默期（>1h 无请求）暂停查询，最小间隔 60s；启动与设置界面改 key 后立即查一次。卡片显示「套餐剩余 N% · N分钟前」与本地计算的「Xh Ym 后重置」（倒计时零请求）、计费块按真实 5h 窗对齐块界；不填则该轨整体不显示（优雅降级）。**该文件含密钥，已加入 `.gitignore`，切勿提交**；工具的日志与调试输出不会打印 key 明文，设置窗中该字段为密码框（掩码显示）
- `daily_budget_cny`：按量付费日预算（元）。填写后显示燃速/还可撑 X 小时并启用按量告警轨
- `alert_pct`：告警阈值（剩余百分比，降序），默认 `[20, 10]` 即 20% 与 10% 各提醒一次
- **设置界面（右键 → 设置）保存即生效**：原子落盘（临时文件 + `os.replace`）成功后立即重启套餐轨轮询（清号/换号时旧账号数据零残留）、引擎与告警即时读新值，无需重启工具；直接手改文件仍需重启
- 同一天内新增/调低告警阈值后，新级别若当日已跌破会**立即补发一次气泡**（之后同级别同日仍只提醒一次，属预期而非重复告警）
- 保存失败（`ZM_NO_STATE=1` 隔离中未落盘、exe 目录只读等）时设置窗红字报错且**不生效**，配置保持原样

### `zm_prices.json` — 价格表覆盖（按模型合并）

```json
{
  "GLM-5.3": {"in": 8, "in_cache": 2, "out": 28},
  "GLM-4.7-Flash": {"in": 0, "in_cache": 0, "out": 0},
  "my-proxy-model": {"in": 4, "out": 16}
}
```

- 单位：元/百万 tokens。内置表为 2026-09 从 [bigmodel 官方定价文档](https://docs.bigmodel.cn/cn/guide/start/pricing) 转录的按量刊例价（`GLM-5.3-Flash` 取标准牌价，限时折扣期实付更低；官方按上下文分档计费的模型取最高档保守估算）
- 已有模型可只覆盖个别档位（如只改 `in_cache`）；新增模型需 `in`/`out` 必备，缺 `in_cache` 档时该模型 cache_read 按 in 全价计
- 坏文件（非 JSON）整体忽略回退内置表；模型条目非法则跳过该条目

## 打包（单文件 exe）

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name zcode-meter --exclude-module tkinter zcode_meter_qt.py
```

产物：`dist/zcode-meter.exe`。体积量级 **约 40–70MB**（PySide6 运行时打包的正常水平，与"轻量"无关）。

frozen 模式（exe）注意事项：

- `zm_crash.log` / `zm_debug.log` / `zm_state.json` 落在 **exe 同目录**，不再是源码目录；exe 放只读目录（如未提权的 Program Files）时崩溃日志会静默放弃（不致启动失败），建议放可写目录
- 开机自启注册表值应改为直接指向 exe：

```powershell
Set-ItemProperty "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" -Name zcode-meter `
  -Value '"<你的路径>\zcode-meter.exe"'
```

- `--exclude-module tkinter` 必须保留：数据层已移除 tkinter 依赖（`zcode_meter_qt.py` 不再经 `data_engine` 间接引入），不排除则体积翻倍；反之若未来恢复 tk 依赖而仍排除，exe 一启动即 `ImportError`

## 架构

```
zcode-meter/
├── data_engine.py       # 数据层(无 UI 依赖):日志tail + SQLite轮询 + 流式估算 + 会话跟随
│                        #   + 价格表/金额 + 多用量源(ZCode/Claude) + quota 轮询线程
│                        #   + 预算告警状态机 + 5h 计费块(QuotaMonitor 仅由 UI 实例化)
├── zcode_meter_qt.py    # Qt UI(PySide6):原生拖动/贴边胶囊/动态尺寸 + 托盘/告警派发
├── zcode_meter.py       # 旧 tkinter 版(弃用,留档)
└── tests/
    ├── run_all.py            # 一键回归
    ├── test_data_engine.py   # 口径/切换/subagent 排除 + 金额/燃速/计费块/多源/quota/告警单测
    ├── test_stress.py        # 布局切换压力测试
    └── debug/                # 历史调试工具(窗口定位/hit-test/注入拖动)
```

## 开发指南

### 回归测试（改代码后必跑）

```bash
python tests/run_all.py            # 全部
python tests/run_all.py data       # 只跑数据层单测
```

### 新增一个统计指标（三步）

1. **数据层** `data_engine.py`：`Snapshot` 加字段；在 `_poll_stats`（轮询）或 `_on_request_done`（请求完成瞬间）的 SQL 里取数赋值
2. **界面层** `zcode_meter_qt.py`：`_build_card`/`_build_bar` 加 label（竖条不显示的字段记得显式置 `None`，防悬空引用）；`_apply_snapshot` 渲染
3. **单测** `tests/test_data_engine.py`：对着 db 手算期望值加断言（口径回归就是这么防的）

## 调试与故障排查

| 症状 | 排查 |
|---|---|
| 窗口消失/崩溃 | 看 `zm_crash.log`（faulthandler 自动记录堆栈）；启动恢复时按屏幕可视区夹取 + 贴边尺寸 `_refit_dock` 周期自愈，无周期出界守护 |
| 交互事件异常 | `ZM_DEBUG=1` 启动，事件流写入 `zm_debug.log` |
| 数据不对 | 先跑 `python tests/run_all.py` 确认口径回归；再对照"显示指标与口径"表核查（今日/按天/按会话/计费块/多源各口径互不相等属预期） |
| 金额带 ≈ / 偏低 | 今日用了价格表未覆盖的模型（非 bigmodel 模型计 ¥0）：在 `zm_prices.json` 补该模型单价后重启 |
| 「套餐剩余」不显示 | 未配置 `quota_api_key`（该轨优雅降级）；或 quota 接口结构变化 —— 看 `zm_debug.log` 首跑失败落盘的响应片段（无 key 无法联调，结构取自 2026-09 社区逆向实测） |
| 告警气泡不弹 | Windows 专注助手开启时系统会抑制托盘气泡（系统行为非本工具 bug）；确认托盘图标存在；同级别同日只提醒一次（看 `zm_alerts.json`） |
| 燃速/还可撑偏大 | 刚启动或今日活跃不足 60 分钟时窗口未满、燃速被低估，属口径而非 bug（见口径表"燃速"行） |
| 两源今日对不上 | ZCode 与 Claude 口径不同（Claude 补 cache、含 sidechain），见口径表"多源今日"行 |
| 位置不被记忆 | 环境残留 `ZM_NO_STATE=1`（回归测试隔离开关，会禁用位置保存/恢复且无提示）——检查后移除；或 `zm_state.json` 所在目录不可写 |
| exe 版日志找不到 | frozen 模式下 `zm_crash.log`/`zm_debug.log`/`zm_state.json`/`zm_config.json`/`zm_prices.json` 落 **exe 同目录** |
| 推送要密码 | 仓库已配 `core.sshCommand` 指向系统 OpenSSH，配合 Windows ssh-agent 服务免密 |

## 路线图

- [x] 位置记忆（重启回到上次位置/形态）
- [x] 历史用量图表（按天/按会话）
- [x] 多会话手动切换（当前自动跟随最近活跃）
- [x] 打包单文件 exe（PySide6 + PyInstaller，见「打包」章节）
- [x] 成本估算 + 双轨预算告警 + 燃速预估（v0.4.0，见「配置」与口径表）
- [x] 5 小时计费块视角（v0.4.0）
- [x] 多 CLI 聚合：用量源抽象 + Claude Code 源（v0.4.0，更多源按 `UsageSource` 接口扩展）
- [x] 托盘模式（v0.4.0）
