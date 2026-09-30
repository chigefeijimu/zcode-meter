# zcode-meter

ZCode token 用量与输出速度的桌面悬浮小工具。贴边变胶囊条，实时输出速度、会话统计、成本估算与今日用量一目了然。

A Windows desktop widget that floats beside your ZCode sessions, showing token usage, cache hit rate, output speed and cost estimates in real time.

## 特性

- 🚀 **全局瞬时吞吐**：全部会话（主对话/子代理/工作流）正在输出的 tok/s 总和 = 流式增长 + 最近 10s 完成重叠加权
- 📊 **双速率统计**：实时速度 + 会话平均速度（按 提供商×模型 分组）
- 💰 **成本估算**：内置 bigmodel 按量刊例价（三档：输入/缓存命中/输出），卡片与横条的今日用量、按天图表同步显示 ¥ 金额（`zm_prices.json` 可覆盖）
- 🔔 **预算告警（双轨）**：Coding Plan 套餐轨（quota 接口轮询）+ 按量日预算轨（本地 token×价格表），跌破阈值托盘气泡提醒（默认 20%/10% 各一次，同级别同日不重复）
- 🔥 **燃速与耗尽预估**：按最近 60 分钟窗口计算每小时燃速，配合日预算显示"还可撑 X 小时"（v0.8.0 起降为卡片燃速格悬停显示）
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
pythonw src/zcode_meter/app.py        # 日常使用(无控制台,新入口)
python src/zcode_meter/app.py         # 调试(带控制台)
python src/zcode_meter/app.py --verify    # 自检:渲染/绑定/贴边判定
```

> v0.6.0 起采用 src 布局,新入口为 `src/zcode_meter/app.py`。根目录保留了一个
> 3 行兼容 shim `zcode_meter.py`,v0.5.x 的旧命令(`pythonw zcode_meter.py`)仍然
> 可用 —— 等价转发到新入口,仅作过渡,文档与脚本请逐步改用新路径。

开机自启（可选）：

```powershell
Set-ItemProperty "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" -Name zcode-meter `
  -Value '"C:\Windows\py.exe" -w "<你的路径>\zcode-meter\src\zcode_meter\app.py"'
```

## CLI 用量速查（无界面）

不想开悬浮窗、只想在终端快速看一眼近期用量时，用 CLI 伴侣（不启动 Qt/托盘/轮询线程）：

```bash
cd src && python -m zcode_meter cost --days 30     # 模块形态（cwd 必须在 src）
python src/zcode_meter/__main__.py cost --days 7   # 脚本直跑形态（仓库根即可）
```

输出为按天文本表（日期 / tokens / ¥）+ 合计行；窗口含价格表未覆盖的模型时金额为下限并附注脚（`≈`），口径与按天图表完全一致（ZCode-DB-only、`completed` 全部 query_source、按刊例价估算）。

- `--days` 默认 30，范围 1..366，超界自动截断；非法值（如非整数）打印错误并以退出码 2 结束
- 本仓库无 `pyproject.toml`/`setup.py`（包在 `src/` 下），模块形态必须 `cd src` 或设 `PYTHONPATH=src`；在仓库根直接 `python -m zcode_meter` 会命中根目录兼容 shim `zcode_meter.py`（同名遮蔽，会转去启动 GUI 而非 CLI）
- CLI 只做只读查询（读 `zm_config.json`/`zm_prices.json`，不写任何 zm_* 业务/状态文件；`import` 数据层既有的 faulthandler 追加 `zm_crash.log` 属全局行为，与 GUI 一致）

## 交互

