#!/usr/bin/env python3
"""探针:逐皮肤量横条高度与各件 sizeHint,定位 chalk 高>34 的来源。"""
import os
import sys
from pathlib import Path

os.environ.setdefault("ZM_NO_STATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QFontMetrics

import zcode_meter.app as m

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
import time as _t
win._plan_next_reset = _t.time() * 1000 + 90 * 60_000
win.bar_segments = {"h": list(m.BAR_SEGMENTS_H), "v": list(m.BAR_SEGMENTS_V)}

print("== font metrics (14px) ==")
for fams in (("Cascadia Code", "Consolas"),
             ("Segoe Print", "Comic Sans MS", "Cascadia Code"),
             ("Georgia", "Times New Roman")):
    f = m.mk_mono(14, QFont.Bold, families=fams)
    actual = f.families() if hasattr(f, "families") else f.family()
    fm = QFontMetrics(f)
    print(f"  families={fams} resolved={actual} "
          f"hint={fm.height()} ascent={fm.ascent()} descent={fm.descent()}")

print("== per-skin horizontal bar ==")
for sid in m.skins.SKIN_IDS:
    win.dock = "top"
    win._apply_skin(sid, persist=False)
    w, h = win._bar_size(False)
    flag = "  <-- OVER" if h > 34 else ""
    print(f"  {sid:<12} {w}x{h}{flag}")
    if h > 34:
        lay = win.layout()
        for i in range(lay.count()):
            wd = lay.itemAt(i).widget()
            if wd is None or wd.isHidden():
                continue
            hs = wd.sizeHint()
            fnt = wd.font()
            fam = fnt.family()
            txt = getattr(wd, "text", lambda: "")()
            print(f"      {type(wd).__name__:<18} fam={fam!r} "
                  f"px={fnt.pixelSize()} hint={hs.height()} "
                  f"maxH={wd.maximumHeight()} text={txt!r}")

win.close()
