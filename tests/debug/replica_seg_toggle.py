#!/usr/bin/env python3
"""定向复刻 test_stress.py『新皮肤下段开关』块(QAction 触发路径),
确认钉行不破坏 toggle 重建 —— 非回归套件本体。"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("ZM_NO_STATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
from PySide6.QtWidgets import QApplication, QMenu

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

win.snap = m.Snapshot(
    state="idle", model="GLM-5.3", title="t", tps_exact=137.9,
    global_tps=42.0,
    today_tokens=1_200_000_000, today_cost_cny=2489.0,
    today_by_source=[("ZCode", 1_200_000_000)], plan_remaining_pct=87.0,
    burn_tokens_per_hour=31_300_000, burn_avg_tokens_per_hour=129_700_000,
    session_in=448_000_000, session_out=624_000,
    recent_speeds=[20, 35, 28, 44, 30, 52, 38],
    speed_by_model=[("bigmodel-api", "GLM-5.3", 56.8, 0)])
win._plan_pct = 87.0
win._plan_left_tok = 2_100_000_000.0
win._plan_next_reset = time.time() * 1000 + 90 * 60_000
win.bar_segments = {"h": list(m.BAR_SEGMENTS_H), "v": list(m.BAR_SEGMENTS_V)}

win.dock = "top"
win._apply_skin("swiss", persist=False)
_seg_host = QMenu()
win._add_segment_menu(_seg_host)
_seg_menu = next(a for a in _seg_host.actions() if a.text() == "显示内容").menu()
_plan_act = next(a for a in _seg_menu.actions()
                 if a.text() == m.BAR_SEGMENT_LABELS_H["plan"])
_plan_act.setChecked(True)
_plan_act.trigger()
check("新皮肤下段开关:关 plan → plan_ring/sep_plan 未建,速度/今日仍显",
      win.plan_ring is None and win.sep_plan is None
      and win.tps_lbl is not None and win.today_lbl is not None,
      f"ring={win.plan_ring} segs={win.bar_segments.get('h')}")
_h = win._bar_size(False)[1]
check("新皮肤下段开关:关段后横高仍<=34", _h <= 34, str(_h))
win.bar_segments = {"h": list(m.BAR_SEGMENTS_H), "v": list(m.BAR_SEGMENTS_V)}
win._apply_skin("swiss", persist=False)

# chalk 全段横条再验一次(toggle 复位路径)
win._apply_skin("chalk", persist=False)
_h = win._bar_size(False)[1]
check("chalk 全段复位:横高<=34", _h <= 34, str(_h))

win.dock = None
win._apply_skin("glass", persist=False)
check("皮肤复位:回 glass 且卡形态 qss 与玻璃逐位(可逆入口)",
      win.skin_id == "glass" and win._bar_form is None
      and win._skin_qss() == m.QSS)
win.close()

if FAILED:
    print(f"\nFAILED: {FAILED}")
    sys.exit(1)
print("\nSEG TOGGLE REPLICA ALL PASS")
