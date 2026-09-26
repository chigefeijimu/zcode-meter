# zcode-meter

ZCode token 用量与输出速度的桌面悬浮小工具。贴边变胶囊条，实时输出速度、会话统计与今日用量一目了然。

A Windows desktop widget that floats beside your ZCode sessions, showing token usage, cache hit rate and output speed in real time.

## 特性

- 🚀 **实时输出速度**：生成中按流式文本增速估算（自动校准换算率），请求完成瞬间切换为精确值
- 📊 **双速率统计**：实时速度 + 会话平均速度（按 提供商×模型 分组）
- 💾 **缓存命中率**：与 ZCode 计费口径一致的精确统计
- 🕐 **延迟指标**：首字等待（TTFT）与单次输出整体耗时
- 📅 **今日总用量**：跨会话、含子代理，与 ZCode 自身统计对账一致
- 🧲 **贴边胶囊条**：拖到屏幕边缘变一条恰好包住文字的胶囊，动态尺寸随分辨率/DPI 自愈
- 🖱️ **原生拖动**：Qt `startSystemMove` 系统级接管，120Hz 高刷屏丝滑跟手
- 🔄 **会话跟随**：切换 ZCode 会话自动切换统计对象（含会话标题）

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
| 右键 | 菜单：直达四边 / 恢复卡片 / 退出 |

绿色呼吸点 = 生成中；速度带 `~` = 流式估算值。

## 显示指标与口径

| 指标 | 口径 |
|---|---|
| 实时速度 | 当前/最近一次主对话请求 |
| 会话平均速度 | 当前会话、按 提供商+模型 分组：Σ输出 ÷ Σ(duration−TTFT) |
| 入/出/缓存率 | 当前会话、main_turn；**input_tokens 已含 cache_read**，命中率 = cache/in |
| 首字等待/整体耗时 | 最近一次请求的 TTFT / duration |
| 今日用量 | 今天 0 点起**全部会话全部来源**（含 subagent），与 ZCode 自身口径一致 |
| 会话跟随 | 按 `part` 表最新写入行判定（排除 `sess_subagent_*`） |

数据源（全部只读，不干扰 ZCode）：

- `~/.zcode/cli/db/db.sqlite` — `model_usage`（token/耗时）、`part`（流式文本/活跃会话）、`session`（标题）
- `~/.zcode/cli/log/zcode-<日期>.jsonl` — 模型请求起止事件（tail）

## 架构

```
zcode-meter/
├── data_engine.py       # 数据层(无 UI 依赖):日志tail + SQLite轮询 + 流式估算 + 会话跟随
├── zcode_meter_qt.py    # Qt UI(PySide6):原生拖动/贴边胶囊/动态尺寸
├── zcode_meter.py       # 旧 tkinter 版(弃用,留档)
└── tests/
    ├── run_all.py            # 一键回归
    ├── test_data_engine.py   # 口径/切换/subagent 排除单测
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

### 迭代工作流

本仓库配有一个 ZCode 迭代流水线（`.zcode/workflows/zcode-meter-iterate.dwf.ts`），在 ZCode 中说：

> 用 zcode-meter-iterate 工作流，需求：\<要修的 bug 或要加的功能\>

它会自动走五个阶段：**理解需求并分析影响面**（读口径表，bug 自动归因 zm_debug/zm_crash 日志）→ **独立评审实现计划**（对照历史踩坑记录查漏）→ **按计划实现** → **自动回归直到全绿**（跑 run_all，最多 3 轮修复）→ **产出迭代报告**（CHANGELOG 建议 + 需人工实测的验收清单）。

## 调试与故障排查

| 症状 | 排查 |
|---|---|
| 窗口消失/崩溃 | 看 `zm_crash.log`（faulthandler 自动记录堆栈）；出界守护每 2 秒会把飞出屏幕的窗口拉回 |
| 交互事件异常 | `ZM_DEBUG=1` 启动，事件流写入 `zm_debug.log` |
| 数据不对 | 先跑 `python tests/run_all.py` 确认口径回归；再对照"显示指标与口径"表核查 |
| 推送要密码 | 仓库已配 `core.sshCommand` 指向系统 OpenSSH，配合 Windows ssh-agent 服务免密 |

## 路线图

- [ ] 位置记忆（重启回到上次位置/形态）
- [ ] 历史用量图表（按天/按会话）
- [ ] 多会话手动切换（当前自动跟随最近活跃）
- [ ] 打包单文件 exe（PySide6 + Nuitka/PyInstaller）

## 历史要点（为什么是现在这个架构）

- tkinter 版拖动迟滞/吞点击/互操作崩溃 → 迁移 PySide6（`startSystemMove` 原生拖动）
- Win32 窗口子类化方案：ctypes 高频回调与 Tk/GC 冲突访问违例 → 彻底弃用
- 禁 Aero Snap（摘除 `WS_THICKFRAME|WS_MAXIMIZEBOX`）防系统贴靠与变条逻辑打架
- 条尺寸必须动态（`_refit_dock` 周期校验自愈），固定尺寸在分辨率/DPI 变化后脱节
