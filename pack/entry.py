# -*- coding: utf-8 -*-
"""打包入口:PyInstaller bundle 里没有 src/ 目录,根 shim 的路径插入
不成立 —— 本文件只做纯转发,由 --paths src 提供包路径。
诊断模式(ZM_PACK_DIAG=1 或 exe 旁无 diag.log 时)写 pack 诊断文件:
冻结环境 dxcam/comtypes 初始化失败会静默降级 veil,必须留痕。"""
import os
import sys
import time


def _diag_dir():
    # exe 旁边(frozen)或仓库 pack/(开发)
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _startup_diag():
    lines = [f"[{time.strftime('%H:%M:%S')}] frozen={getattr(sys, 'frozen', False)}",
             f"python={sys.version.split()[0]}"]
    try:
        import dxcam
        lines.append(f"dxcam import ok: {getattr(dxcam, '__version__', '?')}")
        c = dxcam.create(output_color="BGR")
        lines.append(f"dxcam.create: {c is not None}")
        if c is not None:
            lines.append(f"  region={getattr(c, 'region', None)} "
                         f"width={getattr(c, 'width', None)} "
                         f"height={getattr(c, 'height', None)}")
            import numpy as np
            f = c.grab()
            lines.append(f"  grab: {'None' if f is None else str(f.shape)}")
    except Exception:
        import traceback
        lines.append("dxcam FAILED:\n" + traceback.format_exc())
    try:
        import comtypes
        lines.append(f"comtypes: {comtypes.__version__}")
        import comtypes.client
        lines.append("comtypes.client import ok")
    except Exception:
        import traceback
        lines.append("comtypes FAILED:\n" + traceback.format_exc())
    p = os.path.join(_diag_dir(), "diag.log")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _exit_diag():
    try:
        from zcode_meter import glass_effect as g
        p = os.path.join(_diag_dir(), "diag.log")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"[exit] OK={g.OK} _CLEAN={'set' if g._CLEAN is not None else 'None'} "
                     f"_VERSION={g._VERSION} thread_alive="
                     f"{g._THREAD is not None and g._THREAD.is_alive()}\n")
    except Exception:
        pass


if os.environ.get("ZM_PACK_DIAG") or not os.path.exists(
        os.path.join(_diag_dir(), "diag.log")):
    _startup_diag()

import atexit
atexit.register(_exit_diag)

from zcode_meter.app import main

if __name__ == "__main__":
    main()