| 操作 | 效果 |
|---|---|
| 任意位置按住拖动 | 移动窗口（系统原生拖动，跨显示器自由） |
| 拖到屏幕边缘松手 | 变胶囊条（宽高恰好包住内容，沿边自动居中；判定按**鼠标触边**而非窗口侧边） |
| 按住胶囊条拖离边缘 | 恢复卡片 |
| 右键 → 贴边/恢复卡片 | 直达四边 / 恢复卡片（形态变化即时保存，重启恢复） |
| 右键 → 皮肤 | 九款皮肤子菜单单选切换：玻璃仪表(默认) + 瑞士国际主义 / 琥珀 CRT 终端 / 黑板粉笔 / 液态玻璃药丸 / 工业机柜 / 报纸头版 / 蒸汽波落日 / 工程蓝图，当前项打勾；点击**即时生效**（卡片/横条/竖条三形态同语言换装，段开关与贴边几何机制不变）并写入 `zm_config.json` 持久化，重启恢复 |
| 右键 → 会话 | 列出最近 8 个会话（按 part 最新写入倒序）手动固定统计对象（状态行模型名前缀 📌，完整会话标题见窗口悬停提示）；「自动跟随(最近活跃)」恢复自动 |
| 右键 → 历史用量图表 | 打开独立窗口：按天(近30天，token+¥)/按会话(近20个)/计费块(5h) 三页签，可刷新；三图均为水平条（v0.5.2 起统一，左侧长标签按空间省略），**悬停显示完整标签与数值**；按天图另有近 7 天日均虚线与头部『本月预计 ¥』标注（近 7 天零用量时不显示） |
| 右键 → 设置 | 打开设置窗：quota API Key（密码框，界面任何位置不回显明文）/日预算/告警阈值/quota 刷新间隔；保存即写入 `zm_config.json` 并热生效（套餐轨立即重启轮询、刷新档位就地热更、引擎与告警即时读新值），失败时窗内红字报错且不生效 |
| 右键 → 收起到托盘 | 主窗隐藏到系统托盘（无托盘环境不显示此项）；托盘菜单 显示/隐藏、退出；单击托盘图标恢复 |
| 右键 → 退出 | 退出（退出时保存位置与形态，下次启动恢复） |

蓝色脉冲环 = 生成中；速度带 `~` = 全局吞吐为估算值（流式+窗口加权）；模型名带 📌 = 统计对象已被手动固定（完整会话标题悬停窗口可见）；金额带 `≈` = 含价格表未覆盖的模型，金额为下限估算。

## 显示指标与口径

