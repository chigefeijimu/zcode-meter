"""zcode-meter —— ZCode token 用量与输出速度桌面小工具。

包结构(v0.6.0 起 src 布局):
- data_engine  数据层(日志 tail + SQLite 轮询 + 流式估算 + 价格/告警)
- app          Qt UI 入口(PySide6;原 zcode_meter_qt.py)
- legacy_tk    旧 tkinter 版,仅留档不参与运行

本文件刻意保持零副作用:不 import 包内任何模块 —— data_engine 一被导入就启用
faulthandler 写 zm_crash.log,而「import zcode_meter」必须可以无开销地用于
版本探测(见 tests/test_package.py 断言①)。
"""

__version__ = "0.6.0"
