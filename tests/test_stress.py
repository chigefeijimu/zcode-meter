#!/usr/bin/env python3
"""布局切换压力测试:横条 <-> 竖条 <-> 卡片 多轮循环 + 动态 refit + 尺寸动态性。

历史上这里翻过的车:
- 竖条切换时 elapsed_lbl 持有已销毁旧对象(RuntimeError 崩溃)
- 固定尺寸 vs 动态尺寸(必须动态:分辨率/内容变化自愈)
"""
import os
import sys
from pathlib import Path

# 位置记忆隔离:直接运行本文件时也不改写用户真实 zm_state.json
os.environ.setdefault("ZM_NO_STATE", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtWidgets import QApplication  # noqa: E402

import zcode_meter_qt as m  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    app = QApplication(sys.argv)
    win = m.MeterWindow()
    win._apply_snapshot(win.snap)

    sizes = []
    for i in range(4):
        win._build_bar(); win._apply_snapshot(win.snap); win._refit_dock()
        sizes.append(("h", win._bar_size(False)))
        win._build_bar(vertical=True); win._apply_snapshot(win.snap)
        sizes.append(("v", win._bar_size(True)))
        win._unset_dock()
        check(f"循环{i} 卡片恢复 dock=None", win.dock is None)

    hs = [s for k, s in sizes if k == "h"]
    vs = [s for k, s in sizes if k == "v"]
    check("横条尺寸稳定(动态重排无漂移)", all(x == hs[0] for x in hs), str(hs))
    check("竖条尺寸稳定", all(x == vs[0] for x in vs), str(vs))
    check("横条高度=一行文字级(<=30 逻辑px)", hs[0][1] <= 30, str(hs[0]))
    check("竖条宽度<=90 逻辑px(tok/s大字撑宽)", vs[0][0] <= 90, str(vs[0]))

    # 动态 refit:卡片模式应 no-op 不崩
    win._refit_dock()
    check("卡片模式 refit no-op", win.dock is None)

    win.close()
    if FAILED:
        print(f"\nFAILED: {FAILED}")
        return 1
    print("\nLAYOUT STRESS TESTS ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
