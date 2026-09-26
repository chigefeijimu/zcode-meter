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

    # ---- v0.5.0:贴边条预算段(plan/burn)结构与显隐回归 ----
    # 结构:横条 plan+burn 都创建(带 _budget_sep);竖条只建紧凑 plan,
    # burn 显式置 None(置 None 纪律防另一形态循环摸已销毁 QLabel —— 文件头
    # 记载的 v0.4.0 同类 bug);_bar_form 与实际标签集合一一对应
    win._build_bar()
    check("横条构建:plan/burn 均非 None",
          win.plan_lbl is not None and win.burn_lbl is not None)
    check("横条构建:_bar_form='h'", win._bar_form == "h")
    win._build_bar(vertical=True)
    check("竖条构建:plan 非 None 且 burn 为 None",
          win.plan_lbl is not None and win.burn_lbl is None)
    check("竖条构建:_bar_form='v'", win._bar_form == "v")

    # 注入套餐剩余/燃速后再跑双形态循环:显隐切换直接影响 _bar_size(隐藏
    # 控件须被跳过),虚胖会让高度/宽度断言与尺寸稳定断言当场翻车
    win._plan_pct = 42.0
    win.snap.burn_tokens_per_hour = 12345.0
    sizes2 = []
    for i in range(4):
        win._build_bar(); win._apply_snapshot(win.snap); win._refit_dock()
        sizes2.append(("h", win._bar_size(False)))
        check(f"预算段循环{i} 横条:plan/burn 可见且文案正确",
              win.plan_lbl.isVisible() and win.burn_lbl.isVisible()
              and win.plan_lbl.text() == "套餐剩余 42%"
              and win.burn_lbl.text() == "燃速 12.3K/h"
              and win._budget_sep.isVisible())
        win._build_bar(vertical=True); win._apply_snapshot(win.snap)
        sizes2.append(("v", win._bar_size(True)))
        check(f"预算段循环{i} 竖条:紧凑 plan 可见",
              win.plan_lbl.isVisible() and win.plan_lbl.text() == "套 42%")
        win._unset_dock()
    hs2 = [s for k, s in sizes2 if k == "h"]
    vs2 = [s for k, s in sizes2 if k == "v"]
    check("预算段横条尺寸稳定", all(x == hs2[0] for x in hs2), str(hs2))
    check("预算段竖条尺寸稳定", all(x == vs2[0] for x in vs2), str(vs2))
    check("预算段横条高度仍<=30", hs2[0][1] <= 30, str(hs2[0]))
    check("预算段竖条宽度仍<=90", vs2[0][0] <= 90, str(vs2[0]))
    # 数据缺席:整段隐藏(含分隔线),不残留空占位
    win._plan_pct = None
    win.snap.burn_tokens_per_hour = 0.0
    win._build_bar(); win._apply_snapshot(win.snap)
    check("数据缺席:plan/burn/分隔线整段隐藏",
          not win.plan_lbl.isVisible() and not win.burn_lbl.isVisible()
          and not win._budget_sep.isVisible())

    win.close()
    if FAILED:
        print(f"\nFAILED: {FAILED}")
        return 1
    print("\nLAYOUT STRESS TESTS ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
