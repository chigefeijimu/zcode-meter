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
import math
import os
import queue
import re
import sys
import time

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QCursor, QColor, QDoubleValidator, QFont, QFontMetrics, QGuiApplication,
    QPainter,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPushButton, QStyle, QSystemTrayIcon, QTabWidget, QToolTip, QVBoxLayout,
    QWidget,
)

from data_engine import (
    BudgetAlerts, DataEngine, QuotaMonitor, Snapshot, app_dir, dbg,
    format_age_zh, format_countdown_hm, load_config, save_config,
)

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

# 设置窗样式:复用 QSS_HIST 同一套深色色板,补 QDialog/QLineEdit(输入框与
# 按钮同底色系,主按钮用主题色实底突出);err 红字用于保存失败的状态行
QSS_SET = f"""
QDialog {{ background: {C_BG}; }}
QLabel {{ color: {C_FG}; background: transparent; border: none; }}
QLabel#dim {{ color: {C_DIM}; }}
QLabel#err {{ color: #e06c75; }}
QLineEdit {{ background: #1e2027; color: {C_FG}; border: 1px solid {C_BORDER};
             border-radius: 6px; padding: 5px 8px;
             selection-background-color: {C_ACCENT}; }}
QLineEdit:focus {{ border: 1px solid {C_ACCENT}; }}
QPushButton {{ background: #1e2027; color: {C_FG}; border: 1px solid {C_BORDER};
               border-radius: 6px; padding: 5px 16px; }}
QPushButton:hover {{ background: #2c2f3a; }}
QPushButton:pressed {{ background: {C_BORDER}; }}
QPushButton#primary {{ background: {C_ACCENT}; color: #10241c; border: none;
                       font-weight: 600; }}
QPushButton#primary:hover {{ background: #6fe2b3; }}
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


class TrayController:
    """托盘模式(v0.4.0):主窗可收起到托盘,托盘菜单=显示/隐藏+退出,
    单击托盘图标恢复主窗;预算告警经 tray.showMessage 气泡派发。
    isSystemTrayAvailable() 为 False(无托盘/远程会话)时 create() 返回
    None 整体跳过 —— 不崩、--verify 与 stress 回归不受影响。
    持有者只跨线程传普通数据(告警文本),Qt 调用全部留在 UI 线程。"""

    @staticmethod
    def create(win: "MeterWindow") -> "TrayController | None":
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        try:
            return TrayController(win)
        except Exception:                  # 图标资源等异常也不值得拖垮主窗
            return None

    def __init__(self, win: "MeterWindow"):
        self.win = win
        self.tray = QSystemTrayIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon),
            win)
        m = QMenu()
        m.setStyleSheet(f"QMenu {{ background: #1e2027; color: {C_FG};"
                        f" border: 1px solid {C_BORDER}; }}"
                        "QMenu::item { padding: 4px 18px; }"
                        "QMenu::item:selected { background: #2c2f3a; }")
        m.addAction("显示 / 隐藏", self.toggle)
        m.addSeparator()
        m.addAction("退出", QApplication.quit)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(self._activated)
        self.tray.show()
        self._menu = m                     # QMenu 无父对象,显式持有防 GC

    def toggle(self):
        w = self.win
        if w.isVisible():
            w.hide()
        else:
            w.showNormal(); w.raise_(); w.activateWindow()

    def _activated(self, reason):
        # 单击(Trigger)恢复;双击/上下文菜单交给系统默认行为
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.win.showNormal(); self.win.raise_(); self.win.activateWindow()

    def notify(self, title: str, text: str):
        try:
            self.tray.showMessage(title, text,
                                  QSystemTrayIcon.MessageIcon.Information, 5000)
        except Exception:
            pass          # 专注助手抑制等系统行为不视为错误(README 注明)


class SettingsDialog(QDialog):
    """设置窗(v0.5.0):右键菜单「设置」打开,三字段与 zm_config.json 一一
    对应。key 安全红线:输入框 EchoMode.Password,界面任何回显均为掩码;
    解析/报错/日志路径一律不拼 key 明文。
    保存 = 解析校验 → save_config 落盘 → accept();任一步失败只红字报错、
    不落盘不关窗,成功后由调用方(MeterWindow._apply_config)热生效。
    输入判定以 float(strip()) 为准:QDoubleValidator 只做输入反馈,空串→
    None 等分支不依赖 validator 状态;阈值分隔符兼容半角/全角逗号与空白
    (中文输入法高频形态)。"""

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("zcode-meter · 设置")
        self.setStyleSheet(QSS_SET)
        self.setModal(True)
        self.setMinimumWidth(430)
        self._cfg = dict(cfg)                 # 保存成功后在此暂存规范化结果

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(6)

        def caption(text: str) -> QLabel:
            lb = QLabel(text)
            lb.setObjectName("dim")
            return lb

        root.addWidget(caption("quota API Key(Coding Plan 套餐轨)"))
        self.key_edit = QLineEdit(str(cfg.get("quota_api_key") or ""))
        # 密码框:预填现值但恒以掩码显示,杜绝明文回显泄漏面
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        root.addWidget(self.key_edit)
        root.addWidget(caption("仅存本机 zm_config.json(已 .gitignore);留空 = 不启用套餐轨"))

        root.addWidget(caption("日预算(元,留空 = 不启用按量告警轨)"))
        self.budget_edit = QLineEdit("")
        dv = QDoubleValidator(0.0, 1e9, 2, self)
        dv.setNotation(QDoubleValidator.Notation.StandardNotation)
        self.budget_edit.setValidator(dv)     # 仅输入反馈,判定见 _parse_input
        if cfg.get("daily_budget_cny"):
            self.budget_edit.setText(f"{cfg['daily_budget_cny']:g}")
        root.addWidget(self.budget_edit)

        root.addWidget(caption("告警阈值(剩余百分比,逗号分隔,默认 20,10)"))
        self.alert_edit = QLineEdit(", ".join(f"{v:g}" for v in cfg.get("alert_pct") or []))
        root.addWidget(self.alert_edit)

        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("err")
        self.status_lbl.setWordWrap(True)
        root.addWidget(self.status_lbl)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.setObjectName("primary")
        save.clicked.connect(self._on_save)
        btns.addWidget(cancel)
        btns.addWidget(save)
        root.addLayout(btns)

    def _err(self, text: str):
        self.status_lbl.setText(text)

    def _parse_input(self) -> dict | None:
        """UI 输入 → 合法 cfg dict;非法返回 None(已写状态行红字)。
        数值判定一律 float(strip())、异常文本直接判非法;空串语义:
        key 空=不启用、预算空=None、阈值空=恢复默认 [20,10]。"""
        key = self.key_edit.text().strip()
        budget_txt = self.budget_edit.text().strip()
        budget = None
        if budget_txt:
            try:
                budget = float(budget_txt)
            except ValueError:
                self._err(f"日预算不是数字:{budget_txt}")
                return None
            if not (budget > 0) or math.isinf(budget):
                self._err("日预算须为正数(留空 = 不启用)")
                return None
        pcts = [20.0, 10.0]                   # 阈值留空 → 默认
        alert_txt = self.alert_edit.text().strip()
        if alert_txt:
            pcts = []
            # 全角逗号『，』与任意空白都当分隔符;validator 不参与判定
            for part in re.split(r"[,，、\s]+", alert_txt):
                if not part:
                    continue
                try:
                    v = float(part)
                except ValueError:
                    self._err(f"告警阈值不是数字:{part}")
                    return None
                if not (v > 0) or math.isinf(v):
                    self._err("告警阈值须为正数(剩余百分比)")
                    return None
                pcts.append(v)
            if not pcts:
                self._err("告警阈值不能全为分隔符(留空恢复默认 20,10)")
                return None
        return {"quota_api_key": key, "daily_budget_cny": budget, "alert_pct": pcts}

    def _on_save(self):
        cfg = self._parse_input()
        if cfg is None:
            return                            # 非法输入:红字报错,不落盘不关窗
        if not save_config(cfg):
            # 落盘失败两类:守卫命中必须显式点名(否则用户以为改了实际没改,
            # 丢的还是最敏感的 key);否则按只读目录等 OSError 语义提示
            if _state_guard():
                self._err("ZM_NO_STATE=1/--verify 隔离中,未落盘")
            else:
                self._err("保存失败:配置文件不可写(exe 目录只读?);未生效")
            return
        self._cfg = cfg
        self.accept()

    def result_config(self) -> dict:
        """保存成功(accepted)后取规范化 cfg;热生效用它,而非回读文件。"""
        return self._cfg


class MeterWindow(QWidget):
    # v0.4.0:新增 燃速/套餐剩余 两行 + 今日用量可能多源第二行,自然高度
    # 实测 314(单源)。v0.5.1 套餐剩余改两行文案(第二行重置倒计时),注入
    # 实测:0~2 行模型 → 328/328/342,3 行模型 356 —— CARD_H 336 会把 2 行
    # 模型(342)截断,提到 350(2 行可容、3 行起 356>350 仍截断,与旧 336
    # 的截断点同点,无回退)。CARD_H 须 ≥ 布局自然高度 ——
    # _unset_dock/_restore_state/_detach_to_pointer 用它 setGeometry,偏小会
    # 静默截断(ui-verify 只打印不校验,需人工目视)
    CARD_W, CARD_H = 250, 350          # 逻辑像素(DIP),Qt 自动做 DPI 换算
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

        # ---- v0.4.0:预算告警(双轨)与 quota 轮询 ----
        # 启动位置钉死(评审必改#1):QuotaMonitor 的类定义在 data_engine,
        # 但实例化与启动只发生在这里 —— DataEngine.__init__/run() 及一切测试
        # 路径永不触碰;_state_guard() 为真(--verify / ZM_NO_STATE)时同样
        # 不启动,否则用户在源码目录放了带 key 的 zm_config.json 时,ui/stress
        # 回归会发真实网络请求。这是双闸的第一闸,第二闸在 run_all.py(data
        # 组注入 ZM_NO_STATE=1)。
        cfg = load_config()
        self.daily_budget_cny = cfg["daily_budget_cny"]
        # key 内存基准(v0.5.0 设置窗):设置保存后的 monitor 对账必须与它
        # 比较 —— 严禁落盘后回读文件(恒等 → monitor 永不重启 → 残留旧账号
        # 套餐数据)。用完即弃,只在保存成功后更新。
        self._quota_key = cfg["quota_api_key"]
        self.alerts = BudgetAlerts(cfg["alert_pct"])
        self.quota_monitor = None
        if cfg["quota_api_key"] and not _state_guard():
            self.quota_monitor = QuotaMonitor(cfg["quota_api_key"])
            self.quota_monitor.start()
            # v0.5.1 事件驱动:引擎 completed 水位前进 → monitor 记活跃,
            # 节流器据此决定何时真发 quota 请求(未配 key 不接线,零开销)
            self.eng.on_activity = self.quota_monitor.notify_activity
        self._plan_pct: float | None = None   # quota 轨 5h 窗剩余%(UI 侧缓存)
        # v0.5.1:查询完成时刻(新鲜度『N分钟前』)与 nextResetTime(倒计时
        # 纯本地递减,不为它发请求);换号/清号时随三缓存一并清零
        self._plan_fetched_at: float | None = None
        self._plan_next_reset: float | None = None
        # 托盘:无托盘环境(远程会话等)整体跳过,不崩不影响 --verify/stress
        self.tray = TrayController.create(self)

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
    def _pointer_pos(self):
        """松手时的指针位置(封装成方法便于自检时 override)。"""
        return QCursor.pos()

    def _settle(self):
        """贴边判定按【鼠标触边】:指针怼到屏幕边缘即贴对应边,
        不再要求窗口本体侧边接近 —— 拖着窗口让鼠标碰一下屏边松手即可。"""
        sg = self.screen().availableGeometry()
        pos = self._pointer_pos()
        edge = 6                              # 指针距屏边的判定阈值(px)
        want = None
        if pos.y() <= sg.top() + edge: want = "top"
        elif pos.y() >= sg.bottom() - edge + 1: want = "bottom"
        elif pos.x() <= sg.left() + edge: want = "left"
        elif pos.x() >= sg.right() - edge + 1: want = "right"
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
            cy = sg.center().y()                 # 贴边自动沿边轴居中(用户指定)
            y = max(min(cy - h // 2, sg.bottom() - h - 2), sg.top() + 2)
            x = sg.left() if side == "left" else sg.right() - w + 1
        else:
            cx = sg.center().x()
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
        margins(8,1,8,1) + spacing 6 + 边框 2。
        isHidden() 的控件跳过(v0.5.0):预算段整段隐藏后仍按隐藏 label 计
        会让条宽虚胖、悬空一条分隔线;空文本 QLabel 本身仍占行高,也会撑破
        stress 的『横条高度≤30』断言 —— 所以数据缺席必须走 setVisible(False)
        而非 setText("")。"""
        lay = self.layout()
        if lay is None:
            return 60, 20
        sp = 6
        pad = 3          # 每控件安全余量:中文在 Consolas 回退渲染时 sizeHint 会低估
        if vertical:
            max_w, total_h = 0, 2 + 2
            for i in range(lay.count()):
                w = lay.itemAt(i).widget()
                if w is None or w.isHidden():
                    continue
                hs = w.sizeHint()
                max_w = max(max_w, hs.width())
                total_h += hs.height() + pad + sp
            return max_w + 16 + 2 + pad, max(total_h - sp, 10)
        total_w, max_h = 16 + 2, 0
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is None or w.isHidden():
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
        # 形态标记(v0.5.0):_apply_snapshot 的预算段渲染按它分支 —— 卡片=
        # 纯文本切换(现状),条形态=按数据显隐;每次重建后与实际标签集合
        # 一一对应(stress 尺寸稳定断言依赖此约定)
        self._bar_form = None
        self._budget_sep = None
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

        # 卡片专属两行(有数据才显示):燃速+耗尽预估 / 套餐剩余。
        # v0.5.0 起条形态也有预算段(横条 plan+burn、竖条紧凑 plan),由
        # _build_bar 各自创建 —— 尾部置 None 纪律只保留真正不创建的 label。
        self.burn_lbl = self._mk_lbl("", "dim", "Microsoft YaHei UI", 8)
        root.addWidget(self.burn_lbl)
        self.plan_lbl = self._mk_lbl("", "warn", "Microsoft YaHei UI", 8)
        root.addWidget(self.plan_lbl)

        self.model_lbl = self._mk_lbl("", "faint", "Microsoft YaHei UI", 8)
        root.addWidget(self.model_lbl)

    def _build_bar(self, vertical: bool = False):
        self._clear()
        root = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        root.setContentsMargins(8, 1, 8, 1)
        root.setSpacing(6)
        self._bar_form = "v" if vertical else "h"
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
            # 预算段(v0.5.0):『套餐剩余 N%』(warn 色,quota 轨有数据才
            # 可见)+『燃速 x/h』(dim 色)。数据缺席时 _apply_snapshot 把
            # label 与 _budget_sep 整段隐藏 → 布局回落到原样(today|线|计时),
            # _bar_size 跳过隐藏控件,条宽不虚胖
            self.plan_lbl = self._mk_lbl("", "warn", C_MONO, 9)
            root.addWidget(self.plan_lbl)
            self.burn_lbl = self._mk_lbl("", "dim", C_MONO, 9)
            root.addWidget(self.burn_lbl)
            self._budget_sep = self._mk_sep(False)
            root.addWidget(self._budget_sep)
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
            # 竖条预算段:空间受限只加紧凑套餐剩余(『套 N%』≈30px,竖条宽度
            # 上限 90px 的 stress 断言卡着),无分隔线;燃速段显式置 None
            self.plan_lbl = self._mk_lbl("", "warn", C_MONO, 9)
            self.plan_lbl.setAlignment(Qt.AlignCenter)
            root.addWidget(self.plan_lbl)
            self._budget_sep = None
            root.addStretch(1)
            # 竖条不显示 elapsed/ttft/dur:显式置 None,否则保留已销毁旧对象的悬空引用
            self.elapsed_lbl = self.ttft_lbl = self.dur_lbl = None
            self.burn_lbl = None
        # 横条与竖条共通:卡片专属 label 两种条形态都不创建,统一置 None
        # (只在一种形态置 None 会让另一形态的压力循环摸到已销毁 QLabel
        #  —— test_stress.py 的存在理由;burn/plan 已改由各形态自行创建)
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
        m.addAction("设置", self._open_settings)
        if self.tray is not None:          # 无托盘环境不提供收起,防"收起后找不回"
            m.addAction("收起到托盘", self.hide)
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
        try:
            if self._history_win is None:
                self._history_win = HistoryWindow(self.eng)   # 构造即查询
                # 首次打开:居中于主窗所在屏 —— 不指定位置时窗口可能落在不可预期处
                sg = self.screen().availableGeometry()
                hw = self._history_win
                hw.move(sg.center().x() - hw.width() // 2,
                        sg.center().y() - hw.height() // 2)
            else:
                self._history_win.refresh()   # 再次打开也重新取数(打开与刷新同口径)
            hw = self._history_win
            # 显示动作必须延迟到 QMenu.exec() 模态循环返回之后:
            # 在菜单 triggered 槽里直接 show,ShowWindow 会被菜单关闭的
            # 鼠标抓取时序吞掉 —— 窗口已创建(visible=False)但永不显示,
            # 症状即"点击没反应"(实测窗口枚举确认:存在/位置正常/不可见)
            QTimer.singleShot(0, lambda: (
                hw.showNormal(), hw.raise_(), hw.activateWindow()))
        except Exception:
            import traceback
            with open("zm_error.log", "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + traceback.format_exc())

    # ---- 设置窗(v0.5.0) ----
    def _open_settings(self):
        """右键「设置」:现读磁盘预填(允许用户手改 zm_config.json 后经界面
        接管),保存成功(accepted)才热生效。对话框必须延迟到菜单 exec() 的
        模态循环返回之后 —— 与 _open_history 同坑:triggered 槽内直接弹窗,
        会被菜单关闭的鼠标抓取时序吞掉,症状即『点设置没反应』。"""
        try:
            dlg = SettingsDialog(load_config(), self)

            def _exec_and_apply():
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    self._apply_config(dlg.result_config())

            QTimer.singleShot(0, _exec_and_apply)
        except Exception:
            import traceback
            with open("zm_error.log", "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + traceback.format_exc())

    def _apply_config(self, cfg: dict):
        """设置保存后的热生效(全部 UI 线程):先 save_config,失败即整体
        终止(引擎/告警/monitor 一律不动,内存态与磁盘不脱节)。正常路径下
        设置窗已落盘过一次,这里再写一次是幂等的原子替换 —— 换来的是本方法
        自含『未落盘不生效』不变式,不依赖调用方先save。成功后依次:
        ①同步引擎日预算(裸写先例=quota_hint,GIL 原子,1s 内 _poll_stats
        重算 est_hours_left)→ ②就地更新告警阈值(不重建 BudgetAlerts,
        zm_alerts.json 已触发状态保留)→ ③与 self._quota_key(内存基准)
        四分支对账:不动/启动(含挂活动回调)/停+清套餐缓存(三缓存 +
        v0.5.1 的 fetched_at/next_reset,并摘除活动回调)/换号停+清+立即
        按新 key 重启(回调重挂新实例)。"""
        if not save_config(cfg):
            return
        self.daily_budget_cny = cfg["daily_budget_cny"]
        self.eng.daily_budget_cny = cfg["daily_budget_cny"]
        self.alerts.thresholds = sorted(
            {float(t) for t in (cfg["alert_pct"] or []) if t > 0}, reverse=True)
        old, new = self._quota_key or "", cfg["quota_api_key"] or ""
        if old == new:
            pass                            # ①key 未变:monitor 不动
        elif not old and new:
            if not _state_guard():          # ②启用:仍过守卫闸(测试环境不发真请求)
                self.quota_monitor = QuotaMonitor(new)
                self.quota_monitor.start()
                self.eng.on_activity = self.quota_monitor.notify_activity
        else:
            # ③清号 / ④换号:停旧 monitor + 清三项套餐缓存(旧账号数据零残留:
            # 剩余% 显示、引擎 5h 块界锚点)+ v0.5.1 的新鲜度/倒计时缓存与
            # 活动回调 —— 回调指向旧实例,必须先摘除(旧 monitor 已停,残留
            # 回调无害但脏,且破坏零残留不变式)
            if self.quota_monitor is not None:
                self.quota_monitor.stop()
                self.quota_monitor = None
            self._plan_pct = None
            self._plan_fetched_at = None
            self._plan_next_reset = None
            self.snap.plan_remaining_pct = None
            self.eng.quota_hint = None
            self.eng.on_activity = None
            if new and not _state_guard():  # ④换号:立即按新 key 重启,不停在 None
                self.quota_monitor = QuotaMonitor(new)
                self.quota_monitor.start()
                # 换号必须重挂新实例的回调:漏挂则换号后只剩启动首查,
                # quota 永不因活动刷新(比现状更糟的翻车点)
                self.eng.on_activity = self.quota_monitor.notify_activity
        self._quota_key = new
        # dbg 只记预算/阈值/布尔,不记 key 明文(泄漏面专查项)
        dbg(f"config applied: budget={cfg['daily_budget_cny']} "
            f"alert={cfg['alert_pct']} monitor_on={self.quota_monitor is not None} "
            f"key_changed={old != new}")

    # ---- 数据渲染 ----
    def _poll_queue(self):
        try:
            while True:
                self.snap = self.q.get_nowait()
        except queue.Empty:
            pass
        self._update_quota()
        self._apply_snapshot(self.snap)
        self._check_alerts()

    def _update_quota(self):
        """quota 轨数据搬运(UI 线程):daemon 线程只产出普通 dict,这里取
        拷贝渲染并回写引擎 —— plan_remaining_pct 引擎只写 None、UI 回填
        (引擎从不读它,跨线程无竞态);nextResetTime 给计费块对齐块界用。
        v0.5.1:同时缓存 fetched_at(渲染『N分钟前』新鲜度)与 next_reset_ms
        (倒计时每 200ms 渲染 tick 本地重算,零 API 请求)。"""
        m = self.quota_monitor
        if m is None:
            return
        data = m.latest()
        if not data:
            return
        pct = data.get("remaining_pct")
        if pct is not None:
            self._plan_pct = pct
            self.snap.plan_remaining_pct = pct
        fa = data.get("fetched_at")
        if isinstance(fa, (int, float)) and not isinstance(fa, bool) and fa > 0:
            self._plan_fetched_at = float(fa)
        nrt = data.get("next_reset_ms")
        if isinstance(nrt, (int, float)) and not isinstance(nrt, bool) and nrt > 0:
            self._plan_next_reset = int(nrt)
            self.eng.quota_hint = int(nrt)

    def _check_alerts(self):
        """双轨预算告警,UI 线程评估(评审钉死:类与状态机在 data_engine,
        评估在这里):quota 轨=套餐 5h 窗剩余%;按量轨=(日预算-今日花费
        ZCode 口径)/日预算。命中经托盘气泡派发(无托盘环境静默降级);
        同级别同日只提醒一次由 BudgetAlerts 保证,消息不含任何 key 明文。"""
        today = dt.date.today().isoformat()
        if self._plan_pct is not None:
            lv = self.alerts.evaluate("quota", self._plan_pct, today)
            if lv is not None:
                self._notify(f"套餐 5h 窗剩余 {self._plan_pct:.0f}%(已过 {lv:g}% 阈值)")
        b = self.daily_budget_cny
        if b and self.snap.today_cost_cny is not None:
            remain = (b - self.snap.today_cost_cny) / b * 100.0
            lv = self.alerts.evaluate("budget", remain, today)
            if lv is not None:
                self._notify(f"今日已花 ≈¥{self.snap.today_cost_cny:.2f}(ZCode 口径),"
                             f"预算剩余 {remain:.0f}%")

    def _notify(self, text: str):
        dbg(f"alert: {text}")               # 文本只有百分比/金额,无 key 明文
        if self.tray is not None:
            self.tray.notify("zcode-meter 预算提醒", text)

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
            # 今日用量并入金额版:partial(存在未知模型,金额为下限)带 ≈。
            # 横条同 label 追加金额段不新增 label;竖条空间受限只保 token。
            cost = s.today_cost_cny or 0.0
            cost_txt = (f" · {'≈' if s.today_cost_partial else ''}¥{cost:.2f}"
                        if self.dock in (None, "top", "bottom") and cost else "")
            if self.dock in ("top", "bottom"):
                self.today_lbl.setText(f"今 {fmt_k(s.today_tokens)}{cost_txt}")
            elif self.dock in ("left", "right"):
                self.today_lbl.setText(fmt_k(s.today_tokens))
            else:
                txt = f"{fmt_k(s.today_tokens)}{cost_txt}"
                # 多源聚合(>1 源才显示,避免"只有 ZCode"的噪音行)
                srcs = s.today_by_source
                if srcs and len(srcs) > 1:
                    txt += "\n" + " · ".join(f"{n} {fmt_k(t)}" for n, t in srcs)
                self.today_lbl.setText(txt)
        # ---- 预算段(v0.5.0 起条形态也有):卡片=纯文本切换(现状不动);
        # 条形态=按数据显隐 —— 数据缺席整段 setVisible(False)(label+分隔线),
        # 隐藏控件被 _bar_size 跳过,条宽不虚胖;横条 burn 不带卡片 est 后缀
        # (一行放不下);竖条只保紧凑套餐剩余,文案压到 90px 宽度断言内。
        burn = s.burn_tokens_per_hour or 0.0
        if self._bar_form is None:
            if self.burn_lbl is not None:
                if burn > 0:
                    t = f"燃速 {fmt_k(int(burn))}/h"
                    if s.est_hours_left is not None:
                        t += (" · 预算已超支" if s.est_hours_left <= 0
                              else f" · 预算还可撑 {s.est_hours_left:.1f}h")
                    self.burn_lbl.setText(t)
                else:
                    self.burn_lbl.setText("")   # 无燃速(今日尚未活跃)不显示
            if self.plan_lbl is not None:
                if self._plan_pct is None:
                    self.plan_lbl.setText("")
                else:
                    # 两行:v0.5.1 需求点 4/3 —— 第一行带数据新鲜度(用户
                    # 知道百分比多新,静默期冻结可见),第二行重置倒计时
                    # 纯本地递减(缺 fetched_at/next_reset 时该段自然省略)
                    t = f"套餐剩余 {self._plan_pct:.0f}%"
                    age = format_age_zh(self._plan_fetched_at)
                    if age:
                        t += f" · {age}"
                    cd = format_countdown_hm(self._plan_next_reset)
                    if cd:
                        t += f"\n{cd} 后重置"
                    self.plan_lbl.setText(t)
        else:
            plan_on = self._plan_pct is not None
            burn_on = burn > 0
            if self.plan_lbl is not None:
                self.plan_lbl.setVisible(plan_on)
                if plan_on:
                    self.plan_lbl.setText(
                        f"套餐剩余 {self._plan_pct:.0f}%" if self._bar_form == "h"
                        else f"套 {self._plan_pct:.0f}%")
            if self.burn_lbl is not None:
                self.burn_lbl.setVisible(burn_on)
                if burn_on:
                    self.burn_lbl.setText(f"燃速 {fmt_k(int(burn))}/h")
            if self._budget_sep is not None:
                self._budget_sep.setVisible(plan_on or burn_on)
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
        # 沿边轴保持屏幕居中(与 _set_dock 的居中策略一致,防止重算把居中拉回松手点)
        if self.dock == "top":
            self.setGeometry(sg.center().x() - w // 2, sg.top(), w, h)
        elif self.dock == "bottom":
            self.setGeometry(sg.center().x() - w // 2, sg.bottom() - h + 1, w, h)
        elif self.dock == "left":
            self.setGeometry(sg.left(), sg.center().y() - h // 2, w, h)
        else:
            self.setGeometry(sg.right() - w + 1, sg.center().y() - h // 2, w, h)

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
        # 模拟鼠标触底边(真实 QCursor 不受测试控制,override _pointer_pos)
        sg = self.screen().availableGeometry()
        self._pointer_pos = lambda: QPoint(sg.center().x(), sg.bottom())
        QTimer.singleShot(120, self._verify_dock)

    def _verify_dock(self):
        self._settle()
        print(f"dock after pointer-at-bottom -> {self.dock}",
              "PASS" if self.dock == "bottom" else "FAIL")
        self.eng.stop()
        QApplication.quit()


class BarChart(QWidget):
    """纯 QPainter 条形图:horizontal=True 水平条(v0.5.2 起三页签统一水平,
    用户实测竖柱不便阅读;竖柱形态保留供未来切换)。
    水平条左侧标签超长省略,悬停 tooltip 显示全量 label+数值补偿;
    数值区预留防顶出窗口,extra(¥金额)拼在数值后,高亮下标条变警示色。
    刻意不引 matplotlib 等第三方库 —— 单文件 exe 的体积与启动速度。"""

    def __init__(self, horizontal: bool = True, label_angle: int = 0, parent=None):
        super().__init__(parent)
        self._horizontal = bool(horizontal)
        self._angle = max(0, min(int(label_angle), 90))
        self._items: list[tuple[str, int]] = []
        self._extra: list[str] = []        # 第二行数值文本(如 ¥ 金额),可空
        self._highlight = -1               # 高亮柱下标(计费块的当前块)
        self.setMinimumSize(360, 200)
        self.setMouseTracking(True)        # 悬停 tooltip 需要 mouseMove 事件

    def set_items(self, items):
        """items 兼容二元组 (label, value) 与三元组 (label, value, extra):
        extra 为第二行数值文本(按天页签的 ¥);按会话页签继续传二元组。"""
        self._items = [(str(t[0]), int(t[1] or 0)) for t in items]
        self._extra = [str(t[2]) if len(t) > 2 and t[2] else "" for t in items]
        self._highlight = -1
        self.update()

    def set_highlight(self, i: int):
        self._highlight = int(i)
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

    def _paint_h(self, p: QPainter, w: int, h: int, vmax: int):
        """水平条:左侧标题(超长省略号),条末缩写数值(+¥);行高自适应。
        条形最大宽度必须给数值区预留 —— 画满右缘会让数值矩形宽度为负,
        数值被顶出窗口外不可见(最长条正落在 vmax 上)。"""
        n = len(self._items)
        lbl_w = min(130, int(w * 0.24))
        x0, right = lbl_w + 8, w - 10
        val_w = 96                                     # 数值区预留(token+¥)
        bar_max = max(right - x0 - val_w - 6, 20)
        row_h = min(26, max((h - 8) / max(n, 1), 13))
        f_lbl = QFont("Microsoft YaHei UI", 8)
        f_val = QFont(C_MONO, 8)
        fm = QFontMetrics(f_lbl)
        fm_val = QFontMetrics(f_val)
        for i, (label, val) in enumerate(self._items):
            y = 4 + i * row_h
            cy = y + row_h / 2
            p.setPen(QColor(C_DIM))
            p.setFont(f_lbl)
            p.drawText(QRect(4, y, lbl_w, row_h), Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(label, Qt.ElideRight, lbl_w))
            bw = max(val / vmax * bar_max, 2) if val else 0
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C_WARN) if i == self._highlight else QColor(C_ACCENT))
            bh = min(row_h * 0.5, 12)
            p.drawRect(QRectF(x0, cy - bh / 2, bw, bh))
            p.setPen(QColor(C_FG))
            p.setFont(f_val)
            txt = fmt_k(val)
            extra = self._extra[i] if i < len(self._extra) else ""
            if extra:
                txt = f"{txt} {extra}"
            p.drawText(QRectF(x0 + bw + 6, y, val_w, row_h),
                       Qt.AlignVCenter | Qt.AlignLeft, txt)

    def _paint_v(self, p: QPainter, w: int, h: int, vmax: int):
        """竖柱:数值沿柱身竖排(旋转-90°,每根都显示,不占横向空间);
        数值一旦抽稀会让一半柱子看起来"没有用量",这是要避免的。
        x 轴标签两种形态:
        - angle=0(按天页):水平居中,过密按步长抽稀(保持旧版外观);
        - angle>0(会话/计费块页):以柱中心为锚 45° 斜排,相邻标签是平行
          带,只需法向间距≥行高(锚距×sin45°),槽宽≥行高/sin45° 即全画
          不抽稀;按可用对角线长度 elide(首尾标签另受左缘钳制),bot 边距
          按旋转投影自适应并设上限,防矮窗口被标签区吃光。"""
        n = len(self._items)
        f_val, f_lbl = QFont(C_MONO, 8), QFont("Microsoft YaHei UI", 8)
        fm = QFontMetrics(f_lbl)
        line_h = fm.height()
        w_max = max((fm.horizontalAdvance(t[0]) for t in self._items), default=0)
        rad = math.radians(self._angle)
        side, top = 10, 34
        if self._angle > 0:
            sin_r, cos_r = max(math.sin(rad), 1e-6), max(math.cos(rad), 1e-6)
            need_dx = line_h / sin_r + 4          # 斜排防重叠的锚距下限
            diag_cap = 120.0                      # 对角线长度上限(防 bot 失控)
            bot = min(96, int(min(w_max, diag_cap) * sin_r) + line_h + 8)
            diag_ok = max((bot - 8 - line_h) / sin_r, 12.0)
        else:
            bot = 24
        chart_h = h - top - bot
        slot = (w - side * 2) / n
        bar_w = max(min(slot * 0.62, 46.0), 3.0)
        # 抽稀步长只作用于 x 轴标签:保证相邻被绘制的标签互不重叠。
        # angle=0 用整型公式与旧版逐位等价(仅 '09-26' 常量换成实际最长
        # 标签宽度),防浮点噪声让按天页抽稀密度漂移
        if self._angle > 0:
            stride = max(1, math.ceil(need_dx / max(slot, 1e-6)))
        else:
            stride = max(1, -(-n * (w_max + 6) // max(w - 2 * side, 1)))
        p.setPen(QColor(C_BORDER))
        p.drawLine(side, top + chart_h, w - side, top + chart_h)   # 基线
        for i, (label, val) in enumerate(self._items):
            x = side + i * slot + (slot - bar_w) / 2
            bh = max(val / vmax * chart_h, 2) if val else 0
            p.setPen(Qt.NoPen)
            # 当前计费块用告警色高亮(C_WARN),其余保持主题色
            p.setBrush(QColor(C_WARN) if i == self._highlight else QColor(C_ACCENT))
            p.drawRect(QRectF(x, top + chart_h - bh, bar_w, bh))
            # 数值竖排:以柱顶中心为原点旋转,文本向图顶方向延伸
            p.save()
            p.translate(x + bar_w / 2, top + chart_h - bh - 3)
            p.rotate(-90)
            p.setFont(f_val)
            p.setPen(QColor(C_DIM) if val else QColor(C_BORDER))
            p.drawText(QRectF(0, -6, 56, 12), Qt.AlignLeft | Qt.AlignVCenter,
                       fmt_k(val) if val else "0")
            # 第二行数值(¥ 金额):沿柱身另一侧平行竖排,暗色不抢 token 主数值
            if self._extra and i < len(self._extra) and self._extra[i]:
                p.setPen(QColor("#565a66"))
                p.drawText(QRectF(0, 6, 64, 12), Qt.AlignLeft | Qt.AlignVCenter,
                           self._extra[i])
            p.restore()
            if i % stride == 0:                    # 标签抽稀后仍从首根画起
                p.setPen(QColor(C_DIM))
                p.setFont(f_lbl)
                if self._angle > 0:
                    # 斜排:文本右端落在锚点(柱中心、基线下 4px),向左下
                    # 延伸;左缘不越界 → 可用长度另受锚点到左缘的距离钳制
                    anchor_x = x + bar_w / 2
                    avail = max(min(float(fm.horizontalAdvance(label)), diag_ok,
                                    anchor_x / cos_r), 12.0)
                    text = fm.elidedText(label, Qt.ElideRight, avail)
                    p.save()
                    p.translate(anchor_x, top + chart_h + 4)
                    p.rotate(-self._angle)
                    p.drawText(QRectF(-avail, -line_h / 2.0, avail, line_h),
                               Qt.AlignRight | Qt.AlignVCenter, text)
                    p.restore()
                else:
                    p.drawText(QRectF(x - slot / 2, h - bot + 3, slot + bar_w, bot - 5),
                               Qt.AlignCenter, label)

    def mouseMoveEvent(self, ev):
        """悬停 tooltip:全量 label + 数值。45° 斜排的会话/计费块标签被
        elide 截断(会话页槽宽下仅剩十来个字符,对比旧水平条约 20 字符),
        信息量损失在这里补偿。命中判定与绘制共用同一 slot 公式。"""
        if not self._items:
            return
        n = len(self._items)
        if self._horizontal:
            row_h = min(26, max((self.height() - 8) / max(n, 1), 13))
            i = int((ev.position().y() - 4) / row_h)
        else:
            side = 10
            slot = (self.width() - side * 2) / n
            if slot <= 0:
                return
            i = int((ev.position().x() - side) / slot)
        if 0 <= i < n:
            label, val = self._items[i]
            tip = f"{label}\n{fmt_k(val)} tokens"
            if self._extra and i < len(self._extra) and self._extra[i]:
                tip += f"\n{self._extra[i]}"
            QToolTip.showText(ev.globalPosition().toPoint(), tip, self)


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

        # v0.5.0:三图统一竖柱。按天页保持水平标签(默认 0,外观不变);
        # 会话/计费块页标签远宽于槽位('标题… ·N次'/'09-26 14:00'),旧版
        # 水平摆放要么矩形裁剪要么重叠,改 45° 斜排 + elide + 悬停全量 tooltip
        self.daily_chart = BarChart(horizontal=True)
        self.sess_chart = BarChart(horizontal=True)
        self.block_chart = BarChart(horizontal=True)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.daily_chart, "按天(近30天)")
        self.tabs.addTab(self.sess_chart, "按会话(近20个)")
        # 计费块页签:图 + 说明(块界对齐规则与"示意"声明必须就地写明,
        # 否则 quota 未配置时块界会被当成平台真实计费窗来对账)
        blk = QWidget()
        bl = QVBoxLayout(blk)
        bl.setContentsMargins(0, 6, 0, 0)
        bl.setSpacing(4)
        blk_note = QLabel(
            "块界对齐:已配置 quota(Coding Plan)时按平台 5h 计费窗对齐"
            "(nextResetTime−k×5h,黄色=当前活动块);未配置或不可用时回退锚点="
            "最早 completed 请求时刻,此时块界为示意、非平台真实计费窗。"
            "聚合口径=query_source 全部 completed in+out(同今日用量)。")
        blk_note.setObjectName("dim")
        blk_note.setWordWrap(True)
        bl.addWidget(blk_note)
        bl.addWidget(self.block_chart, 1)
        self.tabs.addTab(blk, "计费块(5h)")

        head = QHBoxLayout()
        hint = QLabel("按天=全部来源 in+out(同今日口径,含 ¥ 按刊例价估算);"
                      "按会话=main_turn(与卡片一致);计费块=每 5h 一桶。"
                      "三图口径不同,合计对不上属预期。")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
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
        # 金额版按天(三元组):fetch_daily_usage 的二元组形状被单测与历史
        # 依赖,金额恒走 fetch_daily_usage_cost
        daily_cost = {}
        for d, tok, cny in self.eng.fetch_daily_usage_cost(30):
            daily_cost[d] = (tok, cny)
        # 补齐 30 天连续序列:无用量日画 0 高柱位,避免空档误导读图
        days = [dt.date.today() - dt.timedelta(days=29 - i) for i in range(30)]
        self.daily_chart.set_items(
            [(d.strftime("%m-%d"),
              daily_cost.get(d.isoformat(), (0, 0.0))[0],
              # 第二行 ¥:仅有用量的天显示(30 天大头是 0,排 ¥0.00 会刷屏)
              f"¥{daily_cost[d.isoformat()][1]:.2f}" if d.isoformat() in daily_cost
              else "")
             for d in days])
        rows = self.eng.fetch_session_usage(20)
        self.sess_chart.set_items(
            [((title or sid[:12]) + f" ·{cnt}次", tok)
             for sid, title, tok, cnt in rows])
        blocks = self.eng.fetch_billing_blocks(29)
        cur = -1
        items = []
        for i, (start_ms, tok, is_cur) in enumerate(blocks):
            if is_cur:
                cur = i
            items.append((dt.datetime.fromtimestamp(start_ms / 1000)
                          .strftime("%m-%d %H:%M"), tok))
        self.block_chart.set_items(items)
        self.block_chart.set_highlight(cur)


def main():
    app = QApplication(sys.argv)
    # 主窗是 Qt.Tool(不参与"最后一个窗口关闭即退出"的计数),历史窗口是普通
    # Window —— 不关掉这个默认行为,关闭历史窗口会连主窗一起退出
    app.setQuitOnLastWindowClosed(False)
    win = MeterWindow()
    if "--verify" in sys.argv:
        QTimer.singleShot(1500, win._verify)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