| 指标 | 口径 |
|---|---|
| 瞬时速度（主数字） | **全局**：所有会话 tok/s 总和 = 各会话 part 流式增长（÷自校准字符比）+ 完成请求输出区间 [first_token, completed] 与最近 10s 窗口的重叠加权；不跟随会话切换，空闲为 -- |
| 会话平均速度 | 当前会话、按 提供商+模型 分组：Σ输出 ÷ Σ(duration−TTFT) |
| 速度趋势（sparkline） | 全局吞吐的逐秒采样最近 ~12 点（与主数字同口径），时间正序 |
| 入/出/缓存率 | 当前会话、main_turn；**input_tokens 已含 cache_read**，命中率 = cache/in |
| 首字等待/整体耗时 | 最近一次请求的 TTFT / duration |
| 今日用量 | 今天 0 点起**全部会话全部来源**（含 subagent），与 ZCode 自身口径一致；口径钉死 **ZCode-DB-only**，多源数字只做附加展示不并入 |
| 今日金额（¥） | ZCode-DB-only 按刊例价：`(in−cache_read)×in价 + cache_read×缓存价 + out×out价`，三档单价（元/M tokens）来自内置价格表。**已知模型缺 in_cache 档时 cache_read 按 in 全价计（宁可高估）；未知/非 bigmodel 模型计 ¥0 并置 partial（显示 ≈，金额为下限）**。订阅套餐内的用量实际不按量扣费，此金额是"按刊例价折算的等值成本"，用于预算感知 |
| 燃速 / 还可撑 X 小时 | 燃速 = 最近 **60 分钟**滚动窗口内的 token（及金额）之和；`还可撑 = (日预算−今日花费) ÷ 燃速¥/h`。**活跃不足 60 分钟时窗口未满、燃速被低估、预估偏大，属窗口口径而非 bug**；燃速为 0 或未配日预算时不显示；**v0.8.0 起「还可撑 X 小时」降为卡片燃速格悬停提示**（『预算还可撑 X.Xh』或『预算已超支』，预算告警气泡保留作主动提醒） |
| 多源今日（卡片第二行） | 各源今日用量聚合：ZCode 同今日口径；Claude = `input_tokens + cache_read + cache_creation`（**Anthropic 口径 input 不含 cache，须补齐后才与 ZCode 可比**），含 sidechain 行（对齐 ZCode 含 subagent），按 message.id 去重但跳过零用量/error 行，时间戳 UTC 转本地定天界。**两源口径不同，合计对不上属预期，勿当 bug** |
| 计费块（5h 页签） | 按 5 小时窗聚合 `completed` 的 **query_source 全部** in+out（同今日 token 口径）。块界对齐：已配置 quota 时按 `nextResetTime−k×5h`（平台真实计费窗，黄色高亮=当前活动块）；未配置时回退锚点=最早 completed 请求时刻，**此时块界为示意、非平台真实计费窗** |
| 套餐剩余（quota 轨） | GET `open.bigmodel.cn/api/monitor/usage/quota/limit`（只读，**事件驱动+节流**：ZCode 有新请求完成即视为「有消耗」触发刷新，活跃期（过去 1h 内有请求）常态最长 3 分钟一次，静默期（>1h 无任何请求）暂停查询，最小间隔 60s 硬闸；启动/设置界面改 key 后立即查一次；可在设置窗改为**固定间隔轮询**，改档后静默期不再暂停，见配置节 `quota_refresh`）：`percentage` 为**已用**百分比，剩余 = 100−percentage，取 TOKENS_LIMIT 中 `number==5` 的条目即 5h 计费窗。只有剩余% 需要网络刷新；重置倒计时纯本地每分钟递减、不发任何请求。旁注查询时间（如「· 3分钟前」），静默期数据冻结属预期、以新鲜度标注为准（固定间隔档按设置间隔刷新、无静默冻结）。接口为社区逆向的非公开文档接口，结构变化时该行不显示（首跑失败会把响应片段落 `zm_debug.log` 便于修） |
| 预算告警（双轨） | quota 轨 = 套餐 5h 窗剩余%；按量轨 = (日预算−今日花费 ZCode 口径) ÷ 日预算。默认阈值剩余 20% / 10% 各提醒一次，**同级别同日只提醒一次**、跨日自动重置（状态存 `zm_alerts.json`），经托盘气泡派发 |
| 会话跟随 | 按 `part` 表最新写入行判定（排除 `sess_subagent_*` 与 `sess_dwf-*`——子代理/工作流会话）；自动（最近活跃）或手动固定。菜单列出最近会话走引擎侧 `sid→rowid` 缓存（暖读 ≤1ms，冷时回退一次全量 SQL） |
| 按天图表 | `completed` **全来源** in+out（同今日口径），本地午夜天界；柱身第二行为当日 ¥（按刊例价，同今日金额口径，含未知模型时为下限）。**历史聚合防御：仅统计最近 10 万行，超出上限的更早记录不计（当前约 78 天用量）**——历史图表查询（按天/计费块/按会话）同受此防线保护，以防未来"图表变小"被误报为 bug |
| 按会话图表 | 该会话 `main_turn`、不含 subagent 会话（与卡片逐字对齐；**三图口径不同，合计对不上账属预期，勿当 bug**） |
| 位置记忆 | 退出/形态变化时保存 x/y/贴边方向到 `zm_state.json`；恢复时按屏幕可视区夹取（分辨率变化/拔显示器后位置失效会被夹回，完全离屏则回主屏默认位） |

注意：手动固定期间「生成中」状态与计时来自**全局日志 tail**（`model.request.started/completed` 事件不区分会话），固定会话无请求时窗口仍可能显示别的会话的生成中与计时，请求完成也会刷新精确速度——自动模式同源，非新回归。

数据源（全部只读，不干扰对应工具）：

- `~/.zcode/cli/db/db.sqlite` — `model_usage`（token/耗时）、`part`（流式文本/活跃会话）、`session`（标题）
- `~/.zcode/cli/log/zcode-<日期>.jsonl` — 模型请求起止事件（tail）
- `~/.claude/projects/**/*.jsonl` — Claude Code 用量（只读解析，仅聚合展示，不进金额/燃速口径）
- `https://open.bigmodel.cn/api/monitor/usage/quota/limit` — 全程**唯一**外部请求端点（quota 轨，只读 GET，需自行配置 key）

## 配置（可选，放本工具同目录）

> 「本工具同目录」的落点：**开发模式（脚本运行）一律落仓库根**（即 `README.md`
> 所在目录,`app_dir()` 向上锚定,不随 v0.6.0 的 src 布局搬到 `src/zcode_meter/`）;
> **frozen（exe）模式落 exe 同目录**。六个文件共用同一落点：`zm_config.json` /
> `zm_prices.json` / `zm_alerts.json` / `zm_state.json` / `zm_debug.log` / `zm_crash.log`。

### `zm_config.json` — 预算与告警

