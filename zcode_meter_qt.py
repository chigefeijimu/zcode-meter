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
import datetime as dt
import json
import os
import queue
import sys
import time

from PySide6.QtCore import QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QCursor, QColor, QFont, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QTabWidget,
    QVBoxLayout, QWidget,
)

from data_engine import DataEngine, Snapshot, app_dir

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

# 历史图表独立窗口样式(带系统边栏的普通窗口,深色配色与主窗一致)
QSS_HIST = f"""
QWidget#histRoot {{ background: {C_BG}; }}
QLabel {{ color: {C_FG}; background: transparent; border: none; }}
QLabel#dim {{ color: {C_DIM}; }}
QTabWidget::pane {{ border: 1px solid {C_BORDER}; }}
QTabBar::tab {{ background: #1e2027; color: {C_DIM}; padding: 6px 14px; }}
QTabBar::tab:selected {{ color: {C_FG}; border-bottom: 2px solid {C_ACCENT}; }}
QPushButton {{ background: #1e2027; color: {C_FG}; border: 1px solid {C_BORDER};
               border-radius: 6px; padding: 5px 16px; }}
QPushButton:hover {{ background: #2c2f3a; }}
QPushButton:pressed {{ background: {C_BORDER}; }}
"""

# 位置记忆状态文件:与 zm_*.log 同目录(frozen 时落 exe 旁)
STATE_PATH = os.path.join(app_dir(), "zm_state.json")


def _state_guard() -> bool:
    """状态读写守卫:--verify 自检或 ZM_NO_STATE=1(回归测试注入)时不读不写
    zm_state.json,防测试改写用户真实的位置记忆。"""
    return "--verify" in sys.argv or os.environ.get("ZM_NO_STATE") == "1"


