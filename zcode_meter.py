"""兼容入口(v0.6.0 src 布局迁移):转发到包内新入口 src/zcode_meter/app.py。"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))  # 必须插在脚本目录之前,否则 import zcode_meter 会解析到本 shim 自身
from zcode_meter.app import main  # noqa: E402

main()  # main() 直读 sys.argv,--verify 等参数原样透传
