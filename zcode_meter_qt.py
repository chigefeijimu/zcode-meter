#!/usr/bin/env python3
"""zcode-meter Qt 版 —— PySide6 实现。

拖动用 Qt.startSystemMove() 让 Windows 合成器原生接管(与拖普通窗口同路径,
天然支持 Aero Snap),从根上消除 tkinter 版的迟滞/吞点击/互操作崩溃。
数据层复用 data_engine.py(tk 版同源)。

用法:
  pythonw zcode_meter_qt.py          正常启动
  python zcode_meter_qt.py --verify  自检:渲染/贴边判定,打印报告后退出
"""
from __future__ import annotations

import ctypes
import queue
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QVBoxLayout, QWidget,
)

from data_engine import DataEngine, Snapshot

C_BG, C_BORDER = "#16171c", "#2c2f3a"
C_FG, C_DIM, C_ACCENT, C_WARN = "#e8eaf0", "#8b8f9c", "#5ad6a0", "#e8c268"
C_MONO = "Consolas"

QSS = f"""
QWidget#root {{ background: {C_BG}; border: 1px solid {C_BORDER}; border-radius: 10px; }}
QLabel {{ color: {C_FG}; background: transparent; border: none; }}
QLabel#dim   {{ color: {C_DIM}; }}
QLabel#accent {{ color: {C_ACCENT}; }}
QLabel#warn  {{ color: {C_WARN}; }}
QLabel#faint {{ color: #565a66; }}
QFrame#sep {{ background: {C_BORDER}; border: none; max-height: 1px; }}
"""

# 胶囊条样式:小圆角 + 细边,内边距由布局控制
QSS_BAR = QSS.replace("border-radius: 10px", "border-radius: 7px")