def fmt_k(n: int) -> str:
    """token 数量级缩写:图表柱顶与卡片共用同一格式。"""
    if n < 1000:
        return str(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}{suf}"
    return str(n)


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
        self._history_win = None            # HistoryWindow 懒创建,复用同一实例

        self.setWindowTitle("zcode-meter")
        self.setObjectName("root")
        self.setStyleSheet(QSS)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)

        self._build_card()
        self.adjustSize()
        sg = QGuiApplication.primaryScreen().availableGeometry()
        self.move(sg.right() - self.CARD_W - 24, sg.top() + 90)
        self.show()
        # 先 show 再恢复:保存的是贴边形态时,_set_dock 里的 self.screen()
        # 只有窗口 map 后才反映真实所在屏(多显示器下不能锚错屏)。
        # show 到首帧绘制之间隔着 exec(),此处置零闪烁。
        self._restore_state()

        # 兜菜单退出/事件循环正常退出路径的位置保存
        QApplication.instance().aboutToQuit.connect(self._save_state)

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

    # ---- 位置记忆:退出保存 x/y/dock,启动恢复并夹回可视区 ----
    def _save_state(self):
        """只存 x/y/形态。宽高刻意不存:尺寸由 CARD_W/CARD_H 常量与
        _refit_dock 动态自愈(固定尺寸在分辨率/DPI 变化后脱节的教训)。"""
        if _state_guard():
            return
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"x": self.x(), "y": self.y(), "dock": self.dock}, f)
        except OSError:
            pass                    # 状态保存失败不影响运行

    @staticmethod
    def _load_state():
        """读状态文件:缺文件/坏 JSON/字段类型不对一律返回 None。"""
        if _state_guard():
            return None
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if (not isinstance(obj, dict)
                or not isinstance(obj.get("x"), int)
                or not isinstance(obj.get("y"), int)
                or obj.get("dock") not in (None, "top", "bottom", "left", "right")):
            return None
        return obj

    def _restore_state(self):
        """恢复上次位置/形态。分辨率变化/拔显示器后保存点可能整块飞出
        可视区:找与之交集面积最大的屏,夹进该屏;完全无交集则放弃恢复
        (保守回主屏默认位,不硬平移)。"""
        st = self._load_state()
        if not st:
            return
        w, h = self.CARD_W, self.CARD_H
        g = QRect(st["x"], st["y"], w, h)
        best, best_area = None, 0
        for scr in QGuiApplication.screens():
            inter = scr.availableGeometry().intersected(g)
            if not inter.isEmpty() and inter.width() * inter.height() > best_area:
                best, best_area = scr.availableGeometry(), inter.width() * inter.height()
        if best is None:
            return
        g = QRect(max(min(st["x"], best.right() - w - 2), best.left() + 2),
                  max(min(st["y"], best.bottom() - h - 2), best.top() + 2),
                  w, h)
        if st["dock"]:
            self.move(g.topLeft())
            self._set_dock(st["dock"])     # 复用贴边夹取(锚定边+居中轴)
            # 多屏保险:_set_dock 依赖的 self.screen() 在跨屏移动后可能滞后,
            # 以恢复时选中的目标屏为准再夹一次
            gg = self.geometry()
            self.move(max(min(gg.x(), best.right() - gg.width() - 2), best.left() + 2),
                      max(min(gg.y(), best.bottom() - gg.height() - 2), best.top() + 2))
        else:
            self.setGeometry(g)

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
        self._save_state()               # 拖离贴边也是形态变化,同样即时落盘

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
        self._save_state()               # 形态变化即时落盘,兜强杀/崩溃路径

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
        self._save_state()               # 形态变化即时落盘,兜强杀/崩溃路径

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
        self._add_session_menu(m)
        m.addAction("历史用量图表", self._open_history)
        m.addSeparator()
        m.addAction("退出", QApplication.quit)
        m.exec(pos)

    def _add_session_menu(self, m: QMenu):
        """『会话』子菜单:列出最近会话供手动固定(📌),或恢复自动跟随。
        打勾只反映菜单打开瞬间的状态,固定后由下一次快照推送刷新 📌。"""
        sm = m.addMenu("会话")
        auto = sm.addAction("自动跟随(最近活跃)")
        auto.setCheckable(True)
        auto.setChecked(self.eng.manual_session is None)
        auto.triggered.connect(self.eng.clear_manual_session)
        sm.addSeparator()
        rows = self.eng.recent_sessions(8)
        if not rows:
            none = sm.addAction("(无最近会话)")
            none.setEnabled(False)
            return
        for sid, title in rows:
            act = sm.addAction(title or f"{sid[:18]}…")
            act.setCheckable(True)
            act.setChecked(self.eng.session_id == sid)
            act.triggered.connect(lambda checked=False, s=sid: self.eng.set_manual_session(s))

    # ---- 历史用量图表窗口 ----
    def _open_history(self):
        if self._history_win is None:
            self._history_win = HistoryWindow(self.eng)   # 构造即查询
        else:
            self._history_win.refresh()   # 再次打开也重新取数(打开与刷新同口径)
        self._history_win.show()
        self._history_win.raise_()
        self._history_win.activateWindow()

    # ---- 数据渲染 ----
    def _poll_queue(self):
        try:
            while True:
                self.snap = self.q.get_nowait()
        except queue.Empty:
            pass
        self._apply_snapshot(self.snap)

    def _apply_snapshot(self, s: Snapshot):
        generating = s.state == "generating"
        self.dot.setText("●")
        self.dot.setStyleSheet(
            f"color: {C_ACCENT};" if generating else f"color: {C_DIM};")
        if self.state_lbl is not None:
            self.state_lbl.setText("生成中" if generating else "空闲")
        if self.title_lbl is not None:
            self.title_lbl.setText(("📌 " if s.manual else "") + (s.title or "当前会话"))
        tps = s.tps_est if (generating and s.tps_est) else s.tps_exact
        txt = f"{tps:.1f}" if tps else "--"
        unit = " tok/s" if self.dock in (None, "top", "bottom") else ""
        self.tps_lbl.setText(txt + unit)
        if self.est_lbl is not None:
            self.est_lbl.setText("~估算" if (generating and s.tps_est) else "")
        if self.elapsed_lbl is not None:
            self.elapsed_lbl.setText(f"{s.gen_elapsed:.0f}s" if generating else "")
        self.in_lbl.setText(f"入 {fmt_k(s.session_in)}")
        self.out_lbl.setText(f"出 {fmt_k(s.session_out)}")
        self.rate_lbl.setText((f"缓存 {s.cache_rate:.2f}%" if self.dock in (None, "top", "bottom")
                               else f"{s.cache_rate:.2f}%"))
        if self.cache_lbl is not None:
            self.cache_lbl.setText(fmt_k(s.session_cache))
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
                self.today_lbl.setText(f"今 {fmt_k(s.today_tokens)}")
            else:
                self.today_lbl.setText(fmt_k(s.today_tokens))
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