```json
{
  "quota_api_key": "你在 bigmodel 开放平台的 API Key",
  "daily_budget_cny": 20,
  "alert_pct": [20, 10]
}
```

- `quota_api_key`：Coding Plan 套餐轨用。填写后按**事件驱动+节流**查询套餐余量：ZCode 有新请求完成即触发刷新，活跃期（过去 1h 内有请求）常态最长 3 分钟一次，静默期（>1h 无请求）暂停查询，最小间隔 60s；启动与设置界面改 key 后立即查一次。卡片两级显示：主数字「套餐剩余 N%」醒目，新鲜度与本地计算的倒计时并入弱化副文本「· N分钟前 · Xh Ym 后重置」（倒计时零请求）、计费块按真实 5h 窗对齐块界；不填则该轨整体不显示（优雅降级）。**该文件含密钥，已加入 `.gitignore`，切勿提交**；工具的日志与调试输出不会打印 key 明文，设置窗中该字段为密码框（掩码显示）
- `daily_budget_cny`：按量付费日预算（元）。填写后显示燃速/还可撑 X 小时并启用按量告警轨
- `alert_pct`：告警阈值（剩余百分比，降序），默认 `[20, 10]` 即 20% 与 10% 各提醒一次
- `quota_refresh`（可选）：quota 刷新档位。缺省 = **自动（事件驱动）**，即上面 `quota_api_key` 描述的节流语义；也可在设置窗「quota 刷新间隔」下拉改为**固定间隔轮询**（预设 3 / 5 / 15 / 30 分钟，或手写 60~86400 秒之间的任意整数）——固定档忽略活动/静默信号、到点就查（静默期不再暂停，代价是无人值守时也按间隔发只读请求），启动/换 key 后仍立即查一次。合法值仅 `"auto"` 或 60~86400 的整数秒，其他值整体忽略（视为自动）；选择「自动」保存时该键**整体省略**（不写入文件），旧三键配置文件不受影响
- `skin`（可选）：界面皮肤。缺省 = **玻璃仪表**；合法值为九款内置皮肤的 id（`glass`/`swiss`/`crt`/`chalk`/`liquid`/`industrial`/`newspaper`/`vaporwave`/`blueprint`，定义在 `src/zcode_meter/skins.py`，皮肤=内置写死、无外部皮肤文件/编辑器/热加载），白名单外任意值（含手改文件写错、文件缺键、坏 JSON）**一律回退玻璃仪表**；选玻璃保存时该键**整体省略**（不写入文件，与 `quota_refresh="auto"` 省键同纪律，存量配置文件零迁移）。右键「皮肤」菜单切换**即时生效并落盘**（`_no_persist` 守卫环境静默跳过），直接手改文件需重启生效；设置窗保存会原样保留该键与 `bar_segments`（可选键不再被设置窗的四键白名单抹掉）
- **设置界面（右键 → 设置）保存即生效**：原子落盘（临时文件 + `os.replace`）成功后立即重启套餐轨轮询（清号/换号时旧账号数据零残留）、引擎与告警即时读新值，无需重启工具；quota 刷新档位改动**就地热更**（不重启轮询线程，1 秒内生效）；直接手改文件仍需重启
- 同一天内新增/调低告警阈值后，新级别若当日已跌破会**立即补发一次气泡**（之后同级别同日仍只提醒一次，属预期而非重复告警）
- 保存失败（配置目录不可写，如 exe 放在只读目录）时设置窗红字报错且**不生效**，配置保持原样

### `zm_prices.json` — 价格表覆盖（按模型合并）

**多数情况不需要这个文件** —— 内置表已覆盖 bigmodel 全系模型且随官方价目更新。
需要它的场景：① 官方调价/限时折扣（把新价写进来，如 Flash 五折期）；② 你走私有
代理/网关，实际单价与刊例不同；③ 内置表缺失的新模型。

```json
{
  "GLM-5.3-Flash": {"in": 0.4, "in_cache": 0.115, "out": 1.4},
  "my-proxy-model": {"in": 4, "in_cache": 0.8, "out": 16}
}
```