class MeterWindow(QWidget):
    CARD_W, CARD_H = 250, 295          # 逻辑像素(DIP),Qt 自动做 DPI 换算
    BAR_H, BAR_V = 24, 38
    EDGE_NEAR = 30

    def __init__(self):
        super().__init__()
        self.q: "queue.Queue[Snapshot]" = queue.Queue(maxsize=20)
        self.eng = DataEngine(self.q)
        self.dock: str | None = None
        self.snap = Snapshot()
        self._breath = 0.0
        self._verify_done = False

        self.setWindowTitle("zcode-meter")
        self.setObjectName("root")
        self.setStyleSheet(QSS)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)

        self._build_card()
        self.adjustSize()
        sg = QGuiApplication.primaryScreen().availableGeometry()
        self.move(sg.right() - self.CARD_W - 24, sg.top() + 90)
        self.show()

        # 窗口 map 后做 DWM 润色 + 免激活
        QTimer.singleShot(80, self._win_polish)

        self._poll_timer = QTimer(self, interval=200, timeout=self._poll_queue)
        self._poll_timer.start()
        self._breath_timer = QTimer(self, interval=60, timeout=self._tick_breath)
        self._breath_timer.start()
        self.eng.start()

    # ---- Win32 润色(唯一保留的互操作,均为一次性安全调用) ----
    def _win_polish(self):
        try:
            user32 = ctypes.windll.user32
            dwmapi = ctypes.windll.dwmapi
            hwnd = int(self.winId())
            # WS_EX_NOACTIVATE:点击不抢焦点
            style_ex = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, style_ex | 0x08000000)
            # 去掉 WS_THICKFRAME|WS_MAXIMIZEBOX → 禁用 Aero Snap:
            # 否则拖到边缘时 Windows 把窗口贴靠成半屏,与"变条"逻辑打架
            style = user32.GetWindowLongW(hwnd, -16)
            user32.SetWindowLongW(hwnd, -16, style & ~(0x00040000 | 0x00010000))
            v = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(v), 4)     # dark
            v2 = ctypes.c_int(2)
            dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(v2), 4)    # Win11 round
        except Exception:
            pass

    # ---- 拖动:系统原生接管 ----
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.dock:
                self._detach_to_pointer()
            self.windowHandle().startSystemMove()      # Windows 合成器接管,原生丝滑
        elif event.button() == Qt.RightButton:
            self._popup_menu(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            QTimer.singleShot(90, self._settle)

    def _detach_to_pointer(self):
        pos = QCursor.pos()
        w, h = self.CARD_W, self.CARD_H
        sg = self.screen().availableGeometry()
        x = max(min(pos.x() - w // 2, sg.right() - w - 2), sg.left() + 2)
        y = max(min(pos.y() - 12, sg.bottom() - h - 2), sg.top() + 2)
        self.dock = None
        self._build_card()
        self.setStyleSheet(QSS)
        self.setGeometry(x, y, w, h)
        self._apply_snapshot(self.snap)

    # ---- 贴边判定(全 Qt 逻辑坐标,无 DPI 手算) ----
    def _settle(self):
        g = self.geometry()
        sg = self.screen().availableGeometry()
        near = self.EDGE_NEAR
        want = None
        if abs(g.top() - sg.top()) < near: want = "top"
        elif abs(g.bottom() - sg.bottom()) < near: want = "bottom"
        elif abs(g.left() - sg.left()) < near: want = "left"
        elif abs(g.right() - sg.right()) < near: want = "right"
        if want and want != self.dock:
            self._set_dock(want)
        elif not want and self.dock:
            self._unset_dock()

    def _set_dock(self, side: str):
        """贴边成胶囊条:宽高按各 label 的 sizeHint 聚合计算,恰好包住文字。
        注意 QWidget 顶层在 QSS border 下 sizeHint() 返回废值(16x2),
        必须手动聚合子控件尺寸,且先填文字再量。"""
        self.dock = side
        sg = self.screen().availableGeometry()
        g = self.geometry()
        vertical = side in ("left", "right")
        self._build_bar(vertical=vertical)
        self.setStyleSheet(QSS_BAR)
        self._apply_snapshot(self.snap)          # 先填文字
        w, h = self._bar_size(vertical)
        if vertical:
            cy = g.center().y()
            y = max(min(cy - h // 2, sg.bottom() - h - 2), sg.top() + 2)
            x = sg.left() if side == "left" else sg.right() - w + 1
        else:
            cx = g.center().x()
            x = max(min(cx - w // 2, sg.right() - w - 2), sg.left() + 2)
            y = sg.top() if side == "top" else sg.bottom() - h + 1
        self.setGeometry(x, y, w, h)     # 不锁死:_refit_dock 周期校验,自愈任何几何漂移

    def _mk_sep(self, vertical: bool) -> QFrame:
        line = QFrame()
        line.setStyleSheet(f"background: {C_BORDER}; border: none;")
        if vertical:
            line.setFixedSize(28, 1)
        else:
            line.setFixedSize(1, 14)
        return line

    def _bar_size(self, vertical: bool) -> tuple[int, int]:
        """遍历条布局内全部控件(label+分隔线)聚合尺寸:
        margins(8,1,8,1) + spacing 6 + 边框 2。"""
        lay = self.layout()
        if lay is None:
            return 60, 20
        sp = 6
        pad = 3          # 每控件安全余量:中文在 Consolas 回退渲染时 sizeHint 会低估
        if vertical:
            max_w, total_h = 0, 2 + 2
            for i in range(lay.count()):
                w = lay.itemAt(i).widget()
                if w is None:
                    continue
                hs = w.sizeHint()
                max_w = max(max_w, hs.width())
                total_h += hs.height() + pad + sp
            return max_w + 16 + 2 + pad, max(total_h - sp, 10)
        total_w, max_h = 16 + 2, 0
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is None:
                continue
            hs = w.sizeHint()
            max_h = max(max_h, hs.height() + pad)
            total_w += hs.width() + pad + sp
        return max(total_w - sp, 10), max_h + 2 + 2

    def _unset_dock(self):
        sg = self.screen().availableGeometry()
        g = self.geometry()
        self.dock = None
        self._build_card()
        self.setStyleSheet(QSS)
        x = max(min(g.left(), sg.right() - self.CARD_W - 8), sg.left() + 8)
        y = max(min(g.top(), sg.bottom() - self.CARD_H - 8), sg.top() + 8)
        self.setGeometry(x, y, self.CARD_W, self.CARD_H)
        self._apply_snapshot(self.snap)

    # ---- 布局 ----
    def _clear(self):
        lay = self.layout()
        if lay is not None:
            QWidget().setLayout(lay)      # 断开并删除旧布局

    def _mk_lbl(self, text="", cls="dim", font=None, size=None):
        lb = QLabel(text)
        lb.setObjectName(cls if cls != "normal" else "")
        if font:
            lb.setFont(QFont(font, size or 9))
        return lb

    def _build_card(self):
        self._clear()
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(4)

        head = QHBoxLayout()
        self.dot = self._mk_lbl("●", "dim", "Segoe UI", 9)
        self.state_lbl = self._mk_lbl("空闲", "dim", "Microsoft YaHei UI", 9)
        self.title_lbl = self._mk_lbl("当前会话", "dim", "Microsoft YaHei UI", 8)
        head.addWidget(self.dot)
        head.addWidget(self.state_lbl)
        head.addStretch(1)
        head.addWidget(self.title_lbl)
        root.addLayout(head)

        big = QHBoxLayout()
        self.tps_lbl = self._mk_lbl("-- tok/s", "accent", C_MONO, 20)
        self.tps_lbl.setFont(QFont(C_MONO, 20, QFont.Bold))
        self.est_lbl = self._mk_lbl("", "dim", "Microsoft YaHei UI", 8)
        self.elapsed_lbl = self._mk_lbl("", "warn", C_MONO, 9)
        big.addWidget(self.tps_lbl)
        big.addWidget(self.est_lbl)
        big.addStretch(1)
        big.addWidget(self.elapsed_lbl)
        root.addLayout(big)

        sep = QFrame(); sep.setObjectName("sep")
        root.addWidget(sep)

        grid = QHBoxLayout()
        left = QVBoxLayout(); right = QVBoxLayout()
        left.setSpacing(2); right.setSpacing(2)
        self.in_lbl = self._mk_lbl("--", "", C_MONO, 9)
        left.addWidget(self._mk_lbl("输入", "dim", "Microsoft YaHei UI", 8))
        left.addWidget(self.in_lbl)
        self.cache_lbl = self._mk_lbl("--", "", C_MONO, 9)
        right.addWidget(self._mk_lbl("缓存", "dim", "Microsoft YaHei UI", 8))
        right.addWidget(self.cache_lbl)
        self.out_lbl = self._mk_lbl("--", "", C_MONO, 9)
        left.addWidget(self._mk_lbl("输出", "dim", "Microsoft YaHei UI", 8))
        left.addWidget(self.out_lbl)
        self.rate_lbl = self._mk_lbl("--", "", C_MONO, 9)
        right.addWidget(self._mk_lbl("命中率", "dim", "Microsoft YaHai UI".replace("Yahai", "YaHei"), 8))
        right.addWidget(self.rate_lbl)
        self.avg_lbl = self._mk_lbl("--", "", C_MONO, 9)
        left.addWidget(self._mk_lbl("平均速度", "dim", "Microsoft YaHei UI", 8))
        left.addWidget(self.avg_lbl)
        grid.addLayout(left, 1)
        grid.addLayout(right, 1)
        root.addLayout(grid)

        self.today_lbl = self._mk_lbl("--", "", C_MONO, 9)
        left.addWidget(self._mk_lbl("今日用量", "dim", "Microsoft YaHei UI", 8))
        left.addWidget(self.today_lbl)
        self.ttft_lbl = self._mk_lbl("--", "", C_MONO, 9)
        left.addWidget(self._mk_lbl("首字等待", "dim", "Microsoft YaHei UI", 8))
        left.addWidget(self.ttft_lbl)
        self.dur_lbl = self._mk_lbl("--", "", C_MONO, 9)
        right.addWidget(self._mk_lbl("整体耗时", "dim", "Microsoft YaHei UI", 8))
        right.addWidget(self.dur_lbl)

        self.model_lbl = self._mk_lbl("", "faint", "Microsoft YaHei UI", 8)
        root.addWidget(self.model_lbl)

    def _build_bar(self, vertical: bool = False):
        self._clear()
        root = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        root.setContentsMargins(8, 1, 8, 1)
        root.setSpacing(6)
        self.dot = self._mk_lbl("●", "dim", "Segoe UI", 8)
        root.addWidget(self.dot)
        if not vertical:
            # 分组:速率 | 延迟 | 用量 | 计时,组间细竖线分隔
            self.tps_lbl = self._mk_lbl("-- tok/s", "accent", C_MONO, 9)
            root.addWidget(self.tps_lbl)
            root.addWidget(self._mk_sep(False))
            self.avg_lbl = self._mk_lbl("", "dim", C_MONO, 9)
            root.addWidget(self.avg_lbl)
            root.addWidget(self._mk_sep(False))
            self.ttft_lbl = self._mk_lbl("", "dim", C_MONO, 9)
            root.addWidget(self.ttft_lbl)
            self.dur_lbl = self._mk_lbl("", "dim", C_MONO, 9)
            root.addWidget(self.dur_lbl)
            root.addWidget(self._mk_sep(False))
            self.in_lbl = self._mk_lbl("", "dim", C_MONO, 9)
            root.addWidget(self.in_lbl)
            self.out_lbl = self._mk_lbl("", "dim", C_MONO, 9)
            root.addWidget(self.out_lbl)
            self.rate_lbl = self._mk_lbl("", "dim", C_MONO, 9)
            root.addWidget(self.rate_lbl)
            root.addWidget(self._mk_sep(False))
            self.today_lbl = self._mk_lbl("", "dim", C_MONO, 9)
            root.addWidget(self.today_lbl)
            root.addWidget(self._mk_sep(False))
            self.elapsed_lbl = self._mk_lbl("", "warn", C_MONO, 9)
            root.addWidget(self.elapsed_lbl)
        else:
            self.tps_lbl = self._mk_lbl("--", "accent", C_MONO, 10)
            self.tps_lbl.setFont(QFont(C_MONO, 10, QFont.Bold))
            self.tps_lbl.setAlignment(Qt.AlignCenter)
            root.addWidget(self.tps_lbl)
            root.addWidget(self._mk_lbl("tok/s", "dim", "Microsoft YaHei UI", 8))
            root.addWidget(self._mk_sep(True))
            self.avg_lbl = self._mk_lbl("--", "", C_MONO, 9)
            self.avg_lbl.setAlignment(Qt.AlignCenter)
            root.addWidget(self.avg_lbl)
            root.addWidget(self._mk_lbl("avg", "dim", "Microsoft YaHei UI", 8))
            root.addWidget(self._mk_sep(True))
            self.in_lbl = self._mk_lbl("--", "", C_MONO, 9)
            self.in_lbl.setAlignment(Qt.AlignCenter)
            root.addWidget(self.in_lbl)
            self.out_lbl = self._mk_lbl("--", "", C_MONO, 9)
            self.out_lbl.setAlignment(Qt.AlignCenter)
            root.addWidget(self.out_lbl)
            self.today_lbl = self._mk_lbl("--", "", C_MONO, 9)
            self.today_lbl.setAlignment(Qt.AlignCenter)
            root.addWidget(self.today_lbl)
            self.rate_lbl = self._mk_lbl("", "dim", "Microsoft YaHei UI", 8)
            self.rate_lbl.setAlignment(Qt.AlignCenter)
            root.addWidget(self.rate_lbl)
            root.addStretch(1)
            # 竖条不显示 elapsed/ttft/dur:显式置 None,否则保留已销毁旧对象的悬空引用
            self.elapsed_lbl = self.ttft_lbl = self.dur_lbl = None
        self.state_lbl = self.model_lbl = self.est_lbl = self.cache_lbl = None
        self.title_lbl = None

    # ---- 菜单 ----
    def _popup_menu(self, pos):
        m = QMenu(self)
        m.setStyleSheet(f"QMenu {{ background: #1e2027; color: {C_FG}; border: 1px solid {C_BORDER}; }}"
                        "QMenu::item { padding: 4px 18px; }"
                        "QMenu::item:selected { background: #2c2f3a; }")
        for label, fn in (("贴到顶部", lambda: self._set_dock("top")),
                          ("贴到底部", lambda: self._set_dock("bottom")),
                          ("贴到左侧", lambda: self._set_dock("left")),
                          ("贴到右侧", lambda: self._set_dock("right")),
                          ("恢复卡片", self._unset_dock)):
            m.addAction(label, fn)
        m.addSeparator()
        m.addAction("退出", QApplication.quit)
        m.exec(pos)

    # ---- 数据渲染 ----
    def _poll_queue(self):
        try:
            while True:
                self.snap = self.q.get_nowait()
        except queue.Empty:
            pass
        self._apply_snapshot(self.snap)

    @staticmethod
    def _fmt_k(n: int) -> str:
        if n < 1000:
            return str(n)
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if n >= div:
                return f"{n / div:.1f}{suf}"
        return str(n)

    def _apply_snapshot(self, s: Snapshot):
        generating = s.state == "generating"
        self.dot.setText("●")
        self.dot.setStyleSheet(
            f"color: {C_ACCENT};" if generating else f"color: {C_DIM};")
        if self.state_lbl is not None:
            self.state_lbl.setText("生成中" if generating else "空闲")
        if self.title_lbl is not None:
            self.title_lbl.setText(s.title or "当前会话")
        tps = s.tps_est if (generating and s.tps_est) else s.tps_exact
        txt = f"{tps:.1f}" if tps else "--"
        unit = " tok/s" if self.dock in (None, "top", "bottom") else ""
        self.tps_lbl.setText(txt + unit)
        if self.est_lbl is not None:
            self.est_lbl.setText("~估算" if (generating and s.tps_est) else "")
        if self.elapsed_lbl is not None:
            self.elapsed_lbl.setText(f"{s.gen_elapsed:.0f}s" if generating else "")
        self.in_lbl.setText(f"入 {self._fmt_k(s.session_in)}")
        self.out_lbl.setText(f"出 {self._fmt_k(s.session_out)}")
        self.rate_lbl.setText((f"缓存 {s.cache_rate:.2f}%" if self.dock in (None, "top", "bottom")
                               else f"{s.cache_rate:.2f}%"))
        if self.cache_lbl is not None:
            self.cache_lbl.setText(self._fmt_k(s.session_cache))
        if self.avg_lbl is not None:
            if self.dock in ("top", "bottom"):
                self.avg_lbl.setText(f"均 {s.tps_avg:.1f} tok/s" if s.tps_avg else "")
            else:
                self.avg_lbl.setText((f"{s.tps_avg:.1f} tok/s" if s.tps_avg else "--"))
        if self.model_lbl is not None:
            rows = s.speed_by_model or []
            lines = []
            for i, (prov, model, tps, out_tok) in enumerate(rows[:4]):
                t = f"{tps:.1f}" if tps else "--"
                if i == 0:
                    lines.append(f"{prov}/{model} · 均 {t} tok/s")
                else:
                    lines.append(f"{prov}/{model} · {t} tok/s")
            self.model_lbl.setText("\n".join(lines))
        if self.today_lbl is not None:
            if self.dock in ("top", "bottom"):
                self.today_lbl.setText(f"今 {self._fmt_k(s.today_tokens)}")
            else:
                self.today_lbl.setText(self._fmt_k(s.today_tokens))
        if self.ttft_lbl is not None:
            if s.last_ttft is None:
                self.ttft_lbl.setText("--")
            elif self.dock in ("top", "bottom"):
                self.ttft_lbl.setText(f"首字 {s.last_ttft:.1f}s")
            else:
                self.ttft_lbl.setText(f"{s.last_ttft:.1f}s")
        if self.dur_lbl is not None:
            if s.last_duration is None:
                self.dur_lbl.setText("--")
            elif self.dock in ("top", "bottom"):
                self.dur_lbl.setText(f"总 {s.last_duration:.1f}s")
            else:
                self.dur_lbl.setText(f"{s.last_duration:.1f}s")
        self._refit_dock()

    def _refit_dock(self):
        """条模式下数据文字变长(如 空→'均 52.1 tok/s')时重算条宽,防截断;
        贴边侧锚定不动,另一轴保持中心。稳定期尺寸不变,零开销跳过。"""
        if not self.dock:
            return
        vertical = self.dock in ("left", "right")
        w, h = self._bar_size(vertical)
        if (w, h) == (self.width(), self.height()):
            return
        sg = self.screen().availableGeometry()
        g = self.geometry()
        if self.dock == "top":
            self.setGeometry(g.x(), sg.top(), w, h)
        elif self.dock == "bottom":
            self.setGeometry(g.x(), sg.bottom() - h + 1, w, h)
        elif self.dock == "left":
            self.setGeometry(sg.left(), g.y(), w, h)
        else:
            self.setGeometry(sg.right() - w + 1, g.y(), w, h)

    def _tick_breath(self):
        self._breath = (self._breath + 0.08) % 1.0
        if self.snap.state == "generating":
            a = 0.45 + 0.55 * abs(2 * self._breath - 1)
            g = int(0x60 + 0x40 * a)
            r_ = int(0x30 + 0x18 * a)
            self.dot.setStyleSheet(f"color: #{r_:02x}{g:02x}70;")

    # ---- 自检 ----
    def _verify(self):
        self._apply_snapshot(self.snap)
        n = sum(1 for c in self.findChildren(QLabel))
        print(f"window: {self.width()}x{self.height()} labels={n}")
        sg = self.screen().availableGeometry()
        self.setGeometry(sg.left() + 100, sg.bottom() - self.height() - 5,
                         self.width(), self.height())
        QTimer.singleShot(120, self._verify_dock)

    def _verify_dock(self):
        self._settle()
        print(f"dock after settle near bottom -> {self.dock}",
              "PASS" if self.dock == "bottom" else "FAIL")
        self.eng.stop()
        QApplication.quit()


def main():
    app = QApplication(sys.argv)
    win = MeterWindow()
    if "--verify" in sys.argv:
        QTimer.singleShot(1500, win._verify)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