class BarChart(QWidget):
    """纯 QPainter 条形图:竖柱(按天)/水平条(按会话)。
    刻意不引 matplotlib 等第三方库 —— 单文件 exe 的体积与启动速度。"""

    def __init__(self, horizontal: bool = False, parent=None):
        super().__init__(parent)
        self._horizontal = horizontal
        self._items: list[tuple[str, int]] = []
        self.setMinimumSize(360, 200)

    def set_items(self, items):
        self._items = [(str(a), int(b or 0)) for a, b in items]
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(C_BG))
        if not self._items:
            p.setPen(QColor(C_DIM))
            p.setFont(QFont("Microsoft YaHei UI", 9))
            p.drawText(self.rect(), Qt.AlignCenter, "无数据")
            return
        vmax = max(v for _, v in self._items) or 1
        if self._horizontal:
            self._paint_h(p, w, h, vmax)
        else:
            self._paint_v(p, w, h, vmax)

    def _paint_v(self, p: QPainter, w: int, h: int, vmax: int):
        """竖柱:柱顶缩写数值,柱底日期标签;标签过密时按步长抽稀。"""
        n = len(self._items)
        side, top, bot = 10, 26, 24
        chart_h = h - top - bot
        slot = (w - side * 2) / n
        bar_w = max(min(slot * 0.62, 46.0), 3.0)
        fm = QFontMetrics(QFont(C_MONO, 8))
        # 抽稀步长:保证相邻被绘制的标签互不重叠(标签宽+6px 间隔)
        stride = max(1, -(-n * (fm.horizontalAdvance("09-26") + 6) // max(w - 2 * side, 1)))
        p.setPen(QColor(C_BORDER))
        p.drawLine(side, top + chart_h, w - side, top + chart_h)   # 基线
        f_val, f_lbl = QFont(C_MONO, 8), QFont("Microsoft YaHei UI", 8)
        for i, (label, val) in enumerate(self._items):
            x = side + i * slot + (slot - bar_w) / 2
            bh = max(val / vmax * chart_h, 2) if val else 0
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C_ACCENT))
            p.drawRect(QRectF(x, top + chart_h - bh, bar_w, bh))
            if i % stride == 0:                    # 抽稀后仍从首根画起
                p.setPen(QColor(C_DIM))
                p.setFont(f_val)
                p.drawText(QRectF(x - slot / 2, top + chart_h - bh - 17,
                                  slot + bar_w, 15), Qt.AlignCenter, fmt_k(val))
                p.setFont(f_lbl)
                p.drawText(QRectF(x - slot / 2, h - bot + 3, slot + bar_w, bot - 5),
                           Qt.AlignCenter, label)

    def _paint_h(self, p: QPainter, w: int, h: int, vmax: int):
        """水平条:左侧标题(超长省略号),条末缩写数值;行高自适应。"""
        n = len(self._items)
        lbl_w = min(190, int(w * 0.32))
        x0, right = lbl_w + 8, w - 10
        row_h = min(30, max((h - 8) / max(n, 1), 14))
        f_lbl = QFont("Microsoft YaHei UI", 8)
        f_val = QFont(C_MONO, 8)
        fm = QFontMetrics(f_lbl)
        for i, (label, val) in enumerate(self._items):
            y = 4 + i * row_h
            cy = y + row_h / 2
            p.setPen(QColor(C_DIM))
            p.setFont(f_lbl)
            p.drawText(QRect(4, y, lbl_w, row_h), Qt.AlignVCenter | Qt.AlignRight,
                       fm.elidedText(label, Qt.ElideRight, lbl_w))
            bw = max(val / vmax * (right - x0), 2) if val else 0
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C_ACCENT))
            bh = min(row_h * 0.5, 12)
            p.drawRect(QRectF(x0, cy - bh / 2, bw, bh))
            p.setPen(QColor(C_FG))
            p.setFont(f_val)
            p.drawText(QRectF(x0 + bw + 6, y, right - x0 - bw - 6, row_h),
                       Qt.AlignVCenter | Qt.AlignLeft, fmt_k(val))


class HistoryWindow(QWidget):
    """历史用量图表独立窗口:普通 Qt.Window(带系统边栏与任务栏图标,与
    主窗 Qt.Tool 不同 —— 有意设计,便于长时间挂在旁边对照)。
    刷新在 UI 线程同步查询:当前 db 规模实测 ~0.1s 量级,可接受;数据量
    再涨需加时间窗或转 worker 线程。"""

    def __init__(self, eng: DataEngine):
        super().__init__(None)
        self.eng = eng
        self.setWindowTitle("zcode-meter · 历史用量")
        self.setObjectName("histRoot")
        self.setStyleSheet(QSS_HIST)
        self.setWindowFlag(Qt.Window, True)
        self.resize(780, 460)

        self.daily_chart = BarChart(horizontal=False)
        self.sess_chart = BarChart(horizontal=True)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.daily_chart, "按天(近30天)")
        self.tabs.addTab(self.sess_chart, "按会话(近20个)")

        head = QHBoxLayout()
        hint = QLabel("按天=全部来源 in+out(同今日口径);按会话=main_turn(与卡片一致)。"
                      "两图口径不同,合计对不上属预期。")
        hint.setObjectName("dim")
        btn = QPushButton("刷新")
        btn.clicked.connect(self.refresh)
        head.addWidget(hint, 1)
        head.addWidget(btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)
        root.addLayout(head)
        root.addWidget(self.tabs, 1)
        self.refresh()

    def refresh(self):
        daily = dict(self.eng.fetch_daily_usage(30))
        # 补齐 30 天连续序列:无用量日画 0 高柱位,避免空档误导读图
        days = [dt.date.today() - dt.timedelta(days=29 - i) for i in range(30)]
        self.daily_chart.set_items(
            [(d.strftime("%m-%d"), daily.get(d.isoformat(), 0)) for d in days])
        rows = self.eng.fetch_session_usage(20)
        self.sess_chart.set_items(
            [((title or sid[:12]) + f" ·{cnt}次", tok)
             for sid, title, tok, cnt in rows])


def main():
    app = QApplication(sys.argv)
    win = MeterWindow()
    if "--verify" in sys.argv:
        QTimer.singleShot(1500, win._verify)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