- 单位：元/百万 tokens。内置表为 2026-09 从 [bigmodel 官方定价文档](https://docs.bigmodel.cn/cn/guide/start/pricing) 转录的按量刊例价（`GLM-5.3-Flash` 取标准牌价，限时折扣期实付更低；官方按上下文分档计费的模型取最高档保守估算）
- 已有模型可只覆盖个别档位（如只改 `in_cache`）；新增模型需 `in`/`out` 必备，缺 `in_cache` 档时该模型 cache_read 按 in 全价计
- 坏文件（非 JSON）整体忽略回退内置表；模型条目非法则跳过该条目

## 隐私与安全

**网络唯一出口**：全程只有一种外部请求 —— quota 轨的只读 `GET https://open.bigmodel.cn/api/monitor/usage/quota/limit`，且仅在用户配置 `quota_api_key` 后发起；其余一切统计（用量/速度/金额/图表）均来自本机文件读取，不访问任何其他网络端点。

**API Key 三层防护**：

- **不入库**：`zm_config.json`（存 `quota_api_key`）已加入 `.gitignore`（实测该条目在第 13 行；`.gitignore` 中 `zm_*` 条目均为 basename 模式，src 布局移动文件后仍全部命中），不会被提交；
- **不回显**：设置窗中该字段为密码框（`QLineEdit.EchoMode.Password`，掩码显示），界面任何位置不出现明文；
- **不落日志**：任何日志与调试路径都不得打印 key 明文（数据层红线注释，泄漏面专查项）；quota 首跑失败落 `zm_debug.log` 的是响应体片段（key 只在请求头、响应体不含 key），同样无泄漏面。

**git 历史零密钥残留**（主张严格限定为下列六个文件）：六个密钥/状态文件 `zm_config.json` / `zm_prices.json` / `zm_alerts.json` / `zm_state.json` / `zm_debug.log` / `zm_crash.log` 在全部提交历史中零记录（`git log --all -- <六文件>` 输出为空）。如实注脚：`zm_stdout.log` 曾被提交 `2622bcf` 误提交（内容为空文件）并由 `08624e8` 移出跟踪，现由 `.gitignore` 规则覆盖 —— 密钥类文件从未进入仓库。

**只读，不干扰被监控工具**：ZCode 数据库一律走 `sqlite3` 的 `mode=ro` 只读连接（`connect_ro`）；Claude 用量为 `~/.claude/projects/**/*.jsonl` 只读解析；本工具绝不写 ZCode 或 Claude 的任何文件。

**本工具自身落盘文件**（完整清单，不止上述六个）：

- `zm_config.json` / `zm_prices.json` / `zm_alerts.json` / `zm_state.json` / `zm_debug.log` / `zm_crash.log` —— 六个配置/状态/日志文件，均锚定 `app_dir()`（开发模式=仓库根，frozen=exe 同目录，见「配置」节的落点说明）；
- `zm_usage_export.csv` —— 仅在右键「导出 CSV」时写出的用量明细（日期/模型/token/金额，无密钥），与上述六文件同锚 `app_dir()`，同样已加入 `.gitignore`（用户数据不进库）；
- `zm_error.log` —— 仅两处 UI 异常兜底路径写入 traceback（不含密钥），落点相对启动时的工作目录（既有行为）；
- 以上即本工具运行期写出的全部持久文件（配置保存的瞬态临时文件写完即原子改名，不残留）。

## 打包（单文件 exe）

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name zcode-meter --paths src --exclude-module tkinter src/zcode_meter/app.py
```

- `--paths src` 必须带上:PyInstaller 静态分析要能解析 `zcode_meter` 包,缺了会打包成功但 exe 一启动即 `ImportError`
- 入口文件为 `src/zcode_meter/app.py`
- **watchdog 为可选依赖**(v0.9 起引擎侧 Claude jsonl watcher 使用,`pip install watchdog` 后再打包才带入;缺它 exe 仍完全可用 —— watcher 在函数内 import,`ImportError` 时落一条 dbg 后优雅降级,Claude 行回退既有的 15s TTL 扫描节奏,**数据永不错只是慢**;不装它打包则省一个运行时依赖)

产物：`dist/zcode-meter.exe`。体积量级 **约 40–70MB**（PySide6 运行时打包的正常水平，与"轻量"无关）。

frozen 模式（exe）注意事项：

- `zm_crash.log` / `zm_debug.log` / `zm_state.json` 落在 **exe 同目录**，不再是仓库根（开发模式落点）；exe 放只读目录（如未提权的 Program Files）时崩溃日志会静默放弃（不致启动失败），建议放可写目录
- 开机自启注册表值应改为直接指向 exe：

```powershell
Set-ItemProperty "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" -Name zcode-meter `
  -Value '"<你的路径>\zcode-meter.exe"'
```

- `--exclude-module tkinter` 必须保留：数据层已移除 tkinter 依赖（`src/zcode_meter/app.py` 不再经 `data_engine` 间接引入），不排除则体积翻倍；反之若未来恢复 tk 依赖而仍排除，exe 一启动即 `ImportError`

## 架构

```
zcode-meter/
├── src/zcode_meter/
│   ├── __init__.py      # 包标识 + 版本号(零副作用,不 import 包内模块)
│   ├── data_engine.py   # 数据层(无 UI 依赖):日志tail + SQLite轮询 + 流式估算 + 会话跟随
│   │                    #   + 价格表/金额 + quota 轮询线程 + 预算告警状态机
│   │                    #   + 5h 计费块(QuotaMonitor 仅由 UI 实例化)
│   │                    #   + ZCode db 文件闸门+today0 时间闸(db 未变跳过
│   │                    #   空闲期 SQL 段,跨午夜强制开闸保清零口径;事件路径
│   │                    #   不受管辖)+ _wake 事件唤醒与 Claude jsonl watcher
│   │                    #   调度(watchdog 可选,缺它回退 15s TTL 扫描)
│   ├── sources/         # 用量源包(Provider 配置化):base=UsageSource 接口
│   │                    #   zcode=ZCode 源 + ZCODE_DIR/DB_PATH/connect_ro/today0_ms
│   │                    #   唯一定义 / claude=Claude 源;__init__.discover_sources()
│   │                    #   自动发现,data_engine re-import 保住旧导入路径
│   ├── skins.py         # 皮肤注册表(SkinDef×9:玻璃默认+8 款):palette/QSS 统一
│   │                    #   模板/radius/字族/QPainter deco(纯绘制无图片依赖);
│   │                    #   纯数据+绘制模块,零 app/data_engine 依赖(防循环导入)
│   ├── app.py           # Qt UI 入口(原 zcode_meter_qt.py):原生拖动/贴边胶囊/动态尺寸
│   │                    #   + 托盘/告警派发
│   ├── __main__.py      # CLI 伴侣(python -m zcode_meter / 脚本直跑):无界面
│   │                    #   用量速查,零 Qt,不启轮询线程(见「CLI 用量速查」)
│   └── legacy_tk.py     # 旧 tkinter 版(弃用,仅留档)
├── zcode_meter.py       # 兼容 shim:旧命令转发到 src/zcode_meter/app.py(v0.6.0 过渡)
├── README.md / CHANGELOG.md / LICENSE / .gitignore
└── tests/
    ├── run_all.py            # 一键回归
    ├── test_data_engine.py   # 口径/切换/subagent 排除 + 金额/燃速/计费块/多源/quota/告警单测
    ├── test_package.py       # src 布局守卫:包可导入/版本号/app_dir 落点/shim 链路
    ├── test_stress.py        # 布局切换压力测试
    └── debug/                # 历史调试工具(窗口定位/hit-test/注入拖动)
```

## 开发指南

### 回归测试（改代码后必跑，在仓库根运行）

```bash
python tests/run_all.py            # 全部
python tests/run_all.py data       # 只跑数据层单测
```

### 人工目视清单（皮肤 9×3，回归测试与截图矩阵不能全替代，发版前过一遍）

九皮肤 × 卡片/横条/竖条三形态的机器渲染矩阵由 `findings/render_skins_matrix.py`
产出（`findings/skins_matrix.png`，与 `design/skins-8x3.html` 定稿逐款比对）；
以下各项截图无法自动判定，需人工目视：

- 浅色两款（瑞士/报纸）叠在**深色与浅色两块壁纸**下的边缘观感（窗口本身不透明，壁纸只影响投影/边缘一圈观感）
- 125% DPI（及如有 100/150%）下各皮肤**主数字不截断**
- CRT 扫描线（1px/3px 重复横线）**不糊字**——条纹叠加后表格数字仍清晰可读
- 蒸汽波太阳条纹（QPainterPath 条纹模拟 mask-composite）与青色透视网格正常呈现
- 黑板木框四角完整（#7a5a38 边框在圆角处不断裂）与两团粉笔灰
- 报纸双栏细线（栏间 1px 栏线）与页眉双线（masthead double rule）
- 液态玻璃 6 只等大药丸 3 列 2 行、黑板卡片入/出同一行（两处定稿微调）
- 已知舍弃项（HTML 保真偏差）：报纸横条金额灰档（HTML 的 `color:#777` 分色）不做——单 QLabel 承载量+价，富文本会破坏 `_bar_size` 的 QFontMetrics 量宽

### 新增一个统计指标（三步）

1. **数据层** `src/zcode_meter/data_engine.py`：`Snapshot` 加字段；在 `_poll_stats`（轮询）或 `_on_request_done`（请求完成瞬间）的 SQL 里取数赋值
2. **界面层** `src/zcode_meter/app.py`：`_build_card`/`_build_bar` 加 label（竖条不显示的字段记得显式置 `None`，防悬空引用）；`_apply_snapshot` 渲染
3. **单测** `tests/test_data_engine.py`：对着 db 手算期望值加断言（口径回归就是这么防的）

### 如何贡献一个源（新 CLI 接入）

用量源走 `src/zcode_meter/sources/` 包做 Provider 配置化：`DataEngine` 构造时
`discover_sources()` 自动枚举包内源模块。**新增一个 CLI 支持只需加一个文件、
零改动 `data_engine.py`** —— 新模块放进包即可被自动发现（源不做运行时热插拔：
运行中新增的文件不会自动加载，重启生效）。

模块模板（`src/zcode_meter/sources/mycli.py`，发现机制只认模块级 `Source`
这个名字，类名本身随意；必须可无参构造）：

```python
from .base import UsageSource


class MyCliSource(UsageSource):
    """MyCLI 源:只读解析 ~/.mycli/usage.jsonl。

    口径注释要求（必须写清,不写会被后人当 bug 修错方向）:
    - in/out 各自的定义(是否含 cache 命中、是否含 subagent/子任务行);
    - 天界时区(timestamp 是 UTC 还是本地,天界按哪个时区的午夜切)。
    两源口径不同时卡片并列展示各自数字,合计对不上属预期(见口径表)。
    """

    name = "MyCLI"     # 卡片 today_by_source 聚合行显示名
    order = 20         # discover_sources 排序键(order 小者在前,同 order 按
                       # 模块名);内置 ZCode=0 / Claude=10,第三方建议 >=20

    def is_available(self) -> bool:
        ...            # 数据落点不存在等情形返回 False(优雅降级,不炸)

    def today_usage(self) -> int:
        ...            # 今日 in+out 总量(本地午夜天界)

    def daily_usage(self, days: int = 30) -> list:
        ...            # [(iso日期, tokens)] 升序,与 ZCode 源同形


Source = MyCliSource   # 发现约定:模块级 Source 属性 = 本模块的源类
```

红线（违反即回归）：

- **只读**：绝不写目标 CLI 的任何文件（ZCode 走 `connect_ro()` 的 `mode=ro`，
  Claude 只读解析 jsonl）
- **严禁在 sources 包内 `import data_engine`**：反向导入会造成循环导入/双模块
  实例，`QuotaMonitor` 与缓存身份分裂（src 布局迁移踩过的同类坑）
- **金额/燃速口径钉死 ZCode-DB-only**：新源只进 `today_by_source` 聚合展示，
  不要并入金额（见 `sources/base.py` 的 `UsageSource` docstring）
- **frozen（exe）包新增源必须重新打包**，且 PyInstaller 静态分析发现不了
  pkgutil 动态导入的模块 —— 需在 `sources/__init__.py` 的字面导入行补上模块
  名（或给打包命令加 `--hiddenimport`），否则 exe 启动即 `ImportError`
- 新源请配单测（合成数据手算对账，参考 `test_claude_source_synthetic` /
  `test_discover_sources`）

## 路线图

- [x] 位置记忆（重启回到上次位置/形态）
- [x] 历史用量图表（按天/按会话）
- [x] 多会话手动切换（当前自动跟随最近活跃）
- [x] 打包单文件 exe（PySide6 + PyInstaller，见「打包」章节）
- [x] 成本估算 + 双轨预算告警 + 燃速预估（v0.4.0，见「配置」与口径表）
- [x] 5 小时计费块视角（v0.4.0）
- [x] 多 CLI 聚合：用量源抽象 + Claude Code 源（v0.4.0，更多源按 `UsageSource` 接口扩展；现走 `sources/` 包自动发现，见「如何贡献一个源」）
- [x] 托盘模式（v0.4.0）
