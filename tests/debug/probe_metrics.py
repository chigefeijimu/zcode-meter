#!/usr/bin/env python3
"""探针:固定高度 QLabel 的 sizeHint 行为 + Segoe Print 墨迹范围。"""
import sys
from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtGui import QFont, QFontMetrics

app = QApplication(sys.argv)

GLASS = ("Cascadia Code", "Consolas")
CHALK = ("Segoe Print", "Comic Sans MS", "Cascadia Code")

for size in (14, 12):
    for fams in (GLASS, CHALK):
        f = QFont()
        f.setFamilies(list(fams))
        f.setPixelSize(size)
        fm = QFontMetrics(f)
        print(f"size={size} {fams[0]:<14} height={fm.height()} "
              f"asc={fm.ascent()} desc={fm.descent()} lead={fm.leading()}")

print()
print("== ink extents (tightBoundingRect) ==")
strings = ["42.0", "t/s", "87%", "87% ~2.1B", "1h30m", "今 1.2M ≈¥12",
           "燃速 12.3K/h", "--"]
for size in (14, 12):
    for fams in (GLASS, CHALK):
        f = QFont()
        f.setFamilies(list(fams))
        f.setPixelSize(size)
        f.setWeight(QFont.Weight.Bold if size == 14 else QFont.Weight.DemiBold)
        fm = QFontMetrics(f)
        for s in strings:
            r = fm.tightBoundingRect(s)
            print(f"  {size}px {fams[0]:<14} {s!r:<20} ink_h={r.height()} "
                  f"top={-r.top()} bottom={r.bottom()}")

print()
print("== QLabel sizeHint vs setFixedHeight ==")
for fams in (GLASS, CHALK):
    lb = QLabel("42.0")
    f = QFont()
    f.setFamilies(list(fams))
    f.setPixelSize(14)
    lb.setFont(f)
    print(f"  {fams[0]:<14} hint={lb.sizeHint().height()} "
          f"minHint={lb.minimumSizeHint().height()}", end=" ")
    lb.setFixedHeight(16)
    print(f"-> after setFixedHeight(16): hint={lb.sizeHint().height()} "
          f"min={lb.minimumHeight()} max={lb.maximumHeight()}")
