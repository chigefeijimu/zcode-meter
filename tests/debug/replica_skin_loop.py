#!/usr/bin/env python3
"""定向复刻 test_stress.py 的 9×3 皮肤循环(含 F4 矩阵/卡防钳宽),
验证『皮肤循环 chalk 横条:高<=34』修复 —— 非回归套件本体。"""
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

F4_KEYS = ("dot", "tps_lbl", "tps_unit_lbl", "spark", "today_lbl",
           "today_cost_lbl", "today_src_lbl", "state_lbl", "model_name_lbl",
           "model_rows", "plan_section", "plan_ring", "plan_cap_lbl",
           "plan_tok_lbl", "plan_sub_lbl", "plan_left_k", "plan_left_v",
           "plan_lbl", "in_out_lbl", "rate_lbl", "timing_lbl", "burn_lbl",
           "avg_burn_lbl", "avg_lbl", "in_lbl", "out_lbl", "title_lbl",
           "est_lbl", "elapsed_lbl", "cache_lbl", "ttft_lbl", "dur_lbl",
           "plan_cd_lbl", "vbar_in_cap", "vbar_burn_cap", "sep_plan",
           "sep_burn", "sep_today", "vsep_plan", "vsep_burn",
           "vsep_today", "vsep_in")


def _f4_nonnone():
    return frozenset(k for k in F4_KEYS if getattr(win, k, None) is not None)


_f4_glass = {}
for _sid in m.skins.SKIN_IDS:
    for _form, _dock in (("card", None), ("h", "top"), ("v", "left")):
        win.dock = _dock
        win._apply_skin(_sid, persist=False)
        if _form == "card":
            check(f"皮肤循环 {_sid} 卡:_bar_form 归位+防钳宽(313x341)",
                  win._bar_form is None
                  and win.width() == m.MeterWindow.CARD_W
                  and win.height() == m.MeterWindow.CARD_H,
                  f"{win.width()}x{win.height()}")
        else:
            _vert = _form == "v"
            _w, _h = win._bar_size(_vert)
            check(f"皮肤循环 {_sid} {'竖' if _vert else '横'}条:"
                  + ("宽==BAR_V_W(100 定宽不随皮肤漂)" if _vert
                     else "高<=34(对一切皮肤同上限)"),
                  win._bar_form == _form
                  and ((_w == m.MeterWindow.BAR_V_W) if _vert
                       else (_h <= 34)),
                  f"{_w}x{_h}")
        _cur = _f4_nonnone()
        if _sid == "glass":
            _f4_glass[_form] = _cur
        else:
            check(f"皮肤循环 {_sid} {_form}:F4 非 None 集合与玻璃一致",
                  _cur == _f4_glass[_form], str(_cur ^ _f4_glass[_form]))
check("皮肤循环:9×3 全矩阵经 _apply_skin 重建(27 次,三形态基准齐全)",
      len(_f4_glass) == 3 and all(_f4_glass.values()))

win.close()
if FAILED:
    print(f"\nFAILED: {FAILED}")
    sys.exit(1)
print("\nSKIN LOOP REPLICA ALL PASS")
