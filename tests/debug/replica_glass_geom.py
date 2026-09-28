#!/usr/bin/env python3
"""定向复刻 test_stress.py 玻璃侧几何断言(循环稳定性/关段/满载收敛/预算段),
确认钉行+钳制对玻璃逐位无漂 —— 非回归套件本体。"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("ZM_NO_STATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
from PySide6.QtWidgets import QApplication

import zcode_meter.app as m

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


app = QApplication(sys.argv)
win = m.MeterWindow()
win._apply_skin("glass", persist=False)

# -- 开头 4 轮横竖循环(尺寸稳定 + 高度上限) --
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
check("横条高度=一行文字级(<=34 逻辑px)", hs[0][1] <= 34, str(hs[0]))
check("竖条宽度==BAR_V_W(100,定宽机制不漂)",
      vs[0][0] == m.MeterWindow.BAR_V_W == 100, str(vs[0]))

# -- 关段(空快照 → 满载)宽度收敛 --
win.bar_segments = {"h": ["today"], "v": ["today", "plan", "burn", "in"]}
win._build_bar()
win._apply_snapshot(win.snap)
hseg = win._bar_size(False)
check("横条关段:plan_ring/plan_cd/burn 未建(None)",
      win.plan_ring is None and win.plan_cd_lbl is None
      and win.burn_lbl is None and win.sep_plan is None,
      f"{win.plan_ring}/{win.plan_cd_lbl}/{win.burn_lbl}")
check("横条关段:速度+今日仍显",
      win.tps_lbl is not None and win.today_lbl is not None, "")
_saved = win.snap
win.snap = m.Snapshot(
    state="idle", model="GLM-5.3", title="t", tps_exact=137.9,
    today_tokens=1_200_000_000, today_cost_cny=2489.0,
    today_by_source=[("ZCode", 1_200_000_000)], plan_remaining_pct=87.0,
    burn_tokens_per_hour=31_300_000, burn_avg_tokens_per_hour=129_700_000,
    session_in=448_000_000, session_out=624_000,
    recent_speeds=[20, 35, 28, 44, 30, 52, 38],
    speed_by_model=[("bigmodel-api", "GLM-5.3", 56.8, 0)])
win._plan_pct = 87.0
win._apply_snapshot(win.snap)
hseg_full_data = win._bar_size(False)
win.bar_segments = {"h": list(m.BAR_SEGMENTS_H), "v": list(m.BAR_SEGMENTS_V)}
win._build_bar()
win._apply_snapshot(win.snap)
hfull_data = win._bar_size(False)
check("横条关段:满载数据下宽度收敛(<全开宽)",
      hseg_full_data[0] < hfull_data[0],
      f"seg {hseg_full_data} vs full {hfull_data}")
win.snap = _saved
win.bar_segments = {"h": ["today"], "v": ["today", "plan", "burn", "in"]}
win._build_bar()
win._apply_snapshot(win.snap)
check("横条关段:尺寸稳定(重建两次同宽)",
      win._bar_size(False) == hseg, f"{win._bar_size(False)} vs {hseg}")

# -- 预算段注入 4 轮(高度仍<=34 + 尺寸稳定) --
win.bar_segments = {"h": list(m.BAR_SEGMENTS_H), "v": list(m.BAR_SEGMENTS_V)}
win._plan_pct = 42.0
win._plan_left_tok = 2_100_000_000.0
win.snap.burn_tokens_per_hour = 12345.0
win.snap.session_in, win.snap.session_out = 1_234_567, 88
sizes2 = []
for i in range(4):
    win._build_bar(); win._apply_snapshot(win.snap); win._refit_dock()
    sizes2.append(("h", win._bar_size(False)))
    check(f"预算段循环{i} 横条:套餐段/燃速段可见且文案正确",
          win.plan_lbl.isVisible() and win.burn_lbl.isVisible()
          and win.plan_ring.isVisible() and win.sep_plan.isVisible()
          and win.sep_burn.isVisible()
          and win.plan_lbl.text() == "42% ~2.1B"
          and win.burn_lbl.text() == "燃速 12.3K/h"
          and not win.plan_cd_lbl.isVisible())
    win._build_bar(vertical=True); win._apply_snapshot(win.snap)
    sizes2.append(("v", win._bar_size(True)))
    win._unset_dock()
hs2 = [s for k, s in sizes2 if k == "h"]
vs2 = [s for k, s in sizes2 if k == "v"]
check("预算段横条尺寸稳定", all(x == hs2[0] for x in hs2), str(hs2))
check("预算段竖条尺寸稳定", all(x == vs2[0] for x in vs2), str(vs2))
check("预算段横条高度仍<=34", hs2[0][1] <= 34, str(hs2[0]))
check("预算段竖条宽度仍==116(定宽不随数据漂)",
      vs2[0][0] == m.MeterWindow.BAR_V_W, str(vs2[0]))

win.close()
if FAILED:
    print(f"\nFAILED: {FAILED}")
    sys.exit(1)
print("\nGLASS GEOM REPLICA ALL PASS")
