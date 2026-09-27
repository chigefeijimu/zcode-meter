#!/usr/bin/env python3
"""src 布局迁移守卫(v0.6.0):包可导入性 / 版本号 / app_dir 仓库根锚定 / shim 链路。

背景:v0.6.0 把 data_engine/zcode_meter_qt(现 app)/zcode_meter(现 legacy_tk)
从仓库根移入 src/zcode_meter/。这里钉死四条迁移不变式:
- 包可导入且零副作用(import zcode_meter 不拉 Qt、不开日志文件)
- app_dir() 在脚本模式锚定仓库根 —— 六个 zm_* 用户文件(配置/状态/日志)必须
  继续落仓库根,搬家即配额 key 无声失配(load_config 吞 OSError 回退默认)
- 根目录 zcode_meter.py shim 与新入口链路等价(--verify 可执行证据)
"""
import os
import subprocess
import sys
from pathlib import Path

# 与 test_stress 同款隔离开关:直接运行本文件时也不触碰用户真实状态文件
os.environ.setdefault("ZM_NO_STATE", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import zcode_meter  # noqa: E402
import zcode_meter.app  # noqa: E402,F401  会拉 PySide6,与 test_stress 同代价
from zcode_meter import data_engine  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    check("__version__ == '0.6.0'", getattr(zcode_meter, "__version__", None) == "0.6.0",
          str(getattr(zcode_meter, "__version__", None)))
    check("from zcode_meter import data_engine 暴露 DataEngine/DB_PATH",
          hasattr(data_engine, "DataEngine") and hasattr(data_engine, "DB_PATH"))

    # (import zcode_meter.app 在模块顶部完成 —— 放函数内会把 zcode_meter
    #  变成局部名,触发 UnboundLocalError,已踩过)
    check("import zcode_meter.app 可导入", "zcode_meter.app" in sys.modules)

    check("app_dir()==仓库根(zm_* 文件落点不变式)",
          data_engine.app_dir() == str(ROOT),
          f"{data_engine.app_dir()} != {ROOT}")

    # shim 链路:python zcode_meter.py --verify(shim 补 sys.path[0] 后转发,
    # main() 直读 sys.argv);裸启动 GUI 常驻不可脚本断言,以 --verify 为可执行证据
    p = subprocess.run([sys.executable, str(ROOT / "zcode_meter.py"), "--verify"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60,
                       env=dict(os.environ, ZM_NO_STATE="1"))
    out = (p.stdout + p.stderr).strip()
    check("shim --verify 退出码 0 且含 PASS", p.returncode == 0 and "PASS" in out,
          f"rc={p.returncode} tail={out.splitlines()[-1] if out.splitlines() else '(空)'}")

    if FAILED:
        print(f"\nFAILED: {FAILED}")
        return 1
    print("\nPACKAGE TESTS ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
