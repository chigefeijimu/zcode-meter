"""zcode-meter —— ZCode token 用量与输出速度桌面小工具。

包结构(v0.9.2 起 src 布局;与 README 目录树互为镜像,单一事实):
- data_engine  数据层(日志 tail + SQLite 轮询 + 流式估算 + 价格/告警)
- app          Qt UI 入口(PySide6;原 zcode_meter_qt.py)
- skins        皮肤注册表(SkinDef×9;纯 QtGui+QtCore+stdlib,零 app/
               data_engine 依赖,防循环导入)
- glass_effect 液态玻璃抓屏管线(dxcam/cv2 可选依赖,按需惰性创建)
- __main__     CLI 伴侣(python -m zcode_meter / 脚本直跑;零 Qt 不启轮询)
- sources      用量源包(base=UsageSource 契约 + zcode/claude 实现,
               discover_sources() 自动发现)
- legacy_tk    旧 tkinter 版,仅留档不参与运行

本文件刻意保持零副作用:不 import 包内任何模块 —— data_engine 一被导入就启用
faulthandler 写 zm_crash.log,而「import zcode_meter」必须可以无开销地用于
版本探测(见 tests/test_package.py 的子进程断言)。版本探测的前提是
sys.path 已含 src/(本仓库无 pyproject/setup.py,包永远不会被安装):
在仓库根直接 import 会命中根目录兼容 shim zcode_meter.py —— 同名遮蔽,
shim 会转去启动 GUI 而不是给出版本号。
"""

__version__ = "0.9.2"
