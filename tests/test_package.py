#!/usr/bin/env python3
"""src 布局迁移守卫(v0.6.0)+ 版本/零副作用红线(v0.9.2):包可导入性 /
版本号对账 / app_dir 仓库根锚定 / shim 链路 / 零副作用子进程断言。

背景:v0.6.0 把 data_engine/zcode_meter_qt(现 app)/zcode_meter(现 legacy_tk)
从仓库根移入 src/zcode_meter/。这里钉死四条迁移不变式:
- 包可导入且零副作用(import zcode_meter 不拉 Qt、不开日志文件)
- app_dir() 在脚本模式锚定仓库根 —— 六个 zm_* 用户文件(配置/状态/日志)必须
  继续落仓库根,搬家即配额 key 无声失配(load_config 吞 OSError 回退默认)
- 根目录 zcode_meter.py shim 与新入口链路等价(--verify 可执行证据)

v0.9.2 起新增三条可执行守卫(此前只活在 docstring 里,后人破坏无测试变红):
- 版本对账(registry open#1):__version__ 与 CHANGELOG.md 头部版本行必须
  一致 —— 两侧任一单改即红,版本漂移不再无机制暴露
- 零副作用守卫(registry open#2):子进程 import zcode_meter 后断言
  data_engine/app 不在 sys.modules —— 往 __init__.py 加任何包内 import
  都会让本测试变红
- 红线断言包(registry open#56 + #33 断言侧):子进程逐个 import sources
  包成员与 skins,断言 data_engine/app 不被连带拉入(skins 只
  QtGui+QtCore+stdlib、sources 包严禁反向导入 data_engine 的事实钉死)
"""
import os
import re
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


def _changelog_head_version():
    """CHANGELOG.md 头部第一个『## vX.Y.Z』行 —— 版本叙事的单一事实源。

    头部行由发版时最新条目占据,与 __init__.__version__ 对账即可让
    「测试硬编码镜像」与「changelog 忘记升版」两类漂移同时变红。
    """
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## v(\d+\.\d+\.\d+)", text, re.M)
    return m.group(1) if m else None


def _probe_no_pull(module_names, extra_forbidden=()):
    """子进程导入 module_names,断言重模块不被连带拉入。

    - 全部走子进程:本文件顶部已 import app/data_engine,进程内 sys.modules
      早被污染,只有干净子进程才能如实观测 __init__/skins 的导入副作用;
    - src 必须插 sys.path 首位:cwd 在仓库根时『import zcode_meter』会命中
      根目录 shim zcode_meter.py(见 __init__.py docstring 的版本探测前提)。
    """
    mods = ", ".join(module_names)
    code = (
        "import sys; sys.path.insert(0, {src!r})\n"
        "import {mods}\n"
        "bad = [m for m in {forbidden!r} if m in sys.modules]\n"
        "print('PULLED:' + ','.join(bad) if bad else 'CLEAN')\n"
    ).format(src=str(ROOT / "src"), mods=mods,
             forbidden=("zcode_meter.data_engine", "zcode_meter.app", *extra_forbidden))
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=90,
                       env=dict(os.environ, ZM_NO_STATE="1"))
    out = (p.stdout + p.stderr).strip()
    return p.returncode == 0 and out == "CLEAN", out.splitlines()[-1] if out else "(无输出)"


def main() -> int:
    head = _changelog_head_version()
    check(f"__version__ 与 CHANGELOG 头部版本一致(头部=v{head})",
          getattr(zcode_meter, "__version__", None) == head and head is not None,
          f"__init__={getattr(zcode_meter, '__version__', None)} changelog={head}")
    check("from zcode_meter import data_engine 暴露 DataEngine/DB_PATH",
          hasattr(data_engine, "DataEngine") and hasattr(data_engine, "DB_PATH"))

    # (import zcode_meter.app 在模块顶部完成 —— 放函数内会把 zcode_meter
    #  变成局部名,触发 UnboundLocalError,已踩过)
    check("import zcode_meter.app 可导入", "zcode_meter.app" in sys.modules)

    check("app_dir()==仓库根(zm_* 文件落点不变式)",
          data_engine.app_dir() == str(ROOT),
          f"{data_engine.app_dir()} != {ROOT}")

    # 零副作用守卫:__init__.py 加任何包内 import 即红(registry open#2)
    ok, detail = _probe_no_pull(["zcode_meter"])
    check("零副作用:子进程 import zcode_meter 不拉 data_engine/app", ok, detail)

    # 红线断言包(registry open#56):sources 逐成员 + skins,严禁反向导入;
    # skins 侧加禁 QtWidgets —— 『只 QtGui+QtCore+stdlib』的事实钉死(#33 断言侧)
    ok, detail = _probe_no_pull(
        ["zcode_meter.sources.base", "zcode_meter.sources.zcode",
         "zcode_meter.sources.claude", "zcode_meter.skins"],
        extra_forbidden=("PySide6.QtWidgets",))
    check("红线:sources 逐成员+skins 导入不拉 data_engine/app/QtWidgets", ok, detail)

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
