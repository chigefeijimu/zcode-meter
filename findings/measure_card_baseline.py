#!/usr/bin/env python3
"""T-A/T-B 需求分析基线测量:卡片自然高度矩阵(test_stress 同款注入 +
adjustSize 实测法,先例见 app.py MeterWindow.CARD_H 注释 v0.5.1 条目)。

方法红线(评审 D1 修订):
1. 必须原生平台 —— offscreen 下 adjustSize 全部塌成 200x22 废值
   (顶层 QSS border 的 sizeHint 废值 + offscreen 不支持
   propagateSizeHints,两者都实测证伪过),QT_QPA_PLATFORM 显式清除;
2. 注入 → processEvents()(布局激活,缺这步 adjustSize 读到的是未激活
   垃圾,同样塌成 200x22)→ adjustSize();
3. 健全性下限:满载自然高度 <300 视为测量管线失效,打印 INVALID 并以
   退出码 1 中止(不给 CARD_H 重估喂垃圾数);
4. 模型名用真实形态(glm-5.3-flash):合成长名会把宽度顶出、产生离群高
   度(首轮 rows1=370 离群即此伪影,复测已不出现)。

只读测量,不改任何源码;输出 0~4 行模型 × 多源 × plan/burn 的自然 w×h。
本文件为 CARD_H 重估的钦定工具(v0.7 T-A/T-B spec),改动须同步 spec。"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("ZM_NO_STATE", "1")
os.environ.pop("QT_QPA_PLATFORM", None)   # 红线 1:禁 offscreen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from PySide6.QtWidgets import QApplication  # noqa: E402

import zcode_meter.app as m  # noqa: E402

SANITY_MIN_H = 300   # 红线 3:满载自然高度下限(基线满载 328~370)


def main() -> int:
    app = QApplication(sys.argv)
    win = m.MeterWindow()
    print(f"DPI scale = {win.devicePixelRatioF()}")

    def measure(rows: int, multi: bool, pb: bool):
        s = m.Snapshot()
        s.today_tokens = 1_234_567
        s.today_cost_cny = 12.34
        s.today_cost_partial = False
        s.last_ttft = 0.8
        s.last_duration = 12.4
        s.tps_avg = 52.1
        s.session_in, s.session_cache, s.session_out = 900_000, 880_000, 123_456
        s.cache_rate = 97.8
        # 红线 4:真实形态模型名(长度对齐 glm-5.3-flash)
        s.speed_by_model = [
            (f"bigmodel", f"glm-5.3-flash", 12.3 + i, 1000 * (i + 1))
            for i in range(rows)]
        s.today_by_source = ([("ZCode", 1_234_567), ("Claude", 456_789)]
                             if multi else None)
        if pb:
            s.burn_tokens_per_hour = 12_345.0
            s.est_hours_left = 3.2
            win._plan_pct = 42.0
            win._plan_fetched_at = time.time() - 180
            win._plan_next_reset = time.time() * 1000 + 90 * 60_000
        else:
            win._plan_pct = None
            win._plan_fetched_at = None
            win._plan_next_reset = None
        win._build_card()
        win.snap = s
        win._apply_snapshot(s)
        app.processEvents()                 # 红线 2:布局激活
        tmn = win.layout().totalMinimumSize()
        win.adjustSize()
        return tmn.width(), tmn.height(), win.width(), win.height()

    print("rows multi pb | layoutMin | adjustSize")
    res = {}
    for rows in range(5):
        for multi in (0, 1):
            for pb in (0, 1):
                lw, lh, aw, ah = measure(rows, bool(multi), bool(pb))
                res[(rows, multi, pb)] = ah
                print(f"  {rows}    {multi}    {pb}  | {lw}x{lh} | {aw}x{ah}")
    win._plan_pct = None
    win._plan_fetched_at = None
    win._plan_next_reset = None
    win.close()

    max_h = max(res.values())
    print(f"\n满载(plan/burn on)各.rows 高度: "
          + " ".join(f"{r}:{max(res[(r, mu, pb)] for mu in (0, 1) for pb in (0, 1) if pb)}"
                     for r in range(5)))
    print(f"全矩阵 max_h={max_h}  当前 CARD_W/CARD_H={m.MeterWindow.CARD_W}/{m.MeterWindow.CARD_H}")
    if max_h < SANITY_MIN_H:
        print("INVALID: 满载自然高度 <300,测量管线失效(offscreen?漏 processEvents?),"
              "结果不得用于 CARD_H 重估")
        return 1
    print("VALID")
    return 0


if __name__ == "__main__":
    sys.exit(main())
