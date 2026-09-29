#!/usr/bin/env python3
"""zcode-meter Qt 版 —— PySide6 实现。

拖动用 Qt.startSystemMove() 让 Windows 合成器原生接管(与拖普通窗口同路径,
天然支持 Aero Snap),从根上消除 tkinter 版的迟滞/吞点击/互操作崩溃。
数据层复用 data_engine.py(tk 版同源)。

用法(v0.6.0 起 src 布局;根目录 zcode_meter.py 为兼容 shim,同样可用):
  pythonw src/zcode_meter/app.py          正常启动
  python src/zcode_meter/app.py --verify  自检:渲染/贴边判定,打印报告后退出
"""
from __future__ import annotations

import ctypes
import csv
import calendar
import ctypes.wintypes as wt
import datetime as dt
import json
import math
import os
import queue
import re
import sys
import time
from pathlib import Path

from PySide6.QtCore import QLineF, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QActionGroup, QCursor, QColor, QDoubleValidator, QFont, QFontMetrics,
    QGuiApplication, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QPushButton, QScrollArea, QSizePolicy, QStyle,
    QSystemTrayIcon, QTabWidget, QToolTip, QVBoxLayout, QWidget,
)

# 脚本直跑(python src/zcode_meter/app.py)时 __package__ 为空:补 src 进
# sys.path 再走同一 continue 执行,本文件既是包模块 zcode_meter.app、也可作
# __main__ 直跑 —— 刻意不做「re-import 自身为 zcode_meter.app」,__main__ 副本
# 与测试导入的包模块会是两套类对象(常量/样式双份、isinstance 失配)。
# frozen(exe)时 PyInstaller 已内置包,无需引导。
if __package__ in (None, "") and not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # .../src

from zcode_meter.data_engine import (
    BAR_SEGMENT_LABELS_H, BAR_SEGMENT_LABELS_V, BAR_SEGMENTS_H,
    BAR_SEGMENTS_V, BudgetAlerts, DataEngine, QuotaMonitor, Snapshot,
    app_dir, dbg, format_age_zh, format_countdown_hm, load_config,
    quota_reset_event, save_config, trend_forecast,
)
# 皮肤注册表(v0.9 T2):skins 单向被本模块 import(它只拉 QtGui,严禁反向
# import app/data_engine,循环导入红线见 skins.py 模块头)
from zcode_meter import skins

user32 = ctypes.windll.user32   # 模块级(snap 校正用;_win_polish 内的局部变量不动)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD)]


def monitor_workarea_of(hwnd) -> tuple[int, int, int, int]:
    """窗口所在显示器的物理工作区(贴边终审校正依赖)。"""
    hmon = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), 1)   # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        rc = wt.RECT()
        user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rc), 0)
        return rc.left, rc.top, rc.right, rc.bottom
    return mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom


C_BG, C_BORDER = "#16171c", "#2c2f3a"
# v0.8.0 T1 色板(spec=design-preview-abd.html):旧名改值,app.py 内 37 处
# 引用零改动。C_FG/C_DIM 是白 alpha 叠 #17181d 的逐通道实算合成(白.9/
# 白.55),QSS faint 字面与 BarChart 内联 faint 同法(白.35)。
# 【F2 硬约束】C_FG/C_DIM/C_BG/C_BORDER/C_ACCENT/C_WARN 连同下方新增三色
# 必须保持 QColor 可解析的 hex 字符串 —— BarChart 直接 QColor(C_FG)/
# QColor(C_DIM) 消费(历史窗零测试覆盖),rgba 字面实测 isValid()==False
# 会静默画黑;半透明白只允许出现在 QSS 字面与 painter 的
# QColor(255,255,255,a) 构造,不落 C_* 常量。
C_FG, C_DIM, C_ACCENT, C_WARN = "#e8e8e8", "#979799", "#7ad7ff", "#ffd166"
C_ACCENT_DIM = "#60cdff"      # 主题蓝次亮档(sparkline 折线等)
C_TIER_OK = "#6ee7a8"         # 套餐余量充足档(tier_color:pct>50)
C_TIER_DANGER = "#ff6b6b"     # 套餐余量告急档(tier_color:pct<20)
C_MONO = "Cascadia Code"
# T-B 间距标度(8pt 栅格半步档):替换全部布局魔法间距,语义就近映射
# (内容行间=xs/s,分组间=m/l,区块边距=l/xl);豁免点就地注释标明
SP = {"xs": 4, "s": 6, "m": 8, "l": 12, "xl": 16}
C_BORDER_SUB = "#232631"   # 次分节符(弱于 C_BORDER 主分节,层级可辨)

QSS = f"""
QWidget#root {{ border-radius: 10px; }}
QLabel {{ color: {C_FG}; background: transparent; border: none; }}
QLabel#dim   {{ color: {C_DIM}; }}
QLabel#accent {{ color: {C_ACCENT}; }}
QLabel#warn  {{ color: {C_WARN}; }}
QLabel#faint {{ color: #68696c; }}
QLabel#soft    {{ color: rgba(255,255,255,204); }}
QLabel#half    {{ color: rgba(255,255,255,128); }}
QLabel#vstrong {{ color: rgba(255,255,255,217); }}
QFrame#sep {{ background: {C_BORDER}; border: none; max-height: 1px; }}
"""
# v0.8.0 T5+T2:#root 的 background/border 已删 —— 窗口底色/描边/圆角全部
# 改由 MeterWindow.paintEvent 自绘(垂直渐变+光晕+1px 白.08 描边,圆角按
# _bar_form 12/8/12)。QSS 侧保留 border-radius 纯为 QSS_BAR 的 replace 派生
# 链不被静默断掉(无 background 属性时 border-radius 不绘制任何东西,
# 双层绘制风险表的反向兜底:两层都不会画底色)。
# N2 补充白梯度档(v0.8.0):主三档(白.9/.55/.35 → C_FG/C_DIM/faint hex)
# 之外的三档半透明白 —— soft 204(卡片 grid 六格 v)、half 128(模型行速度)、
# vstrong 217(竖条组 v)。落地机制钉死:仅以 QSS objectName selector 的
# rgba 字面 + painter 侧 QColor(255,255,255,a) 构造存在,不新增 C_* 常量
# (F2 hex 约束不破)。QSS_BAR 经上方 replace 自动继承;历史/设置窗不用
# 这三档,不补进 QSS_HIST/QSS_SET。

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


def mk_mono(size: int, weight=None, families=None) -> QFont:
    """等宽字体构造(v0.8.0 T1/M1):Cascadia Code 主族 + Consolas 回退。

    - setFamilies 而非单 family 构造:未装 Cascadia 的机器(精简系统/
      非 Win11)渲染期自动落到 Consolas,数字宽度不漂、不落系统默认衬线;
    - 中文/⏱ 等缺字形不列进 families,走 Qt 字体合并(v0.7 ⏱ 先例),
      显式列中文字体反而会把它抬成首选、数字不再等宽;
    - weight 传 QFont.Weight 枚举(如 QFont.Bold),None 用默认权重。
    等宽构造唯一入口:_mk_lbl 内部单点 + 少数直接 setFont 的调用点,
    其余 label 经 _mk_lbl 自动继承。
    - size 是像素(setPixelSize):与预览 HTML 的 px 字号一一对应。
      v0.8.0 首版误用 setPointSize(pt=px×1.33),整卡放大 1.33 倍 →
      高度溢出、grid 列宽爆掉,视觉对版时纠正(2026-09-28);
    - families(v0.9 T2):皮肤字族替换(None=玻璃缺省 Cascadia+Consolas,
      逐位零漂移)。_build_card/_build_bar 构造期传 self._skin() 值;
      BarChart/历史窗不传皮肤 —— 继续消费玻璃常量(nonGoal 红线)。"""
    f = QFont()
    f.setFamilies([C_MONO, "Consolas"] if families is None
                  else list(families))
    f.setPixelSize(size)
    if weight is not None:
        f.setWeight(weight)
    return f


# 位置记忆状态文件:与 zm_*.log 同目录(frozen 时落 exe 旁)
STATE_PATH = os.path.join(app_dir(), "zm_state.json")

# 导出的用量明细落点(右键『导出 CSV』):与 STATE_PATH 同锚 app_dir,
# frozen 时落 exe 旁。内容是模型名+token+金额(无密钥),.gitignore 已追加
# 防用户本地导出物随仓库误提交。
EXPORT_PATH = os.path.join(app_dir(), "zm_usage_export.csv")


def _state_guard() -> bool:
    """状态读写守卫:--verify 自检或 ZM_NO_STATE=1(回归测试注入)时不读不写
    zm_state.json,防测试改写用户真实的位置记忆。"""
    return "--verify" in sys.argv or os.environ.get("ZM_NO_STATE") == "1"


def _yf(px: int) -> QFont:
    """图表中文标签字体(YaHei,像素基准):px 化对版时与 mk_mono
    同步切 setPixelSize,原 QFont(fam, pt) 构造留给外部显式传参。"""
    f = QFont("Microsoft YaHei UI")
    f.setPixelSize(px)
    return f


def fmt_k(n: int) -> str:
    """token 数量级缩写:图表柱顶与卡片共用同一格式。"""
    if n < 1000:
        return str(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}{suf}"
    return str(n)


# 数据新鲜度冻结阈值(秒)与冻结透明度:贴边(条)形态下套餐轨数据龄超
# 15 分钟 → 整窗降至 0.8。这是 v0.5.1 拍板的静默期语义(quota 静默期停
# 查询,数据停在最后一次活跃时刻)的可见化 —— 纯展示层,不影响任何调度。
FROZEN_AFTER_S = 900.0
FROZEN_OPACITY = 0.8


def frozen_opacity(dock: str | None, fetched_at: float | None,
                   now: float | None = None) -> float:
    """纯函数:贴边形态 + 套餐数据龄 > 15 分钟 → 0.8,其余 → 1.0。

    - dock 为 None(卡片/未贴边)恒 1.0:卡片是主动查看形态,突然半透明
      会被读成『窗口坏了』;条形态常驻屏边,半透明=轻量的『数据可能过期』;
    - fetched_at 缺失/≤0(quota 未配置或尚无首查结果)恒 1.0:没数据就
      无『过期』可言;
    - 龄恰为 900s 不冻结(严格大于):边界两侧抖动无信息量。
    不触任何 Qt/实例状态 —— test_stress 直接注入参数断言。"""
    if dock is None or fetched_at is None or fetched_at <= 0:
        return 1.0
    if now is None:
        now = time.time()
    return FROZEN_OPACITY if now - fetched_at > FROZEN_AFTER_S else 1.0


def tier_color(pct: float) -> str:
    """纯函数:套餐剩余百分比 → 三档视觉色(hex 字符串,QColor 可解析)。

    pct>50 → C_TIER_OK;20≤pct≤50 → C_WARN;pct<20 → C_TIER_DANGER。
    20 与默认告警首阈值 alert_pct=[20,10] 巧合对齐但刻意不动态绑定 ——
    阈值可配多级,分档色是视觉语言非告警状态,动态绑定会让 30/10 配置
    退化成两档。不触任何 Qt/实例状态 —— test_stress 直接注入参数断言
    (同 frozen_opacity 先例)。"""
    if pct > 50:
        return C_TIER_OK
    if pct >= 20:
        return C_WARN
    return C_TIER_DANGER


class TrayController:
    """托盘模式(v0.4.0):主窗可收起到托盘,托盘菜单=今日概览三行(T1)+
    打开历史图表+显示/隐藏+退出,单击托盘图标恢复主窗;预算告警经
    tray.showMessage 气泡派发。
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
        # ---- 今日概览区(T1):三个 disabled QAction,文本在 aboutToShow 刷新 ----
        # 单击 Trigger 已被『恢复主窗』占用(见 _activated),快捷面板只能落在
        # 菜单顶部。刻意不用 QWidgetAction+QLabel:纯展示条目做成不可点
        # QAction,天然不抢 hover 高亮、不多一层布局;disabled 只影响交互,
        # 样式表未写 :disabled 分支,Qt 以正常前景色渲染,概览可读性不降。
        # 菜单平时不打开 → 刷新挂在 aboutToShow 上零轮询开销,打开瞬间直读
        # win 的 UI 线程缓存(snap/_plan_pct)——同线程无竞态、零额外查询。
        self._ov_cost = m.addAction("今日 ¥0.00")
        self._ov_plan = m.addAction("套餐剩余 —")
        self._ov_burn = m.addAction("燃速 0/h")
        for a in (self._ov_cost, self._ov_plan, self._ov_burn):
            a.setEnabled(False)
        m.addSeparator()
        # 历史图表入口:槽体本身也要 QTimer.singleShot(0) 延迟 —— triggered
        # 槽内直接 show 会被菜单关闭的鼠标抓取时序吞掉("点击没反应"坑,
        # _open_history 内部注释有实测记载);这里把整个调用延到菜单模态
        # 循环返回之后,_open_history 内部对 show 还有第二层延迟,双保险。
        m.addAction("打开历史图表",
                    lambda: QTimer.singleShot(0, self.win._open_history))
        m.addSeparator()
        m.addAction("显示 / 隐藏", self.toggle)
        m.addSeparator()
        m.addAction("退出", QApplication.quit)
        m.aboutToShow.connect(self._refresh_overview)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(self._activated)
        self.tray.show()
        self._menu = m                     # QMenu 无父对象,显式持有防 GC

    def _refresh_overview(self):
        """aboutToShow 刷新顶部三行概览,文案口径与卡片逐字对齐:
        - 今日金额:partial(含未知模型,金额为下限)前缀 ≈,同 today_lbl;
        - 套餐剩余:_plan_pct 为 None(quota 未配/未返回)显示占位『—』而非
          藏行 —— 菜单恒三行,行数稳定才扫得快(与卡片『缺数据即隐藏』的
          条形态策略不同,菜单是快照面板不做显隐逻辑);
        - 燃速:trailing 60min 窗口口径,0(今日未活跃)如实显示 0/h。"""
        s = self.win.snap
        cost = s.today_cost_cny or 0.0
        approx = "≈" if s.today_cost_partial else ""
        self._ov_cost.setText(f"今日 {approx}¥{cost:.2f}")
        pct = self.win._plan_pct
        if pct is None:
            self._ov_plan.setText("套餐剩余 —")
        else:
            lt = self.win._plan_left_tok
            self._ov_plan.setText(
                f"剩 {pct:.0f}%" + (f" · ~{fmt_k(int(lt))}" if lt is not None else ""))
        self._ov_burn.setText(
            f"燃速 {fmt_k(int(s.burn_tokens_per_hour or 0))}/h")

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
        root.setContentsMargins(SP["xl"], SP["l"], SP["xl"], SP["l"])
        root.setSpacing(SP["s"])

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

        # ---- v0.7 刷新档位下拉:自动(事件驱动)或固定间隔轮询 ----
        root.addWidget(caption("quota 刷新间隔(自动 = 事件驱动:活跃期最长 3 分钟一查、静默期暂停)"))
        self.refresh_combo = QComboBox()
        # 档位 ↔ 值(数据层 _norm_quota_refresh 钳 [60,86400] 秒);
        # 选『自动』= 整键省略(缺省即 auto 的可选键语义),固定档才落
        # quota_refresh —— 设置窗从不写 "auto" 字面值,zm_config.json 保持
        # 最小形状(旧文件零 diff)
        for lbl, val in (("自动(事件驱动)", "auto"), ("3 分钟", 180),
                         ("5 分钟", 300), ("15 分钟", 900), ("30 分钟", 1800)):
            self.refresh_combo.addItem(lbl, val)
        # 入参 cfg 即 load_config() 输出(已规范化):quota_refresh 缺省或
        # 'auto' → 选第 0 项;合法但非预设(手改文件如 600s)→ 加显选项
        # 回显,不悄悄改值,保存原样透传
        cur = cfg.get("quota_refresh", "auto")
        idx = next((i for i in range(self.refresh_combo.count())
                    if self.refresh_combo.itemData(i) == cur), -1)
        if idx < 0:
            self.refresh_combo.addItem(f"自定义({cur / 60:g} 分钟)", cur)
            idx = self.refresh_combo.count() - 1
        self.refresh_combo.setCurrentIndex(idx)
        root.addWidget(self.refresh_combo)

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
        key 空=不启用、预算空=None、阈值空=恢复默认 [20,10]。
        刷新档位:选『自动』省键(可选键语义),固定档才含 quota_refresh。"""
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
        out = {"quota_api_key": key, "daily_budget_cny": budget, "alert_pct": pcts}
        # 档位:itemData 恒为 "auto" 或合法 int(构造时已钳);选『自动』时
        # 整键省略 —— 回退默认与显式 auto 对消费方等价(get 缺省),但省键
        # 才能守住 test_save_config_roundtrip 的三键形状
        rv = self.refresh_combo.currentData()
        if isinstance(rv, int) and not isinstance(rv, bool):
            out["quota_refresh"] = rv
        return out

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


# ================= v0.8.0 T5:可复用绘制件(spec=design-preview-abd.html) =================
# 三形态(卡/横条/竖条)共用的纯 QPainter 原语 + 三 QWidget。画在 QWidget 上
# 而非 QSS/SVG:单文件零依赖原则(BarChart 同款先例),且半透明白只允许
# 落在 painter 的 QColor(255,255,255,a) 构造(F2 约束,常量层注释)。
# 色值钉死 #60cdff(C_ACCENT_DIM)/#7ad7ff(C_ACCENT):模块级函数在 T1 常量
# 之后定义,直接引用常量而非字面 —— T1 若微调色值此处自动跟随。


def paint_sparkline(painter: QPainter, rect: QRectF, values,
                    line: str | None = None, dot: str | None = None) -> None:
    """速度趋势折线(预览 :153-157):#60cdff 1.5px 圆帽折线 + 末端点
    #7ad7ff r=2。x_i = i/(n-1)*w;y 按 min-max 线性映射、上下各 1px inset;
    min==max 画水平中线(单值无趋势,不放大噪声);n<2 直接 return ——
    控件侧另有 <2 点隐藏兜底(UI 不闪空,风险表钉死),这里再防一层。
    时间正序由数据层 fetch_recent_speeds 保证,函数不排序不反转。
    line/dot(v0.9 T2):皮肤折线/端点色,None=玻璃缺省 C_* 常量(零漂移)——
    皮肤基建只加参不改缺省,玻璃渲染路径与旧版逐位一致。"""
    vals = [float(v) for v in (values or [])]
    n = len(vals)
    if n < 2:
        return
    painter.setRenderHint(QPainter.Antialiasing)
    w, h = rect.width(), rect.height()
    lo, hi = min(vals), max(vals)
    pts = []
    for i, v in enumerate(vals):
        x = rect.left() + i / (n - 1) * w
        y = (rect.top() + h / 2 if hi == lo else
             rect.top() + 1 + (1 - (v - lo) / (hi - lo)) * (h - 2))
        pts.append(QPointF(x, y))
    pen = QPen(QColor(line if line is not None else C_ACCENT_DIM))
    pen.setWidthF(1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPolyline(QPolygonF(pts))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(dot if dot is not None else C_ACCENT))
    painter.drawEllipse(pts[-1], 2.0, 2.0)


def paint_ring(painter: QPainter, rect: QRectF, pct: float, color,
               base: QColor | None = None) -> None:
    """环形进度(预览 :81/:166-170):底环白.10 3.5px、前景 color 3.5px 圆帽,
    12 点起顺时针 pct%·360°,pct 钳 [0,100]。中心文字由调用方叠加
    (卡片 46px 中心 N% 11pt tier 色 / 横条 12px 无字)—— 函数只画环,
    文字与环的字号层级解耦。Qt 角度系 0°=3 点钟、正值逆时针:12 点=90°,
    顺时针扫描即负 span。base(v0.9 T2):皮肤底环色,None=玻璃缺省白.10。"""
    p = max(0.0, min(100.0, float(pct)))
    painter.setRenderHint(QPainter.Antialiasing)
    # 内缩 stroke/2(≈2px):圆帽在 0%/100% 端点不越出控件矩形
    r = QRectF(rect).adjusted(2.0, 2.0, -2.0, -2.0)
    painter.setPen(QPen(QColor(255, 255, 255, 26) if base is None else base,
                        3.5))                                 # 白.10 ≈ 26/255
    painter.drawArc(r, 0, 360 * 16)
    if p > 0:
        pen = QPen(QColor(str(color)), 3.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(r, 90 * 16, int(-p * 3.6 * 16))


class PulseIndicator(QWidget):
    """脉冲环状态指示(预览 :57-66/:224/:259):active=蓝环+内核+外扩圈
    2s ease-in-out;idle=白.35 静态空心环。

    - 相位复用 MeterWindow._breath 与 60ms _breath_timer:窗口侧只推进相位
      (set_phase),本控件不建定时器 —— 多一个 timer 就多一份空转 repaint;
      _breath 周期 750ms(0.08/tick),×0.375 折算成 2s 周期(CSS keyframes
      2s 的等价实现);
    - 仅 generating 时 update()(set_phase 内部判定):idle 后零重绘,
      与旧 QLabel 呼吸点『空闲不刷样式』的节制同款;
    - 尺寸三档:卡 14 / 横条 10 / 竖条 12(构造参数),环宽/内核/外扩圈
      全按直径比例缩放;
    - 色参(v0.9 T2):idle/active/core 三色可选注入,None=玻璃缺省
      (白.89 静态环/C_ACCENT_DIM 环+外扩圈/内核 α230,与旧版逐位一致);
      active 兼作外扩圈基色 —— 外扩圈只变 α 不变色相,由 active 的 RGB
      派生(QColor 拷贝后 setAlpha),玻璃路径数值恒等。"""

    def __init__(self, diameter: int, parent=None, idle_color: QColor | None = None,
                 active_color: QColor | None = None, core_color: QColor | None = None):
        super().__init__(parent)
        self._d = int(diameter)
        self.setFixedSize(self._d, self._d)
        self._active = False
        self._phase = 0.0
        self._c_idle = idle_color
        self._c_active = active_color
        self._c_core = core_color
        # 指示件不参与交互:让鼠标按下穿透到主窗(拖动窗口的既定路径)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def sizeHint(self) -> QSize:
        return QSize(self._d, self._d)

    def set_active(self, on: bool) -> None:
        if self._active != bool(on):
            self._active = bool(on)
            self.update()

    def set_phase(self, phase: float) -> None:
        self._phase = float(phase) % 1.0
        if self._active:
            self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        d = float(self._d)
        cx = cy = d / 2
        ring_w = max(1.2, d * 0.15)            # 14→2.1  CSS 2px;10→1.5
        ring_r = d / 2 - ring_w / 2 - 0.5
        # 皮肤色参(T2):None=玻璃缺省常量,数值与旧版逐位一致
        idle_c = (self._c_idle if self._c_idle is not None
                  else QColor(255, 255, 255, 89))
        act_c = (self._c_active if self._c_active is not None
                 else QColor(C_ACCENT_DIM))
        core_c = (self._c_core if self._c_core is not None
                  else QColor(96, 205, 255, 230))
        if not self._active:
            # idle:白.35 静态空心环(89/255,与 faint 档同值)
            pen = QPen(idle_c, ring_w)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), ring_r, ring_r)
            return
        # active:外扩圈(2s ease-in-out)→ 蓝环 → 内核
        u = (self._phase * 0.375) % 1.0        # 750ms 相位折算 2s 周期
        wave = math.sin(math.pi * u)           # 0→1→0, ease-in-out 对称形
        halo_r = ring_r + 1.0 + d * 0.36 * wave
        halo_a = int(89 * (1.0 - wave))        # 起 0.35 → 峰值 0
        if halo_a > 0:
            halo_c = QColor(act_c)             # 外扩圈=active 色相,只变 α
            halo_c.setAlpha(halo_a)
            p.setPen(QPen(halo_c, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), halo_r, halo_r)
        pen = QPen(act_c, ring_w)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), ring_r, ring_r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(core_c)                     # 内核 opacity .9
        p.drawEllipse(QPointF(cx, cy), d * 0.28, d * 0.28)


class SparklineWidget(QWidget):
    """速度趋势控件:固定 sizeHint(卡 72x24 / 横条 44x16 / 竖条 60x14,
    预览 :153/:226-229/:262-265),paintEvent 全权交给 paint_sparkline。
    <2 点的显隐由宿主(_apply_snapshot)驱动 setVisible —— 本控件不隐藏
    自己,保持纯展示件语义;隐藏控件被 _bar_size 跳过(v0.5.0 机制)。"""

    def __init__(self, w: int, h: int, parent=None, stretch: bool = False,
                 line_color: str | None = None, dot_color: str | None = None):
        super().__init__(parent)
        # stretch=True(卡形态 2026-09-28):吃满主数字行剩余宽度 —— 用户
        # 反馈卡片上半右侧空白,速度趋势是填充该带的最自然数据;min w=72
        # 保底可读。条形态保持 fixed(v0.5.0 语义,_bar_size 依赖定宽)。
        if stretch:
            self.setMinimumSize(int(w), int(h))
            sp = self.sizePolicy()
            sp.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
            sp.setVerticalPolicy(QSizePolicy.Policy.Fixed)
            self.setSizePolicy(sp)
        else:
            self.setFixedSize(int(w), int(h))
        self._values: list = []
        # 皮肤折线/端点色(T2):None=玻璃缺省 C_*(paint_sparkline 缺省),
        # _build_card/_build_bar 构造期传 self._skin() 值
        self._line_color = line_color
        self._dot_color = dot_color
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def sizeHint(self) -> QSize:
        # minimumSize 而非当前 width/height:未布局时 QWidget 默认 640x480,
        # stretch 模式无定宽钳制,sizeHint 报 480 高会把布局撑爆
        # (measure 满载 755px 事故,2026-09-28);fixed 模式 min==定宽,同式
        return QSize(self.minimumWidth(), self.minimumHeight())

    def set_values(self, values) -> None:
        self._values = [float(v) for v in (values or [])]
        self.update()

    def paintEvent(self, ev):
        paint_sparkline(QPainter(self), QRectF(self.rect()), self._values,
                        self._line_color, self._dot_color)


class RingWidget(QWidget):
    """环形进度控件:paintEvent 全权交给 paint_ring;diameter 46(卡片,
    中心 11pt Bold tier 色 N%)或 12(横条,无字)。pct/color 经 set_pct
    注入 —— tier 色由宿主按 tier_color(pct) 算好传入,本控件不掺业务。
    base_color(v0.9 T2):皮肤底环色,None=玻璃缺省白.10。"""

    def __init__(self, diameter: int, center_text: bool = True, parent=None,
                 base_color: QColor | None = None):
        super().__init__(parent)
        self._d = int(diameter)
        self.setFixedSize(self._d, self._d)
        self._center_text = bool(center_text)
        self._pct: float = 0.0
        self._color: str = C_WARN              # 首帧前兜底色(段隐藏,不可见)
        self._base = base_color
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def sizeHint(self) -> QSize:
        return QSize(self._d, self._d)

    def set_pct(self, pct: float, color=None) -> None:
        self._pct = max(0.0, min(100.0, float(pct)))
        if color is not None:
            self._color = str(color)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        paint_ring(p, QRectF(self.rect()), self._pct, self._color, self._base)
        if self._center_text:
            p.setFont(mk_mono(11, QFont.Bold))
            p.setPen(QColor(self._color))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       f"{self._pct:.0f}%")


class MeterWindow(QWidget):
    # v0.4.0:新增 燃速/套餐剩余 两行 + 今日用量可能多源第二行,自然高度
    # 实测 314(单源)。v0.5.1 套餐剩余改两行文案(第二行重置倒计时),注入
    # 实测:0~2 行模型 → 328/328/342,3 行模型 356。v0.7 T-A 信息层级重排:
    # 满载 0~4 行模型 → 303/303/317/331/345,全矩阵 max=345,CARD_H=346。
    # v0.8.0 T5+T2 卡片重绘(spec=design-preview-abd.html .g-card):宽改
    # 设计宽 313(预览 :36);满载文本自然宽实测 372>313,按 spec『elide/
    # 固定列宽』修宽度策略(CARD_GRID_COL_W),三态收敛 313。视觉对版
    # (2026-09-28)字号 px 化后重测(findings/measure_card_baseline.py,
    # 原生平台+停引擎三闸防真实数据竞态):grid 键值叠印修复(4 行结构)→
    # 284/297/310/323/336;模型行对版再测 → 305/318/331/344/357;上半右侧
    # 空白利用(sparkline 拉伸/多源并入今日行/预算余量上卡,今日行省一行)
    # 再测 → 287/300/313/326/339,全矩阵 max=339 → CARD_H=341(339 截断点
    # +2 余量)。机制不变:
    # _unset_dock/_restore_state/_detach_to_pointer 用它 setGeometry,偏小会
    # 静默截断(ui-verify 只打印不校验,需人工目视)。
    CARD_W, CARD_H = 313, 341
    # grid 六格固定列宽(v0.8.0 T5+T2 宽度策略裁决):满载文本自然宽实测
    # 372 > CARD_W 313(findings/measure_card_baseline.py,13pt 字号期),按
    # spec 的『elide/固定列宽』修标签宽度策略 —— 列宽钉死后 v label 走
    # Ignored 策略(不被 sizeHint 反推),文本按列宽 QFontMetrics elide
    # (典型值全显,极端值 elide 尾段+tooltip 回读全量)。视觉对版
    # (2026-09-28):字号 px 化(13px)后单字符宽 7.8px,边距回归 16×2、
    # 列距 2→8(预览 gap 12 的紧凑折中,313 内放不下 12),列宽按新内容
    # 宽 281−16 重算:入/出 102(真机实测会话 "427.1M / 528K"=13 字符
    # 101px,首版 92 会被 elide 截断)/ 缓存命中 68("100.00%"=55)/
    # ⏱首/总 95("0.8 / 12.4s"=86),合计 265+16+32=313 收敛。
    CARD_GRID_COL_W = (102, 68, 95)
    _bar_form = None              # 类级默认:paintEvent 可能早于首次 _build_card
    _pill_geo = None              # 液态玻璃药丸几何缓存(事件循环下一拍量取)
    _pill_measure_pending = False # 防重复排程的量取闸
    _settle_timer = None          # 类级默认:moveEvent 可能早于 __init__ 定时器创建
    _in_prog_move = False         # 程序性移动(吸附/恢复)期间,moveEvent 不喂防抖
    _dock_guard_until = 0.0       # 贴边保护期:吸附后的连锁 settle 判定直接跳过          # 逻辑像素(DIP),Qt 自动做 DPI 换算
    BAR_H, BAR_V = 24, 38
    # v0.8.0 T3/F1:竖条定宽 —— 全 spec 唯一明示的尺寸机制变更。宽度侧由
    # 『max_w 聚合 + min(...,100) cap』改为定宽常量(内容超宽按旧 cap 同款
    # 哲学硬截;竖条文案已钉死紧凑形,预期不触界);_bar_size 高度侧聚合与
    # 『跳过 hidden』机制、横向分支均一字不动。
    BAR_V_W = 100   # 116→104→100 用户两轮『再收窄』2026-09-28(内容 84;套餐行最坏 ~906.5M · 4h 59m=79px,倒计时受 5h 窗上限约束)
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
        # 贴边条可勾选段(v0.8.0 对版期,用户『靠边停放自定义显示内容』):
        # h=顶/底共用、v=左/右共用;速度+圆点恒显不进清单。右键菜单勾选
        # 即时重建+save_config 持久化
        self.bar_segments = cfg.get("bar_segments",
                                    {"h": list(BAR_SEGMENTS_H),
                                     "v": list(BAR_SEGMENTS_V)})
        # 皮肤 id(v0.9 T2):cfg 来自 load_config(已归一化,T1 落地后恒含
        # 白名单内 skin 键;T1 未落地时无此键 → glass,同样安全)。registry
        # 缺项回退 glass 由 _skin() 承担(手改文件指向未实现 id 防 KeyError)
        self.skin_id = cfg.get("skin", "glass")
        # key 内存基准(v0.5.0 设置窗):设置保存后的 monitor 对账必须与它
        # 比较 —— 严禁落盘后回读文件(恒等 → monitor 永不重启 → 残留旧账号
        # 套餐数据)。用完即弃,只在保存成功后更新。
        self._quota_key = cfg["quota_api_key"]
        self.alerts = BudgetAlerts(cfg["alert_pct"])
        self.quota_monitor = None
        if cfg["quota_api_key"] and not _state_guard():
            self.quota_monitor = QuotaMonitor(cfg["quota_api_key"],
                                              cfg.get("quota_refresh", "auto"))
            self.quota_monitor.start()
            # v0.5.1 事件驱动:引擎 completed 水位前进 → monitor 记活跃,
            # 节流器据此决定何时真发 quota 请求(未配 key 不接线,零开销)
            self.eng.on_activity = self.quota_monitor.notify_activity
        self._plan_pct: float | None = None   # quota 轨 5h 窗剩余%(UI 侧缓存)
        # v0.5.1:查询完成时刻(新鲜度『N分钟前』)与 nextResetTime(倒计时
        # 纯本地递减,不为它发请求);换号/清号时随三缓存一并清零
        self._plan_fetched_at: float | None = None
        self._plan_next_reset: float | None = None
        self._plan_left_tok: float | None = None   # 剩余量推算(当前块token×剩余%/已用%)
        # 剩余量动态校准:每次 quota 刷新若 pct 跳变,用本地窗口 token 增量
        # 标定"1%=X token"(EMA);比块累计推算更即时、不带历史误差
        self._pct_ratio: float | None = None        # 1% 对应的 token 数
        self._last_pct_seen: float | None = None
        self._last_win_tok_seen: float | None = None
        # 重置事件判据的 prev 侧(上一次 quota 快照):_update_quota 在覆盖它
        # 之前先与最新快照比对。换号/清号分支必须一并置 None —— 残留旧账号
        # 快照会让新号首查的 nextResetTime 前跳被误判成『额度已重置』
        # (T8 重写该分支时必须保留那行清 prev)
        self._prev_quota: dict | None = None
        # 数据新鲜度视觉化:setWindowOpacity 的值缓存 —— 仅变化时才 set,
        # _poll_timer 200ms 一跳重复设置同一值会引发无谓的重绘 churn;
        # 初始 1.0 与 Qt 默认窗口透明度一致(见 _apply_freshness)
        self._frozen_opacity = 1.0
        # 托盘:无托盘环境(远程会话等)整体跳过,不崩不影响 --verify/stress
        self.tray = TrayController.create(self)

        self.setWindowTitle("zcode-meter")
        self.setObjectName("root")
        self.setStyleSheet(self._skin_qss())   # 皮肤化(v0.9 T2);玻璃=qss 逐位同旧
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        # 半透明窗口:自绘圆角(26px)外是直角窗口底,系统默认不透明底色
        # 在四角露成『延伸边角』(用户截图 2026-09-29;玻璃底色深所以此前
        # 不可见,液态玻璃亮幕对比下暴露)。Translucent 后窗口形状完全由
        # paintEvent 的圆角 path 决定,四角真正透明
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._build_card()
        self.adjustSize()
        # v0.8.0 T5+T2:adjustSize 按内容取自然宽度,新卡内容(30pt 主数字+
        # 单位+sparkline)自然宽可能 ≠ 设计宽 313 —— 夹到 CARD_W,防『初始
        # 路径宽度与常量脱节』(风险表:启动走 adjustSize 而非常量)。高度
        # 保留自然值,后续 _unset_dock/_restore_state 按 CARD_H 校正。
        if self.width() != self.CARD_W:
            self.resize(self.CARD_W, self.height())
        sg = QGuiApplication.primaryScreen().availableGeometry()
        self.move(sg.right() - self.CARD_W - 24, sg.top() + 90)
        self.show()
        # 先 show 再恢复:保存的是贴边形态时,_set_dock 里的 self.screen()
        # 只有窗口 map 后才反映真实所在屏(多显示器下不能锚错屏)。
        # show 到首帧绘制之间隔着 exec(),此处置零闪烁。
        self._restore_state()
        # Liquid Glass A:启动皮肤若是液态玻璃,窗口 map 后开亚克力
        # (winId 需要原生句柄,延到 80ms 润色同拍)
        if self.skin_id == "liquid":
            QTimer.singleShot(80, self._enable_acrylic)

        # 兜菜单退出/事件循环正常退出路径的位置保存
        QApplication.instance().aboutToQuit.connect(self._save_state)

        # 窗口 map 后做 DWM 润色 + 免激活
        QTimer.singleShot(80, self._win_polish)

        self._poll_timer = QTimer(self, interval=200, timeout=self._poll_queue)
        self._poll_timer.start()
        # 贴边判定防抖:系统拖动模态循环会吞 ButtonRelease,release 路径
        # 时灵时不灵;改由 moveEvent 触发 — 窗口停止移动 150ms 后判定
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(150)
        self._settle_timer.timeout.connect(self._settle)
        self._breath_timer = QTimer(self, interval=60, timeout=self._tick_breath)
        self._breath_timer.start()
        self.eng.start()

    # ---- v0.9 T2 皮肤基建:注册表访问与 QSS 形态分派 ----
    def _skin(self) -> "skins.SkinDef":
        """当前皮肤定义:registry 缺项回退 glass 注册表项 —— T1 白名单先行
        落地/用户手改 zm_config.json 指向尚未实现的 id 时防 KeyError,任何
        合法白名单 id 都能安全渲染(T2 期安全,回归断言钉死)。"""
        return skins.REGISTRY.get(self.skin_id) or skins.REGISTRY["glass"]

    def _skin_qss(self) -> str:
        """按当前形态取皮肤样式表:卡(未贴边,_bar_form=None)→ qss,
        横/竖条 → qss_bar。玻璃两值与模块级 QSS/QSS_BAR 逐位相等(stress
        断言钉死),qss_bar 派生链与 app.py:118 同一条 replace(防静默断链)。"""
        sk = self._skin()
        return sk.qss_bar if self._bar_form in ("h", "v") else sk.qss

    # ---- v0.8.0 T5:窗口背景自绘(替代 QSS #root background/border) ----
    def paintEvent(self, ev):
        """皮肤分派(v0.9 T2):deco 非 None 的皮肤交其自绘背景(painter/
        win/form),玻璃与 deco 未实现的皮肤走下方既有 渐变+光晕+描边 代码
        路径 —— 玻璃路径逐位不动(deco 恒 None),T2 骨架期九款全部落到
        玻璃兜底(『registry 仅 glass 亦安全渲染』同性质)。
        deco 前置 layout().activate():Qt 布局惰性激活(重建后几何要等
        LayoutRequest 才落地),deco 在 paint 里现读 label.geometry(),
        重建(贴边↔卡片)后的首帧 paint 若先于布局激活,读到的是旧形态
        几何 —— 真机『3 个竖条药丸+横杠残影』的真因(2026-09-29,调研
        SO:78795785);activate 幂等,已激活时零开销。"""
        deco = self._skin().deco
        if deco is not None:
            # 药丸几何缓存未建(首帧/竞态)→ 排一拍后量(此时 Qt 事件循环
            # 已自然完成布局激活,几何必然新鲜;--verify 无循环不触发,
            # 天然守卫),本帧先画无药丸的干净底。
            if (self._bar_form is None and self._pill_geo is None
                    and not self._pill_measure_pending
                    and self.isVisible()):
                self._pill_measure_pending = True
                QTimer.singleShot(0, self._measure_pill_geo)
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            deco(p, self, self._bar_form)
            return
        self._paint_glass(ev)

    def _measure_pill_geo(self):
        """事件循环下一拍量六格 k/v 几何写 _pill_geo(此时布局激活必然
        已完成,几何新鲜)。量不到(极少)再排一拍,最多 5 次。"""
        self._pill_measure_pending = False
        vs = skins._card_grid_vs(self)
        ks = getattr(self, "_grid_k_labels", None)
        if (len(vs) != 6 or not ks or len(ks) != 6
                or self._bar_form is not None):
            return
        pairs = list(zip(ks, vs))     # 每丸 = 标题 label + 数值 label(同列)
        try:
            geos = [(kg.geometry(), vg.geometry()) for kg, vg in pairs]
            ok = all(g.height() > 0 and g.top() > 0
                     for kg, vg in geos for g in (kg, vg))
        except RuntimeError:
            ok = False
        if not ok:
            self._pill_retry = getattr(self, "_pill_retry", 0) + 1
            if self._pill_retry <= 5:
                QTimer.singleShot(0, self._measure_pill_geo)
            return
        self._pill_retry = 0
        # 六丸定稿(用户 2026-09-29):每丸完整包住『标题+数值』两行,
        # 宽 81(中线距 93 留 12 缝),高=标题顶-4 到数值底+3
        # 统一形状终版(用户『6 丸大小形状一致』+『上下排要有间隔』):
        # 宽 = min 中线距 89.5 − 缝 6 = 83.5 → 三缝全部 ≥6 且一致;
        # 高 36(k 顶−2 到 v 底+2),排间缝 = 5−4 = 1px 不重叠。
        # 六丸 83×37 全等。
        # 高度不外扩(k 顶到 v 底原值):两排文字间只有 5px 空隙,±2 呼吸
        # 会让药丸贴死/重叠(用户红圈 2026-09-29);紧凑丸排间净空 5px
        # 第二排整体下移 3px(用户 2026-09-29『第二行的药丸整行往下移一点』
        # —— 下排药丸底距模型分节线过近,下移拉开与上排的层次;丸高不变)
        ROW2_SHIFT = 3.0
        pills = []
        for i, (kg, vg) in enumerate(geos):
            cx = (kg.left() + kg.right() + vg.left() + vg.right()) / 4.0
            y0 = float(min(kg.top(), vg.top()))
            y1 = float(max(kg.bottom(), vg.bottom()))
            if i >= 3:
                y0 += ROW2_SHIFT
                y1 += ROW2_SHIFT
            pills.append((cx, y0, y1))
        # 宽在循环外按中线距定:全部同宽
        mids = [c for c, _, _ in pills]
        gaps = [mids[1] - mids[0], mids[2] - mids[1]]
        pill_w = min(gaps) - 6.0
        pills = [(cx, y0, y1) for cx, y0, y1 in pills]
        self._pill_geo = {"pill_w": float(pill_w), "pills": pills}

    def _paint_glass(self, ev):
        """玻璃皮肤背景(原 paintEvent 正文,v0.9 T2 原样下沉,逐位不动)。
        深渐变玻璃底(不透明近似版,预览 .g-card/.g-bar/.g-vbar):
        ① 垂直渐变 #1c1e28→#15161d —— 预览底 rgba(24,26,34,.58) 叠
        blur 壁纸后的等效观感(视觉对版 2026-09-28:首版 #17181d 过暗,
        光晕叠上去仅 3 个 RGB 单位差、肉眼不可辨,玻璃感整体丢失);
        ② 顶部中央 200x100 径向光晕(rgba(96,205,255,0.18)→透明,y 半轴
        压缩 0.5 成椭圆,裁进圆角);
        ③ 1px rgba(255,255,255,0.08) 描边 + 顶部内发光线(预览 inset
        0 1px 0 白.06 的等价物,首版遗漏)。圆角按 _bar_form 分档:
        卡/竖条 12(预览 :42/:119)、横条 8(预览 :104)—— 圆角归属钉死在
        形态而非 QSS(QSS_BAR 的 7px 派生已不绘制,见 QSS 注释)。
        覆盖 paintEvent 即接管控件底色渲染,不调 super(默认实现只做 QSS
        背景绘制,#root 已无 background,调了也是空转);子控件(QLabel 等)
        由 Qt 在父窗口之后独立绘制,不受影响。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        radius = 12 if self._bar_form in (None, "v") else 8
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0),
                            radius, radius)
        grad = QLinearGradient(0.0, 0.0, 0.0, float(h))
        grad.setColorAt(0.0, QColor("#1c1e28"))
        grad.setColorAt(1.0, QColor("#15161d"))
        p.fillPath(path, grad)
        # 顶部光晕:径向渐变圆心在顶边中点,半径 100(水平全幅 200);
        # scale(1, 0.5) 把纵向压成 100px 高的椭圆下半(预览 ::before 的
        # top:-60px 只露下半 40px 的等价近似),clip 进圆角防溢出。
        p.save()
        p.setClipPath(path)
        p.translate(w / 2.0, 0.0)
        p.scale(1.0, 0.5)
        halo = QRadialGradient(QPointF(0.0, 0.0), 100.0)
        halo.setColorAt(0.0, QColor(96, 205, 255, 46))    # 0.18 ≈ 46/255
        halo.setColorAt(0.7, QColor(96, 205, 255, 0))
        p.fillRect(QRectF(-100.0, -100.0, 200.0, 200.0), halo)
        p.restore()
        # 顶部内发光线:圆角矩形内 1px 横线(白.06)—— 预览玻璃感的
        # 上沿高光,一半来自这条线、一半来自光晕
        p.save()
        p.setClipPath(path)
        p.setPen(QPen(QColor(255, 255, 255, 15), 1.0))
        p.drawLine(QPointF(radius, 1.5), QPointF(w - radius, 1.5))
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 20), 1.0))    # 白.08 ≈ 20/255
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

    # ---- Liquid Glass 方案 A:Windows 亚克力(真实背景模糊) ----
    def _enable_acrylic(self):
        """SetWindowCompositionAttribute 亚克力(未公开 API,调研 2026-09-29):
        DWM 把窗口身后内容实时模糊后垫底 —— 『玻璃压住的底层光线』由系统
        供给。tint 走液态玻璃亮灰蓝(ABGR:alpha 高字节)。失败/Win11 兼容
        性问题 → 静默回退自绘亮幕(deco 的 base 渐变原样在),观感降级不
        崩溃。仅液态玻璃皮肤调用。"""
        import ctypes
        from ctypes import wintypes

        class ACCENT_POLICY(ctypes.Structure):
            _fields_ = [("AccentState", ctypes.c_int),
                        ("AccentFlags", ctypes.c_int),
                        ("GradientColor", wintypes.DWORD),
                        ("AnimationId", ctypes.c_int)]

        class WINCOMPATTRDATA(ctypes.Structure):
            _fields_ = [("Attribute", ctypes.c_int),
                        ("Data", ctypes.c_void_p),
                        ("SizeOfData", ctypes.c_size_t)]

        try:
            hwnd = int(self.winId())
            # ACCENT_ENABLE_ACRYLICBLURBEHIND=4;tint #39415a @ a0 →
            # ABGR = 0xA05A4139(alpha a0, B 5a, G 41, R 39)
            accent = ACCENT_POLICY(4, 2, 0xA05A4139, 0)
            data = WINCOMPATTRDATA(
                19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
                ctypes.sizeof(accent))
            ok = ctypes.windll.user32.SetWindowCompositionAttribute(
                hwnd, ctypes.byref(data))
            return bool(ok)
        except Exception:
            return False

    def _disable_acrylic(self):
        """关亚克力(ACCENT_ENABLE_BLURBEHIND=0 即恢复普通窗口)。"""
        import ctypes
        from ctypes import wintypes

        class ACCENT_POLICY(ctypes.Structure):
            _fields_ = [("AccentState", ctypes.c_int),
                        ("AccentFlags", ctypes.c_int),
                        ("GradientColor", wintypes.DWORD),
                        ("AnimationId", ctypes.c_int)]

        class WINCOMPATTRDATA(ctypes.Structure):
            _fields_ = [("Attribute", ctypes.c_int),
                        ("Data", ctypes.c_void_p),
                        ("SizeOfData", ctypes.c_size_t)]

        try:
            hwnd = int(self.winId())
            accent = ACCENT_POLICY(0, 0, 0, 0)
            data = WINCOMPATTRDATA(
                19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
                ctypes.sizeof(accent))
            ctypes.windll.user32.SetWindowCompositionAttribute(
                hwnd, ctypes.byref(data))
        except Exception:
            pass

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
            # DWM 圆角策略 DONOTROUND:系统小圆角(~8px)小于自绘圆角
            # (26px),系统角外的直角窗口底露成四角『延伸边角』(用户截图
            # 2026-09-29);关掉系统圆角,四角完全交自绘
            v2 = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(v2), 4)    # DONOTROUND
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
            # _set_dock 走 _apply_dock_geometry(物理坐标唯一权威,含夹取),
            # 事后不得再用 Qt 坐标二次 move —— DPI≠100% 下两套坐标互相
            # 否定,是"贴边后位置漂移/闪烁"的病根之一(旧多屏保险已删)
            self._set_dock(st["dock"])
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

    def moveEvent(self, event):
        super().moveEvent(event)
        if self._in_prog_move or self._settle_timer is None:
            return   # 程序性移动(吸附自身)不触发判定链
        # 用户拖动:重置防抖,停止移动 150ms 后做贴边判定
        self._settle_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 贴边↔卡片形态切换窗口尺寸剧变(条 801x40 ↔ 卡 313x341),Qt 的
        # resize 重绘对增大的方向只补画新增区域 → 旧帧残留(『白边/灰白
        # 遮罩』)。update() 是异步排队,与布局激活的时序仍可能错一拍,
        # repaint() 同步重绘(几何此时已新鲜,paintEvent 内会再 activate)
        self.repaint()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            QTimer.singleShot(90, self._settle)   # 保留双保险(release 到达时立即判定)

    def _detach_to_pointer(self):
        pos = QCursor.pos()
        w, h = self.CARD_W, self.CARD_H
        sg = self.screen().availableGeometry()
        x = max(min(pos.x() - w // 2, sg.right() - w - 2), sg.left() + 2)
        y = max(min(pos.y() - 12, sg.bottom() - h - 2), sg.top() + 2)
        self.dock = None
        # 同步重建(回滚 singleShot 延迟):延迟版被 startSystemMove 的
        # 模态拖动循环吞掉 —— 拖动恢复卡片变长条(用户 2026-09-29)。崩溃
        # 真因是 paint 里 measure 读已销毁对象(paintEvent 已修:形态守卫
        # +异常保护),重建本身同步是安全的(v0.8.0 前素来如此)。
        self._build_card()
        self.setStyleSheet(self._skin_qss())   # 皮肤化(T2):卡形态 → qss
        self.layout().activate()   # 刷新窗口最小宽,防钳宽(旧最小宽钳 313)
        self.setGeometry(x, y, w, h)
        self._apply_snapshot(self.snap)
        self._save_state()               # 拖离贴边也是形态变化,同样即时落盘

    # ---- 贴边判定(全 Qt 逻辑坐标,无 DPI 手算) ----
    def _pointer_pos(self):
        """松手时的指针位置 —— Win32 物理坐标(与贴边判定同坐标系)。
        QCursor.pos() 返回逻辑像素,DPI≠100% 下与真实鼠标位置有换算
        偏差(125% 时约 48px):鼠标已怼到物理屏边,逻辑判定却差几十px
        永不触发 —— 实测拖拽贴右失效、窗口被拖出屏幕的病根。"""
        try:
            pt = wt.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            return QPoint(pt.x, pt.y)
        except Exception:
            return QCursor.pos()

    def _settle(self):
        """贴边判定按【鼠标触边】:指针怼到屏幕边缘即贴对应边。
        指针(物理)与工作区(物理)同坐标系比较,鼠标碰到真实边缘必触发。"""
        if time.time() < self._dock_guard_until:
            return   # 刚吸附完:此时的 settle 是吸附移动的连锁反应,不是用户意图
        pos = self._pointer_pos()
        try:
            hwnd = int(self.winId())
            sl, st_, sr_, sb_ = monitor_workarea_of(hwnd)
        except Exception:
            sg = self.screen().availableGeometry()
            sl, st_, sr_, sb_ = sg.left(), sg.top(), sg.right(), sg.bottom()
        edge = 8                              # 物理像素阈值
        want = None
        if pos.y() <= st_ + edge: want = "top"
        elif pos.y() >= sb_ - edge + 1: want = "bottom"
        elif pos.x() <= sl + edge: want = "left"
        elif pos.x() >= sr_ - edge + 1: want = "right"
        if want and want != self.dock:
            self._set_dock(want)
        elif not want and self.dock:
            self._unset_dock()

    def _apply_dock_geometry(self):
        """贴边状态的唯一几何权威:尺寸按 Qt 逻辑算(_bar_size),换算物理后
        连位置带尺寸一次 SetWindowPos 完成。此前 _set_dock(Qt 逻辑)/
        _refit(Qt 逻辑)/_snap(Win32 物理)三方混管,DPI≠100% 时逻辑↔物理
        换算偏差互相否定,窗口在两个位置间每 200ms 跳一次(用户所见"闪")
        且可能停在错乱状态。单一权威 + 单一坐标系后不存在打架。"""
        if self.dock not in ("left", "right", "top", "bottom"):
            return
        try:
            vertical = self.dock in ("left", "right")
            w, h = self._bar_size(vertical)
            scale = self.devicePixelRatioF()
            self._in_prog_move = True
            self._dock_guard_until = time.time() + 0.8   # 吸附后的连锁判定保护期
            pw, ph = round(w * scale), round(h * scale)
            hwnd = int(self.winId())
            l, t, r_, b_ = monitor_workarea_of(hwnd)
            if self.dock == "right":
                x, y = r_ - pw, t + (b_ - t - ph) // 2
            elif self.dock == "left":
                x, y = l, t + (b_ - t - ph) // 2
            elif self.dock == "top":
                x, y = l + (r_ - l - pw) // 2, t
            else:
                x, y = l + (r_ - l - pw) // 2, b_ - ph
            user32.SetWindowPos(hwnd, None, x, y, pw, ph, 0x0010)  # NOACTIVATE
        except Exception:
            import traceback
            dbg("apply_dock_geometry failed: " + traceback.format_exc()[-200:])
        finally:
            self._in_prog_move = False

    def _set_dock(self, side: str):
        """贴边成胶囊条:宽高按各 label 的 sizeHint 聚合计算,恰好包住文字。
        注意 QWidget 顶层在 QSS border 下 sizeHint() 返回废值(16x2),
        必须手动聚合子控件尺寸,且先填文字再量。"""
        self.dock = side
        sg = self.screen().availableGeometry()
        g = self.geometry()
        vertical = side in ("left", "right")
        self._build_bar(vertical=vertical)
        self.setStyleSheet(self._skin_qss())   # 皮肤化(T2):条形态 → qss_bar
        self._apply_snapshot(self.snap)          # 先填文字
        w, h = self._bar_size(vertical)   # 仅供 _apply_dock_geometry 内部重算,此处不再自设几何
        self._apply_dock_geometry()      # 唯一几何权威(物理坐标,尺寸+位置一次到位)
        self._save_state()               # 形态变化即时落盘,兜强杀/崩溃路径

    def _mk_sep(self, vertical: bool, color: str | None = None) -> QFrame:
        """条内分隔线。color(v0.9 T2):皮肤分隔线色,None=玻璃缺省 C_BORDER
        (零漂移);_build_bar 构造期传 self._skin().sep。"""
        line = QFrame()
        line.setStyleSheet(f"background: {color or C_BORDER}; border: none;")
        if vertical:
            # 宽随定宽派生:BAR_V_W − 左右边距 8×2 − 边框 2 − 两侧呼吸 8×2
            # (116 时代=84;104 时代=72,收窄后随动不再钉死 —— 用户对版
            # 2026-09-28);横条侧竖线(1x14)不动
            line.setFixedSize(self.BAR_V_W - 32, 1)
        else:
            line.setFixedSize(1, 14)
        return line

    def _bar_size(self, vertical: bool) -> tuple[int, int]:
        """遍历条布局内全部控件(label+分隔线)聚合尺寸:
        margins(8,1,8,1) + spacing 6 + 边框 2。
        isHidden() 的控件跳过(v0.5.0):预算段整段隐藏后仍按隐藏 label 计
        会让条宽虚胖、悬空一条分隔线;空文本 QLabel 本身仍占行高,也会撑破
        stress 的『横条高度≤34』断言 —— 所以数据缺席必须走 setVisible(False)
        而非 setText("")。
        v0.8.0 T3/F1:竖条宽度侧不再聚合 —— 直接返回定宽 BAR_V_W(删
        max_w 聚合与 min(...,100) cap);高度侧聚合与跳过 hidden 机制不变,
        横向分支一字不动。"""
        lay = self.layout()
        if lay is None:
            return 60, 20
        sp = 10   # 横条项间距(预览 gap 10,2026-09-28 对版)—— 旧 6 每件差
                  # 4px,~13 件累计 ~50px,条尾『均燃 148.4M/h』被裁(用户截图)
        pad = 2   # 每控件安全余量:中文在 Consolas 回退渲染时 sizeHint 会低估
        if vertical:
            # 边框 2 + 上下边距 16×2 + 4px 总余量 + 项间距 8×(n−1)。
            # 旧式每控件 pad 3 ×~13 项 ≈ 39px 虚高全被首尾 stretch 均分,
            # 上下白 ~35px(用户对版 2026-09-28『还是太远』);高度侧
            # sizeHint 足够准,余量收敛为总量 4(不足由 stretch 先垫)。
            total_h = 38
            n = 0
            for i in range(lay.count()):
                w = lay.itemAt(i).widget()
                if w is None or w.isHidden():
                    continue
                total_h += w.sizeHint().height()
                n += 1
            return self.BAR_V_W, max(total_h + 8 * max(n - 1, 0), 10)
        total_w, max_h = 28 + 2, 0     # 左右边距 14×2(预览 padding 5px 14px)+ 边框
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is None or w.isHidden():
                continue
            hs = w.sizeHint()
            # v0.9 T3:横条 QLabel 钉了玻璃行高(_build_bar 横分支,QLabel.
            # sizeHint() 不随 setFixedHeight 变 —— 实测 Segoe Print 14px 仍报
            # 24),聚合侧须按 maximumHeight 钳才反映真实布局行高;未钉件
            # max==QWIDGETSIZE_MAX(16777215),min 即原 hint,玻璃逐位不变。
            # 『横高≤34 对一切皮肤同上限』由此钳制 + 钉行配对成立
            max_h = max(max_h, min(hs.height(), w.maximumHeight()) + pad)
            total_w += hs.width() + pad + sp
        # 高度:最高子件 + 上下边距 5×2 + 边框 2 + 余量 2(2026-09-28 横条
        # 对版随布局参数同步,旧锚 margins(2,0,2,0))
        return max(total_w - sp, 10), max_h + 10 + 2 + 2

    def _unset_dock(self):
        sg = self.screen().availableGeometry()
        g = self.geometry()
        self.dock = None
        # 同 _detach_to_pointer:重建延后到事件栈外(可能由 settle/菜单
        # 触发,栈上同样有 paint/输入事件对旧控件的引用)
        self._build_card()
        self.setStyleSheet(self._skin_qss())   # 皮肤化(T2):卡形态 → qss
        # 先激活新布局:顶层布局激活时会把布局最小宽写入窗口
        # minimumWidth —— 横条时代的最小宽若未刷新,setGeometry(313)
        # 被钳成条宽(用户『取消贴边卡片变宽』病根,2026-09-28)
        self.layout().activate()
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

    def _mk_lbl(self, text="", cls="dim", font=None, size=None, families=None):
        lb = QLabel(text)
        lb.setObjectName(cls if cls != "normal" else "")
        if font:
            # v0.8.0 T1:等宽族经 mk_mono 单点构造(Cascadia 主+Consolas
            # 回退);其余族(中文 UI/Segoe)维持单族 QFont 构造。
            # size 一律像素(与 mk_mono 同基准,对齐预览 px 字号)。
            # families(v0.9 T2):皮肤等宽字族(None=玻璃缺省),仅作用于
            # font==C_MONO 分支 —— C_MONO 在此是『等宽标记』,字族本体由
            # 皮肤 font_mono_families 提供
            if font == C_MONO:
                lb.setFont(mk_mono(size or 9, families=families))
            else:
                f = QFont(font)
                f.setPixelSize(size or 9)
                lb.setFont(f)
        return lb

    def _build_card(self):
        self._clear()
        # 形态标记(v0.5.0):_apply_snapshot 按 _bar_form 分支 —— 卡片走
        # _apply_card(v0.8.0 新结构),条形态走 T3 重绘后的条分支;
        # 每次重建后与实际标签集合一一对应(F4 属性表卡列,stress 断言锚)
        self._bar_form = None
        # 皮肤参数(v0.9 T2):色参/分隔线/等宽字族构造期注入;玻璃值与
        # 旧缺省逐位相等(qss 逐位断言 + 绘制色镜像断言双兜底)
        sk = self._skin()
        mono = sk.font_mono_families
        root = QVBoxLayout(self)
        # 边距 14/16/12 = 预览 padding 原值(:42)。首版用 8 是给 13pt 字号
        # 的列宽预算让路;字号 px 化后文本窄回预览宽度,边距回归原值
        # (视觉对版 2026-09-28),grid 列宽预算随内容宽 281 重算。
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(6)

        # ---- v0.8.0 T5+T2 卡片结构(预览 .g-card :145-191,自上而下) ----
        # ① 状态行:PulseIndicator(14)+状态文本(elapsed 并入:idle『空闲』/
        # generating『生成中 Ns』)+右侧当前模型名。会话标题不再占行(F3
        # 裁决):完整标题+📌 前缀进窗口 setToolTip,_apply_card 维护。
        head = QHBoxLayout()
        head.setSpacing(SP["s"])
        self.dot = PulseIndicator(14, idle_color=sk.pulse_idle,
                                  active_color=sk.pulse_active,
                                  core_color=sk.pulse_core)   # 皮肤色(T2)
        head.addWidget(self.dot)
        self.state_lbl = self._mk_lbl("空闲", "dim", "Microsoft YaHei UI", 11)
        head.addWidget(self.state_lbl)
        head.addStretch(1)
        # 模型名超宽 elide 由 _apply_card 用 QFontMetrics 钳宽完成(布局侧
        # 只给它 Ignored 水平策略,防长名反推撑宽卡片)
        self.model_name_lbl = self._mk_lbl("", "faint", "Microsoft YaHei UI", 10)
        head.addWidget(self.model_name_lbl)
        root.addLayout(head)

        # ② 主数字行:速度 30pt Bold 蓝 + 单位 11pt faint + Sparkline(stretch,
        # min 72x24,吃满行剩余宽 —— 用户反馈上半右侧空白 2026-09-28,速度
        # 趋势拉通填充;预览 :150-157 的 72px 定宽升级为弹性)。旧 est_lbl
        # (『~估算』)删除:流式估算改为速度文本的 ~ 前缀(README『速度带~=
        # 流式估算』口径);旧 elapsed_lbl 并入①状态文本。单位拆独立 label
        # 是为 30pt/11pt 双字号共存(sizeHint 可测,stress 可断言)—— 不入
        # F4 属性表但遵守同款 None 纪律。
        big = QHBoxLayout()
        big.setSpacing(SP["s"])
        self.tps_lbl = self._mk_lbl("--", "accent", C_MONO, 30, families=mono)
        self.tps_lbl.setFont(mk_mono(30, QFont.Bold, families=mono))
        big.addWidget(self.tps_lbl)
        self.tps_unit_lbl = self._mk_lbl("tok/s", "faint", C_MONO, 11,
                                         families=mono)
        big.addWidget(self.tps_unit_lbl, 0, Qt.AlignBottom)
        self.spark = SparklineWidget(72, 24, stretch=True,
                                     line_color=sk.spark_line,
                                     dot_color=sk.spark_dot)
        big.addWidget(self.spark, 0, Qt.AlignBottom)
        root.addLayout(big)

        # ③ 今日 hero 三段 baseline:fmt_k 19pt Bold 白.9(C_FG)/金额 13pt
        # 白.55 600(DemiBold,140 档)/『今日』10pt faint(89 档)。
        # 三段连排左对齐(预览 .g-today :159-163:n/c/t 顺排无 stretch,
        # 视觉对版 2026-09-28 修正 —— 首版把『今日』甩到右端是旧版遗留)。
        # 金额段 cost=0 整段隐藏、partial ≈ 前缀 —— 口径逐字沿 v0.7 实现。
        # 多源拆分并入本行右端(dim 9px,右对齐,Ignored+elide 防撑宽):
        # 用户反馈卡片上半右侧空白(2026-09-28),此行上移消化一行高度。
        today = QHBoxLayout()
        today.setSpacing(SP["s"])
        self.today_lbl = self._mk_lbl("--", "normal", C_MONO, 19, families=mono)
        self.today_lbl.setFont(mk_mono(19, QFont.Bold, families=mono))
        today.addWidget(self.today_lbl)
        self.today_cost_lbl = self._mk_lbl("", "dim", C_MONO, 13, families=mono)
        self.today_cost_lbl.setFont(mk_mono(13, QFont.DemiBold, families=mono))
        today.addWidget(self.today_cost_lbl)
        today.addWidget(self._mk_lbl("今日", "faint", "Microsoft YaHei UI", 10))
        self.today_src_lbl = self._mk_lbl("", "dim", "Microsoft YaHei UI", 9)
        self.today_src_lbl.setSizePolicy(QSizePolicy.Policy.Ignored,
                                         QSizePolicy.Policy.Fixed)
        self.today_src_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        today.addWidget(self.today_src_lbl, 1)
        root.addLayout(today)

        # ④ 套餐 section 容器化(评审钉死):分节线+RingWidget46+右信息三行
        # 包进同一 QWidget,_plan_pct None 时整组 setVisible(False) —— 分节
        # 线随段隐藏不悬空(旧实现分节线恒显,数据缺席时孤线漂浮)。
        self.plan_section = QWidget()
        pv = QVBoxLayout(self.plan_section)
        pv.setContentsMargins(0, 8, 0, 0)
        pv.setSpacing(4)
        plan_line = self._mk_card_sep(sk.sep_card)   # 皮肤分节线(T2)
        pv.addWidget(plan_line)
        prow = QHBoxLayout()
        prow.setSpacing(SP["m"])
        self.plan_ring = RingWidget(46, base_color=sk.ring_base)   # 中心 N% 11pt tier 色
        prow.addWidget(self.plan_ring)
        info = QVBoxLayout()
        info.setSpacing(0)
        self.plan_cap_lbl = self._mk_lbl("套餐剩余", "faint",
                                         "Microsoft YaHei UI", 9)
        info.addWidget(self.plan_cap_lbl)
        self.plan_tok_lbl = self._mk_lbl("—", "normal", C_MONO, 14,
                                         families=mono)
        self.plan_tok_lbl.setFont(mk_mono(14, QFont.DemiBold, families=mono))
        info.addWidget(self.plan_tok_lbl)
        self.plan_sub_lbl = self._mk_lbl("", "faint", "Microsoft YaHei UI", 10)
        info.addWidget(self.plan_sub_lbl)
        prow.addLayout(info, 1)
        # 右端:预算余量『还可撑 X h』(v0.8 降 tooltip 的 est_hours_left 恢复
        # 上卡 —— 用户反馈套餐行右侧空白 2026-09-28;该数据本属套餐语境,
        # F3 当时的取舍是密度让位,空间释放后回归)。est None 隐藏;≤0『超支』
        # 红档。k/v 两行右对齐,与左 info 的 k/v 结构呼应。
        plan_right = QVBoxLayout()
        plan_right.setSpacing(0)
        self.plan_left_k = self._mk_lbl("还可撑", "faint",
                                        "Microsoft YaHei UI", 9)
        self.plan_left_k.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        plan_right.addWidget(self.plan_left_k)
        self.plan_left_v = self._mk_lbl("", "soft", C_MONO, 13, families=mono)
        self.plan_left_v.setFont(mk_mono(13, QFont.DemiBold, families=mono))
        self.plan_left_v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        plan_right.addWidget(self.plan_left_v)
        prow.addLayout(plan_right)
        pv.addLayout(prow)
        root.addWidget(self.plan_section)
        # plan_lbl(F4 属性表:三形态恒建):卡形态的 % 由 ring 中心呈现,本
        # label 不进布局、保持隐藏,但文本/颜色同步刷新 —— 横竖条(T3)以
        # 它为 % 文本载体,置 None 会让形态循环摸已销毁对象;不设 parent、
        # 不 addWidget,重建时随 Python 引用释放,不泄漏进 findChildren。
        self.plan_lbl = self._mk_lbl("", "warn", C_MONO, 13, families=mono)
        self.plan_lbl.hide()

        # ⑤ grid 六格 3 列(预览 :179-185):入/出、缓存命中、⏱首/总、燃速、
        # 均燃、均速;k 9pt faint(89)、v 13pt soft(204,N2 档)。六格结构
        # 恒建恒显,缺参 -- (与旧卡 timing 缺参同语义,不做整行隐藏)。
        # 列宽固定(CARD_GRID_COL_W 宽度策略):v label 水平 Ignored,文本
        # 由 _apply_card 按列宽 elide —— 不固定会被长文本反推出 372px。
        root.addWidget(self._mk_card_sep(sk.sep_card))   # 皮肤分节线(T2)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)  # 视觉对版:预览列 gap 12 在 313 宽内放不下,8 为折中
        grid.setVerticalSpacing(4)    # 预览 k→v margin 1px+行内自然距的等效折中(原 2 过挤)
        for c, cw in enumerate(self.CARD_GRID_COL_W):
            grid.setColumnMinimumWidth(c, cw)

        # 液态玻璃皮肤例外:六药丸等宽网格,文字全部居中(用户 2026-09-29
        # 『六药丸等宽、文字居中』)—— v 值在此处定对齐,玻璃与其它皮肤
        # 左对齐不变;末列右对齐的分支随后按 _center_grid 分派
        _center_grid = (sk.id == "liquid")

        def cell(txt, cls=""):
            lb = self._mk_lbl("--", cls, C_MONO, 13, families=mono)
            lb.setFont(mk_mono(13, QFont.DemiBold, families=mono))
            lb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            if _center_grid:
                lb.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
            return lb

        self.in_out_lbl = cell("--")
        self.rate_lbl = cell("--")
        self.timing_lbl = cell("-- / --")
        self.burn_lbl = cell("--")
        self.avg_burn_lbl = cell("--")
        self.avg_lbl = cell("--")
        # 末列(⏱首/总、均速)值右对齐:下半部右缘与上半部(sparkline 右端/
        # 多源/预算余量,均 297)共线 —— 用户反馈『下半右侧往右放一点,卡片
        # 上下左右视觉对齐』(2026-09-28);k 行同步右对齐(用户反馈『标题和
        # 数值开头对齐』:值右对齐后其开头浮动,列头仍贴左则错位 —— 两行同
        # 右缘即天然对齐)。液态玻璃皮肤例外:六药丸等宽网格,文字全部居中
        # (用户 2026-09-29『六药丸等宽、文字居中』)—— 玻璃与其它皮肤维持
        # 上面的对齐语言
        for lb in (self.timing_lbl, self.avg_lbl):
            lb.setAlignment((Qt.AlignHCenter if _center_grid
                             else Qt.AlignRight) | Qt.AlignVCenter)
        for col, (k, v) in enumerate((
                ("入 / 出", self.in_out_lbl), ("缓存命中", self.rate_lbl),
                ("首 / 总", self.timing_lbl), ("燃速", self.burn_lbl),
                ("均燃", self.avg_burn_lbl), ("均速", self.avg_lbl))):
            # k/v 行号必须按『第几对』展开成 4 行(0=k1,1=v1,2=k2,3=v2)。
            # v0.8.0 首版误写 col//3(+1):第二组键(燃速/均燃/均速)与第一组
            # 值(入出/命中/首总)同落 row1 同格叠印 —— 真机『文字重叠』
            # 的真因(2026-09-28 用户截图+findChildren 几何转储定位:燃速
            # g=(16,207) 与 in_out 值全等),此前误诊为列宽/elide 问题。
            k_lb = self._mk_lbl(k, "faint", "Microsoft YaHei UI", 9)
            if _center_grid:
                k_lb.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
            elif v in (self.timing_lbl, self.avg_lbl):
                k_lb.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(k_lb, (col // 3) * 2, col % 3)
            grid.addWidget(v, (col // 3) * 2 + 1, col % 3)
            # k label 引用入列表(liquid 药丸需要罩住标题 —— 第十轮真因:
            # 标题 label 从未存引用,药丸几何无从包含它)
            if col == 0:
                self._grid_k_labels = []
            self._grid_k_labels.append(k_lb)
        root.addLayout(grid)

        # ⑥ 模型列表 rows[:4](预览 :187-190):容器+每行 HBox(名左 10pt
        # faint 89 档/速度右 10pt half 128 档),替换 v0.7 单 QLabel 多行
        # join —— 独立 label 才能左右分栏且行内距受布局 spacing 管。
        # 四行结构恒建、按数据显隐(_apply_card),rows 不足 4 不撑高。
        # ⑥ 模型列表 rows[:4](预览 .g-models :93-96/.m :94-96):容器+每行
        # HBox(名左/速度右)+分隔线入容器。视觉对版(2026-09-28)按预览补:
        # - margin-top:10 → 分节线距 grid 10px(root spacing 6 + 容器上边距 4)
        # - padding-top:8 → 线距首行 8px(addSpacing)
        # - .m line-height:1.8 → 18px 行节奏(10px 字自然行高 ~13 + 行距 5)
        # - 速度 b:600 字重(DemiBold)
        # 名 10px faint 89 档/速度右 10px half 128 档;四行结构恒建、按数据
        # 显隐(_apply_card),rows 不足 4 不撑高。
        self.model_rows = QWidget()
        mv = QVBoxLayout(self.model_rows)
        mv.setContentsMargins(0, 4, 0, 0)
        mv.setSpacing(0)
        # 模型行弱分节线:玻璃 rgba(255,255,255,0.05) 逐位不变;皮肤走
        # sep_card_weak 弱档(sep_card 主档 0.07 的姊妹键,T2 骨架期补充)
        mv.addWidget(self._mk_card_sep(sk.sep_card_weak))
        mv.addSpacing(8)
        self._model_rows_items = []          # [(name_lbl, tps_lbl)]×4,随容器重建
        for _i in range(4):
            if _i:
                mv.addSpacing(5)
            row = QHBoxLayout()
            row.setSpacing(SP["s"])
            nm = self._mk_lbl("", "faint", "Microsoft YaHei UI", 10)
            row.addWidget(nm, 1)
            sp = self._mk_lbl("", "half", C_MONO, 10, families=mono)
            sp.setFont(mk_mono(10, QFont.DemiBold, families=mono))
            row.addWidget(sp)
            mv.addLayout(row)
            self._model_rows_items.append((nm, sp))
        root.addWidget(self.model_rows)

        # ---- 尾部 None 纪律(F4 属性表卡列):本形态不创建的 label 显式
        # 置 None —— 残留上次布局的已销毁对象引用会让条形态分支摸炸
        # (v0.4.0 压力循环教训,test_stress 文件头记载) ----
        self.in_lbl = self.out_lbl = None    # 卡用 in_out_lbl;out 并入其文本
        self.title_lbl = self.est_lbl = self.elapsed_lbl = None
        self.cache_lbl = self.ttft_lbl = self.dur_lbl = None
        self.model_lbl = None                # 已由 model_rows 容器替代(v0.7→v0.8)
        # v0.8.0 T3 条形态件(F4 属性表):卡形态不建,同点显式置 None
        self.plan_cd_lbl = None              # 横建(倒计时独立段)
        self.vbar_in_cap = self.vbar_burn_cap = None   # 竖建(入出/燃速组 k 行)
        self.sep_plan = self.sep_burn = self.sep_today = None      # 横建
        self.vsep_plan = self.vsep_burn = self.vsep_today = self.vsep_in = None  # 竖建

    def _mk_card_sep(self, rgba: str | None = None) -> QFrame:
        """卡片主分节线(v0.8.0 T5+T2):rgba 白内联字面(预览 .07/.05 两档)。
        内联样式自诞生即渲染(D4 裁决先例),零 QSS/objectName 依赖 —— 与
        _mk_sep 的内联机制同款;不进 F4 属性表(卡侧恒建,无跨形态引用,
        ④的分节线随 plan_section 容器整组隐藏)。
        rgba(v0.9 T2):皮肤分节线色,None=玻璃缺省 .07 字面(零漂移)。"""
        line = QFrame()
        line.setStyleSheet(
            f"background: {rgba or 'rgba(255,255,255,0.07)'}; "
            "border: none; max-height: 1px;")
        return line

    def _build_bar(self, vertical: bool = False):
        """v0.8.0 T3 横竖条重绘(spec=design-preview-abd.html :222-237/:258-274)。

        横条段序钉死:脉冲环10|速度 14pt Bold 蓝+『t/s』|sparkline 44x16|
        sep_plan|套餐段(RingWidget12 实画小环 + plan_lbl『N% ~X』tier 色
        600 + plan_cd_lbl 倒计时 dim)|sep_today|今日段|sep_burn|燃速段。
        旧 avg/ttft/dur/in/out/rate/elapsed 段全删(F4:横竖条 None)。
        竖条全 AlignHCenter 分组钉死:脉冲环12|速度 24pt Bold 蓝|『TOK/S』|
        sparkline 60x14|四 vsep 分隔的今日/套餐/燃速/入出四组(v=vstrong
        217 档,k=faint 89 档 9pt);定宽 BAR_V_W=116,水平边距 8(N1)。
        分隔线命名=其【后】段/组(与横条 sep 约定一致,F4 属性表唯一权威):
        段/组数据缺席时其前 sep/vsep 一并隐藏,不悬空 —— spec 段序行内的
        vsep 名字有一处错位,以属性表为准修正(今日组前=vsep_today 恒显、
        套餐组前=vsep_plan 跟套餐、燃速组前=vsep_burn 跟燃速、入出组前=
        vsep_in 恒显)。
        v0.9 T2:皮肤参数构造期注入 —— 分隔线色 skin.sep、脉冲环/折线/环
        底色、等宽字族 skin.font_mono_families;玻璃值与旧缺省逐位相等。"""
        self._clear()
        sk = self._skin()
        mono = sk.font_mono_families
        root = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        if vertical:
            # 预览 .g-vbar padding 16px 8px(:115):左右 8(N1 同旧)、上下
            # 16 —— 旧 0+双 stretch 让圆点贴顶边(用户对版 2026-09-28);
            # stretch 保留:条高富余时仍垂直居中,贴边态则 16px 呼吸
            root.setContentsMargins(8, 16, 8, 16)
        else:
            # 预览 .g-bar padding 5px 14px(:102):上下 5、左右 14(旧 2/0
            # 收窄版无呼吸,用户对版 2026-09-28)
            root.setContentsMargins(14, 5, 14, 5)
        root.setSpacing(8 if vertical else 10)   # 预览 gap:竖条 8、横条 10
        self._bar_form = "v" if vertical else "h"
        # 本形态的段开关(v0.8.0 对版期『靠边停放自定义显示内容』):速度+
        # 圆点恒显不进清单;未选段不构建(F4 None 纪律:对应该件显式置 None,
        # _apply_snapshot 的消费分支按 seg_on 短路)
        segs = self.bar_segments.get(self._bar_form,
                                     list(BAR_SEGMENTS_H if not vertical
                                          else BAR_SEGMENTS_V))

        def seg_on(key: str) -> bool:
            return key in segs

        if vertical:
            root.addStretch(1)   # 首尾对称弹性:条高富余时内容整体垂直居中(用户要求)
        if not vertical:
            self.dot = PulseIndicator(10, idle_color=sk.pulse_idle,
                                      active_color=sk.pulse_active,
                                      core_color=sk.pulse_core)
            root.addWidget(self.dot)
            self.tps_lbl = self._mk_lbl("--", "accent", C_MONO, 14,
                                        families=mono)
            self.tps_lbl.setFont(mk_mono(14, QFont.Bold, families=mono))
            root.addWidget(self.tps_lbl)
            # 单位与速度分色分号(预览 :232 spd 蓝 14 与 dim t/s 12 分离;
            # 旧单 label 全蓝 14。文本恒 "t/s",_apply_snapshot 只更新数字)
            self.tps_unit_lbl = self._mk_lbl("t/s", "dim", C_MONO, 12,
                                             families=mono)
            root.addWidget(self.tps_unit_lbl)
            if seg_on("spark"):
                self.spark = SparklineWidget(44, 16, line_color=sk.spark_line,
                                             dot_color=sk.spark_dot)
                root.addWidget(self.spark)
            self.sep_plan = (self._mk_sep(False, sk.sep)
                             if seg_on("plan") else None)
            if self.sep_plan is not None:
                root.addWidget(self.sep_plan)
            # 套餐段:实画小环(RingWidget12,不用 ⊙ 字形 —— Cascadia 无该
            # 字形保证,风险表引 ⏱ 字体合并先例 CHANGELOG v0.7:12)。
            # 段文字 9→12px(预览 .g-bar 基准 12,用户对版 2026-09-28)
            if seg_on("plan"):
                self.plan_ring = RingWidget(12, center_text=False,
                                            base_color=sk.ring_base)
                root.addWidget(self.plan_ring)
                self.plan_lbl = self._mk_lbl("", "warn", C_MONO, 12,
                                             families=mono)
                self.plan_lbl.setFont(mk_mono(12, QFont.DemiBold, families=mono))   # 600(tier 色由 _apply_snapshot 注入)
                root.addWidget(self.plan_lbl)
            self.plan_cd_lbl = (self._mk_lbl("", "dim", C_MONO, 12,
                                             families=mono)
                                if seg_on("cd") else None)
            if self.plan_cd_lbl is not None:
                root.addWidget(self.plan_cd_lbl)
            # 今日段:『今X』+金额段 f" ≈¥N"(金额取整;cost=0 省段/N3、
            # partial ≈ 前缀 —— 与卡片同守卫,由 _apply_snapshot 拼装)
            self.sep_today = (self._mk_sep(False, sk.sep)
                              if seg_on("today") else None)
            if self.sep_today is not None:
                root.addWidget(self.sep_today)
                self.today_lbl = self._mk_lbl("", "dim", C_MONO, 12,
                                              families=mono)
                root.addWidget(self.today_lbl)
            self.sep_burn = (self._mk_sep(False, sk.sep)
                             if seg_on("burn") else None)
            if self.sep_burn is not None:
                root.addWidget(self.sep_burn)
                # 燃速段:瞬时优先口径不变(dim),文案由 _apply_snapshot 拼装
                self.burn_lbl = self._mk_lbl("", "dim", C_MONO, 12,
                                             families=mono)
                root.addWidget(self.burn_lbl)
            # ---- 竖条专属件置 None(F4 横列) ----
            self.plan_sub_lbl = None
            self.in_lbl = None
            self.vbar_in_cap = self.vbar_burn_cap = None
            self.vsep_plan = self.vsep_burn = self.vsep_today = self.vsep_in = None
            # ---- 横条关段件置 None(F4 横列;全开时与旧版逐位一致) ----
            if not seg_on("spark"):
                self.spark = None
            if not seg_on("plan"):
                self.plan_ring = self.plan_lbl = None
            if not seg_on("cd"):
                self.plan_cd_lbl = None
            if not seg_on("today"):
                self.today_lbl = None
            if not seg_on("burn"):
                self.burn_lbl = None
            # ---- v0.9 T3:横条行高钉玻璃基准(对一切皮肤同上限) ----
            # 高瘦字族(Segoe Print 14px 行高 24 vs 玻璃 Cascadia 16,黑板
            # 粉笔;多出的 8px 是内部 leading)会把 _bar_size 的 max_h 抬到
            # 40,破『横高≤34』。行盒统一钳在玻璃缺省字族行高(14px→16/
            # 12px→14,families=None 即玻璃基准,随 mk_mono 缺省自演进):
            # 墨迹实测 Segoe Print 14px ≤13/12px ≤12,行盒余量(5/4px)吃
            # 得下,不裁字形。QLabel.sizeHint() 不随 setFixedHeight 变
            # (实测),_bar_size 的 maximumHeight 钳与本案配对生效;QFrame
            # 分隔线/dot/spark/ring 非 QLabel,天然不被波及
            for i in range(root.count()):
                wd = root.itemAt(i).widget()
                if isinstance(wd, QLabel):
                    _f = wd.font()
                    wd.setFixedHeight(
                        QFontMetrics(mk_mono(_f.pixelSize(), _f.weight())).height())
        else:
            def vnum(txt="", cls="", size=14):
                # .cell .v 600 字重(预览 :126 font-weight:600)—— 旧默认
                # 常规字重,用户对版 2026-09-28
                lb = self._mk_lbl(txt, cls, C_MONO, size, families=mono)
                lb.setFont(mk_mono(size, QFont.DemiBold, families=mono))
                lb.setAlignment(Qt.AlignHCenter)
                return lb

            def vcap(txt, mono_flag=False):
                # k 行/caption:faint 89 档 9px;TOK/S 帽标 letter-spacing
                # 1px(预览 :123),cell .k 无字距;中文走 YaHei、纯 ASCII
                # 走 mono,中文不列进 mk_mono 的 families 才不抬成首选
                # (形参改名 mono_flag,T2:避免与外层皮肤字族变量 mono 撞名)
                lb = self._mk_lbl(txt, "faint",
                                  C_MONO if mono_flag else "Microsoft YaHei UI",
                                  9, families=mono)
                if mono_flag:
                    f = lb.font()   # PySide6 font() 返回副本,须 set 回
                    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
                    lb.setFont(f)
                lb.setAlignment(Qt.AlignHCenter)
                return lb

            # 组1 主数字:脉冲环+速度+单位+sparkline
            self.dot = PulseIndicator(12, idle_color=sk.pulse_idle,
                                      active_color=sk.pulse_active,
                                      core_color=sk.pulse_core)
            root.addWidget(self.dot, 0, Qt.AlignHCenter)
            self.tps_lbl = self._mk_lbl("--", "accent", C_MONO, 24,
                                        families=mono)
            self.tps_lbl.setFont(mk_mono(24, QFont.Bold, families=mono))
            self.tps_lbl.setAlignment(Qt.AlignHCenter)
            root.addWidget(self.tps_lbl, 0, Qt.AlignHCenter)
            root.addWidget(vcap("TOK/S", mono_flag=True), 0, Qt.AlignHCenter)
            if seg_on("spark"):
                self.spark = SparklineWidget(60, 14, line_color=sk.spark_line,
                                             dot_color=sk.spark_dot)
                root.addWidget(self.spark, 0, Qt.AlignHCenter)
            # 组2 今日(v=fmt_k vstrong;vsep_today 恒显语义随段开关退役:
            # 段可关后 sep 跟段,不再恒建)
            self.vsep_today = (self._mk_sep(True, sk.sep)
                               if seg_on("today") else None)
            if self.vsep_today is not None:
                root.addWidget(self.vsep_today, 0, Qt.AlignHCenter)
                self.today_lbl = vnum("--", "vstrong")
                root.addWidget(self.today_lbl, 0, Qt.AlignHCenter)
                root.addWidget(vcap("今日"), 0, Qt.AlignHCenter)
            # 组3 套餐:v=N% tier 色、k=[~lt, cd]『 · 』join(空列表→置空但
            # 组结构保留,v 行仍显 —— N3 缺段自然省略同现状口径明文化)
            self.vsep_plan = (self._mk_sep(True, sk.sep)
                              if seg_on("plan") else None)
            if self.vsep_plan is not None:
                root.addWidget(self.vsep_plan, 0, Qt.AlignHCenter)
                self.plan_lbl = vnum("", "warn")   # tier 色由 _apply_snapshot 注入
                root.addWidget(self.plan_lbl, 0, Qt.AlignHCenter)
                self.plan_sub_lbl = vcap("")
                root.addWidget(self.plan_sub_lbl, 0, Qt.AlignHCenter)
            # 组4 燃速:v=『296M/h』式 vstrong、k=『燃速 · 均137』式含均燃
            self.vsep_burn = (self._mk_sep(True, sk.sep)
                              if seg_on("burn") else None)
            if self.vsep_burn is not None:
                root.addWidget(self.vsep_burn, 0, Qt.AlignHCenter)
                self.burn_lbl = vnum("", "vstrong")
                root.addWidget(self.burn_lbl, 0, Qt.AlignHCenter)
                self.vbar_burn_cap = vcap("燃速")
                root.addWidget(self.vbar_burn_cap, 0, Qt.AlignHCenter)
            # 组5 入出:入 v 行 + 出并入 k 行(out_lbl 三形态 None)
            self.vsep_in = (self._mk_sep(True, sk.sep)
                            if seg_on("in") else None)
            if self.vsep_in is not None:
                root.addWidget(self.vsep_in, 0, Qt.AlignHCenter)
                self.in_lbl = vnum("--", "vstrong")
                root.addWidget(self.in_lbl, 0, Qt.AlignHCenter)
                self.vbar_in_cap = vcap("入 · 出--")
                root.addWidget(self.vbar_in_cap, 0, Qt.AlignHCenter)
            root.addStretch(1)   # 保留:与顶部 stretch 对称,内容垂直居中
            # ---- 横条专属件置 None(F4 竖列) ----
            self.plan_ring = None
            self.plan_cd_lbl = None
            self.tps_unit_lbl = None    # 竖条单位走『TOK/S』caption
            self.sep_plan = self.sep_burn = self.sep_today = None
            # ---- 竖条关段件置 None(F4 竖列;全开时与旧版逐位一致) ----
            if not seg_on("spark"):
                self.spark = None
            if not seg_on("today"):
                self.today_lbl = None
            if not seg_on("plan"):
                self.plan_lbl = self.plan_sub_lbl = None
            if not seg_on("burn"):
                self.burn_lbl = self.vbar_burn_cap = None
            if not seg_on("in"):
                self.in_lbl = self.vbar_in_cap = None
        # ---- 两形态共通(F4 修正版属性总表,唯一权威)----
        # 卡建件两种条形态都不建;三形态全 None 的已删件(title/est/elapsed/
        # cache/ttft/dur/out)与卡建横竖 None 件(avg/rate)在此显式置 None ——
        # 漏一处即形态循环摸已销毁对象(v0.4.0 教训,test_stress 文件头)
        self.state_lbl = self.model_lbl = self.est_lbl = self.cache_lbl = None
        self.title_lbl = self.elapsed_lbl = self.ttft_lbl = self.dur_lbl = None
        self.out_lbl = None               # 出量并入 vbar_in_cap 文本(F4:三形态 None)
        self.today_src_lbl = self.timing_lbl = None
        self.plan_left_k = self.plan_left_v = None
        self.avg_lbl = self.rate_lbl = None
        self.plan_section = None
        self.plan_cap_lbl = self.plan_tok_lbl = None
        self.in_out_lbl = self.avg_burn_lbl = None
        self.model_rows = None
        self.model_name_lbl = None
        self.today_cost_lbl = None
        self._model_rows_items = []

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
        self._add_segment_menu(m)
        self._add_skin_menu(m)
        self._add_session_menu(m)
        m.addAction("历史用量图表", self._open_history)
        m.addAction("设置", self._open_settings)
        m.addAction("导出 CSV", self._export_csv)
        m.addAction("复制今日摘要", self._copy_today_summary)
        if self.tray is not None:          # 无托盘环境不提供收起,防"收起后找不回"
            m.addAction("收起到托盘", self.hide)
        m.addSeparator()
        m.addAction("退出", QApplication.quit)
        m.exec(pos)

    def _add_segment_menu(self, m: QMenu):
        """「显示内容」子菜单(v0.8.0 对版期,用户『靠边停放自定义显示内容』):
        按当前形态列出可勾选段(横=顶/底共用、竖=左/右共用),勾选即时重建
        条 + save_config 持久化。速度+圆点恒显不进清单(全关=纯速度胶囊,
        合法形态)。"""
        seg_menu = m.addMenu("显示内容")
        seg_menu.setStyleSheet(m.styleSheet())
        form = "v" if self._bar_form == "v" else "h"
        labels = BAR_SEGMENT_LABELS_V if form == "v" else BAR_SEGMENT_LABELS_H
        on = self.bar_segments.get(form) or []

        def toggle(key: str, checked: bool):
            segs = list(self.bar_segments.get(form) or [])
            if checked and key not in segs:
                segs.append(key)
            elif not checked and key in segs:
                segs.remove(key)
            self.bar_segments[form] = segs
            # 重征形态:贴边态重建条并按可见件重算几何;未贴边(菜单从
            # 卡片打开)仅落盘,下次贴边生效
            if self.dock:
                self._build_bar(vertical=self.dock in ("left", "right"))
                self.setStyleSheet(self._skin_qss())   # 皮肤化(T2):qss_bar
                self._apply_dock_geometry()
                self._apply_snapshot(self.snap)
            self._save_config_segments()

        for key in (BAR_SEGMENTS_V if form == "v" else BAR_SEGMENTS_H):
            act = seg_menu.addAction(labels[key])
            act.setCheckable(True)
            act.setChecked(key in on)
            act.triggered.connect(lambda checked, k=key: toggle(k, checked))

    def _add_skin_menu(self, m: QMenu):
        """「皮肤」子菜单(v0.9 T3,插在「显示内容」之后):九款单选 ——
        QActionGroup exclusive 保证勾态恒唯一,当前项打勾,点选经 _apply_skin
        即时重建当前形态并 save_config 持久化。九项文案取 skins 注册表的
        menu_label(glass 项文案即『玻璃仪表(默认)』—— 玻璃是可逆性的必需
        入口,非第 9 款皮肤);键序按 skins.SKIN_IDS(glass 在第 0 位,与
        注册表键序同源,skins.py:303 注释钉死 T3 按此序出项)。"""
        skin_menu = m.addMenu("皮肤")
        skin_menu.setStyleSheet(m.styleSheet())
        # 组挂子菜单为父(不挂 self):子菜单每次右键重建,组随菜单销毁,
        # 不在窗口上逐次累积 QActionGroup 对象
        group = QActionGroup(skin_menu)      # exclusive:九项互斥,勾态唯一
        group.setExclusive(True)
        for sid in skins.SKIN_IDS:
            act = skin_menu.addAction(skins.REGISTRY[sid].menu_label)
            act.setCheckable(True)
            act.setChecked(sid == self.skin_id)
            group.addAction(act)
            act.triggered.connect(
                lambda checked=False, s=sid: self._apply_skin(s))

    def _save_config_segments(self):
        """bar_segments 并入 zm_config.json(原子写路径复用 save_config):
        读-改-写,其它键(quota key/budget/alert)原样保留 —— 与设置窗
        同一条落盘纪律(key 明文不进日志)。_no_persist 守卫下静默跳过
        (测试/自检环境)。"""
        cfg = load_config()
        cfg["bar_segments"] = dict(self.bar_segments)
        save_config(cfg)

    def _save_config_skin(self):
        """skin 并入 zm_config.json(v0.9 T3,原子写路径复用 save_config,
        镜像 _save_config_segments 的读-改-写):其它键原样保留;选玻璃时由
        数据层可选键纪律整键省略(glass=缺省语义,data_engine save_config
        承担)。ZM_NO_STATE/--verify 守卫由 save_config 默认路径内建承担 ——
        不得绕过守卫直写文件(v0.5.0『用户以为改了实际没改』红线)。"""
        cfg = load_config()
        cfg["skin"] = self.skin_id
        save_config(cfg)

    # ---- v0.9 T3:皮肤切换(九款即时重建;persist=False 供测试/渲染矩阵) ----
    def _apply_skin(self, skin_id: str, persist: bool = True):
        """切换皮肤并按当前形态即时重建(卡片/横竖条通用入口,右键「皮肤」
        子菜单与回归测试/渲染矩阵共用同一条产品路径):
        - 白名单外静默忽略(不当错误):配置层 _norm_skin 已保证存档值合法,
          这里是第二道闸,防编程误用把 UI 打炸;
        - 未贴边(卡):复刻 _unset_dock 的『先 activate 再 setGeometry』
          纪律 —— 条形态时代的布局最小宽若不刷新,setGeometry(313) 会被
          钳成条宽,之后没人再缩回(用户『取消贴边卡片变宽』同款病根);
        - 贴边(条):复刻段开关的重征形态路径(_build_bar → QSS →
          _apply_dock_geometry → _apply_snapshot),几何权威不旁路;
        - persist=True 时 _save_config_skin 落盘(ZM_NO_STATE/--verify 守卫
          由 save_config 默认路径内建);persist=False 零落盘副作用。
        形态本身不变:切皮肤不改变贴边状态,只重建当前形态的件与样式。"""
        if skin_id not in skins.SKIN_IDS:
            return
        prev = self.skin_id
        self.skin_id = skin_id
        # Liquid Glass 方案 A:液态玻璃皮肤开 Windows 亚克力(真背景模糊),
        # 离开该皮肤关掉恢复普通不透明窗口。失败静默(deco 自绘亮幕兜底)
        if self.isVisible():
            if skin_id == "liquid" and prev != "liquid":
                self._enable_acrylic()
            elif prev == "liquid" and skin_id != "liquid":
                self._disable_acrylic()
        if self.dock:
            self._build_bar(vertical=self.dock in ("left", "right"))
            self.setStyleSheet(self._skin_qss())
            self._apply_dock_geometry()
            self._apply_snapshot(self.snap)
        else:
            sg = self.screen().availableGeometry()
            g = self.geometry()
            self._build_card()
            self.setStyleSheet(self._skin_qss())
            # 先激活新布局再 setGeometry:防最小宽钳制(_unset_dock 同款)
            self.layout().activate()
            x = max(min(g.left(), sg.right() - self.CARD_W - 8), sg.left() + 8)
            y = max(min(g.top(), sg.bottom() - self.CARD_H - 8), sg.top() + 8)
            self.setGeometry(x, y, self.CARD_W, self.CARD_H)
            self._apply_snapshot(self.snap)
        if persist:
            self._save_config_skin()

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

    # ---- 导出 CSV / 复制今日摘要(菜单在 _popup_menu『设置』之后) ----
    def _export_csv(self):
        """右键『导出 CSV』:近 30 天 date×模型明细平铺写 zm_usage_export.csv。
        encoding='utf-8-sig'(带 BOM)—— Excel 对无 BOM 的 UTF-8 按 ANSI 猜
        编码,中文模型名/日期直接乱码;newline='' 是 csv.writer 的官方要求,
        缺席时 Windows 下 writer 的 \r\n 之上再叠一层 CRLF,Excel 里每行尾多
        出空行(验收项『列不错位』的一半坑在这)。行粒度=date×model 平铺、
        刻意不插合计行:partial 日合计与普通行混在同一文件里,读者在表格里
        二次求和时会被当普通行重复加。OSError(exe 目录只读/文件被 Excel
        占用)只气泡+dbg 不抛 —— 菜单槽内异常会被 Qt 静默吞掉,用户什么都
        看不到反而像『点了没反应』。"""
        rows = self.eng.fetch_daily_model_usage(30)
        try:
            with open(EXPORT_PATH, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(("date", "model", "in", "cache_read", "out",
                            "total", "cny", "partial"))
                for d, model, i_, c_, o_, cny, partial in rows:
                    # total=in+out:input_tokens 已含 cache_read(口径红线),
                    # 切勿 in+cache+out 双计;partial 落 0/1(表格软件布尔兼容)
                    w.writerow((d, model, i_, c_, o_, i_ + o_, cny,
                                int(bool(partial))))
            ok = True
        except OSError:
            ok = False
        # 气泡直连 tray.notify 而非 _notify:后者标题钉死『预算提醒』,
        # 导出是文件操作不是预算事件,混用会稀释告警标题的信号量
        if ok:
            dbg(f"csv exported: {EXPORT_PATH} rows={len(rows)}")
            if self.tray is not None:
                self.tray.notify("zcode-meter", "已导出 zm_usage_export.csv")
        else:
            dbg(f"csv export failed: {EXPORT_PATH}")
            if self.tray is not None:
                self.tray.notify("zcode-meter",
                                 "导出失败:zm_usage_export.csv 不可写(被占用?)")

    def _copy_today_summary(self):
        """右键『复制今日摘要』:格式钉死四行,数据缺席宁缺勿错 ——
        套餐轨未配置/无数据时整行省略(_plan_pct is None),贴出旧数据比缺行
        更误导;燃速恒显示(0 也是『今日未活跃』的信息);partial(含未知
        模型,金额为下限)在金额后追加『(下限)』。数据直读 UI 线程缓存
        快照(self.snap)与 _plan_pct,与托盘概览(T1)同源同刻。复制成功
        仅 dbg 不弹气泡:复制是用户的显式动作,弹气泡纯属打扰(降噪裁决)。"""
        s = self.snap
        cost = s.today_cost_cny or 0.0
        lines = [f"zcode-meter · {dt.date.today().isoformat()}",
                 f"今日 {fmt_k(s.today_tokens)} tokens · ¥{cost:.2f}"
                 + ("(下限)" if s.today_cost_partial else "")]
        if self._plan_pct is not None:
            lines.append(f"套餐剩余 {self._plan_pct:.0f}%")
        lines.append(f"燃速 {fmt_k(int(s.burn_tokens_per_hour))}/h")
        QGuiApplication.clipboard().setText("\n".join(lines))
        dbg("today summary copied to clipboard")

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
        按新 key 重启(回调重挂新实例)。v0.7 刷新档位:key 未变就地裸写
        monitor.refresh 热更(不重启线程),②④新实例以新档位构造。
        v0.9 T3 落盘保活:设置窗 _parse_input 只造四键 cfg,而 save_config
        白名单重建只写输入携带的键 —— 直传会把用户已存的 bar_segments/skin
        静默抹掉、重启即回默认(评审脚本复现实证:先带 bar_segments 保存再
        以 4 键 cfg 保存,文件键从含段开关掉回三键)。这里从内存态补挂后再
        落盘;skin 沿数据层可选键纪律,glass 不写键。"""
        cfg["bar_segments"] = dict(self.bar_segments)
        if self.skin_id != "glass":
            cfg["skin"] = self.skin_id
        if not save_config(cfg):
            return
        self.daily_budget_cny = cfg["daily_budget_cny"]
        self.eng.daily_budget_cny = cfg["daily_budget_cny"]
        self.alerts.thresholds = sorted(
            {float(t) for t in (cfg["alert_pct"] or []) if t > 0}, reverse=True)
        old, new = self._quota_key or "", cfg["quota_api_key"] or ""
        # 刷新档位:可选键语义,消费方一律 get 缺省 auto(旧配置文件无此键)
        new_refresh = cfg.get("quota_refresh", "auto")
        if old == new:
            # ①key 未变:monitor 不动;刷新档位就地热更(裸写属性,run() 每
            # tick 现读,1s 内生效 —— 重启线程反而丢 _last_fetch_ts 频率
            # 记账,换档立即重查一次白耗请求)。未配 key(monitor=None)时
            # 无对象可热更,档位随②启用分支的新实例生效
            if self.quota_monitor is not None:
                self.quota_monitor.refresh = new_refresh
        elif not old and new:
            if not _state_guard():          # ②启用:仍过守卫闸(测试环境不发真请求)
                self.quota_monitor = QuotaMonitor(new, new_refresh)
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
            self._plan_left_tok = None
            # 校准状态一并清(P1 修复:_pct_ratio 原先无任何重置路径,首个
            # 样本后冷启动估算永久不可达;换号后旧账号的 ratio/基线混进新
            # 账号首个样本,会污染剩余量推算 —— 账号边界是它唯一必须归零
            # 的位置,与上方三缓存同属『旧账号数据零残留』不变式)
            self._pct_ratio = None
            self._last_pct_seen = None
            self._last_win_tok_seen = None
            self._prev_quota = None   # 清 prev:残留旧号快照会让新号首查误报『额度已重置』(T8 重写须保留)
            self.snap.plan_remaining_pct = None
            self.eng.quota_hint = None
            self.eng.on_activity = None
            if new and not _state_guard():  # ④换号:立即按新 key 重启,不停在 None
                self.quota_monitor = QuotaMonitor(new, new_refresh)
                self.quota_monitor.start()
                # 换号必须重挂新实例的回调:漏挂则换号后只剩启动首查,
                # quota 永不因活动刷新(比现状更糟的翻车点)
                self.eng.on_activity = self.quota_monitor.notify_activity
        self._quota_key = new
        # dbg 只记预算/阈值/布尔/档位,不记 key 明文(泄漏面专查项)
        dbg(f"config applied: budget={cfg['daily_budget_cny']} "
            f"alert={cfg['alert_pct']} monitor_on={self.quota_monitor is not None} "
            f"key_changed={old != new} refresh={new_refresh}")

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
        self._apply_freshness()
        self._refit_dock()          # 数据变化后重算条尺寸
        # 几何已由 _refit_dock/_apply_dock_geometry 统一管理,无独立校正需要

    def _apply_freshness(self):
        """数据新鲜度 → 整窗透明度:贴边条形态且套餐数据 >15 分钟未更新
        (静默期停查的可见代价)→ 0.8;数据回新鲜/脱离贴边即自愈回 1.0 ——
        本方法由 _poll_queue 每 200ms 驱动,形态切换点无需另行挂钩。
        只读 dock 与 _plan_fetched_at,不碰 quota 调度/告警/托盘
        (quota_fetch_decision 的静默期语义由 data 层单测钉死,与展示解耦);
        值缓存防 churn:与上次相同则跳过 setWindowOpacity。"""
        op = frozen_opacity(self.dock, self._plan_fetched_at)
        if op != self._frozen_opacity:
            self._frozen_opacity = op
            self.setWindowOpacity(op)

    def _update_quota(self):
        """quota 轨数据搬运(UI 线程):daemon 线程只产出普通 dict,这里取
        拷贝渲染并回写引擎 —— plan_remaining_pct 引擎只写 None、UI 回填
        (引擎从不读它,跨线程无竞态);nextResetTime 给计费块对齐块界用。
        v0.5.1:同时缓存 fetched_at(渲染『N分钟前』新鲜度)与 next_reset_ms
        (倒计时每 200ms 渲染 tick 本地重算,零 API 请求)。
        重置通知:在覆盖 _prev_quota 前先比对快照检出 5h/cycle 重置(顺序
        不可乱,否则 prev 恒等于 cur、事件永不触发);经 BudgetAlerts.
        fire_once 当日去重后直连托盘气泡 —— 刻意不走 _notify(标题钉死
        『预算提醒』,重置不是预算事件,混用会稀释告警标题的信号量)。
        quota 未配置/未出数据时本方法早退,事件自然不触发。
        v0.9.x 新数据闸(P1 修复):整段搬运只在 quota 新数据到达时执行一次。
        本方法由 _poll_queue 每 200ms 驱动,而 QuotaMonitor 只在真实抓取成功
        时写 _latest(两次抓取 ≥60s),latest() 在两次抓取之间返回内容恒等
        的 dict —— 旧代码每 tick 无条件重放整段,造成两个 P1:
        ① 校准节拍错位:_last_win_tok_seen 每 200ms 被覆写,d_tok 只覆盖
           一个 tick 的块增量,d_pct 却是整个刷新间隔(180s)的跳变,
           ratio(1%=X token)系统性低估 刷新间隔/0.2s 倍(180s 档 900
           倍),坏 ratio 又把冷启动估算永久遮蔽(_pct_ratio 无重置路径);
        ② fetch_billing_blocks(1)(开 SQLite 连接+聚合 SQL)在 UI 线程
           5 次/秒常驻。
        fetched_at 由 _fetch_and_record 每次成功抓取附加、时刻单调,是与
        _plan_fetched_at(上次已处理的那次抓取)比对『这份数据是否新』的
        唯一凭证;清号/换号分支已置 _plan_fetched_at=None,新号首查天然
        过闸。fetched_at 缺失/非法时不设闸(与尾部记账分支同一判据,
        _fetch_and_record 的 writer 契约保证该形态不存在,防御未来变更,
        宁可退回旧行为也不把 quota 轨卡死)。"""
        m = self.quota_monitor
        if m is None:
            return
        data = m.latest()
        if not data:
            return
        fa = data.get("fetched_at")
        if (isinstance(fa, (int, float)) and not isinstance(fa, bool)
                and fa > 0 and float(fa) == self._plan_fetched_at):
            return                      # 同一份数据已处理过,等下次真实抓取
        ev = quota_reset_event(self._prev_quota, data)
        self._prev_quota = dict(data)
        if ev is not None and self.alerts.fire_once(
                f"reset_{ev}", dt.date.today().isoformat()):
            dbg(f"quota reset notified: {ev}")   # 只记事件类型,无 key 明文
            if self.tray is not None:            # 无托盘环境仅 dbg,不降级成弹窗
                self.tray.notify("zcode-meter",
                                 "5h 额度已刷新" if ev == "5h" else "周期额度已重置")
        pct = data.get("remaining_pct")
        if pct is not None:
            self._plan_pct = pct
            self.snap.plan_remaining_pct = pct
            # 剩余量估算(两级):
            # ① 动态校准:相邻两次 quota 刷新的 pct 跳变 × 本地窗口 token
            #    增量 → ratio(1%=X token,EMA 平滑) → 剩余 = pct×ratio。
            #    即时且自校准,不依赖块初期的大分母,不带整块历史误差。
            #    分子分母必须同覆盖一个刷新间隔:_last_pct_seen/_last_win_tok_seen
            #    只随新数据闸内的本分支更新(不再被 UI tick 每 200ms 覆写),
            #    d_pct 与 d_tok 才是同一时段的两种读数。
            # ② 冷启动退守:当前 5h 块本地用量 ÷ 已用% 反推(块累计口径)。
            # 接口不回 token 绝对量(TIME_LIMIT 的 usage/remaining 是工具
            # 次数额度,非 token —— 2026-09-28 实测原始 payload 确认)。
            try:
                blocks = self.eng.fetch_billing_blocks(1)
                win_tok = next((t for _s, t, c in blocks if c), None)
            except Exception:
                win_tok = None
            used_pct = 100.0 - pct
            # ① 校准:pct 有跳变且本地窗口用量可读
            if (self._last_pct_seen is not None and win_tok is not None
                    and self._last_win_tok_seen is not None):
                d_pct = self._last_pct_seen - pct      # pct 下降 = 消耗
                d_tok = win_tok - self._last_win_tok_seen
                if d_pct >= 1 and d_tok > 0:
                    new_ratio = d_tok / d_pct
                    self._pct_ratio = (new_ratio if self._pct_ratio is None
                                       else self._pct_ratio * 0.6 + new_ratio * 0.4)  # EMA
            self._last_pct_seen = pct
            self._last_win_tok_seen = float(win_tok) if win_tok else None
            # ② 估算输出
            if self._pct_ratio is not None:
                self._plan_left_tok = self._pct_ratio * pct
            elif win_tok and used_pct > 0.5:
                self._plan_left_tok = win_tok * pct / used_pct
            else:
                self._plan_left_tok = None
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

    def _set_dot_active(self, active: bool):
        """dot 状态设置。v0.8.0 T3:三形态 dot 均 PulseIndicator(卡14/横10/
        竖12),M5 中间态的 QLabel『●』分支随条形态重绘删除 —— isinstance
        分派使命完成,不再保留旧色逻辑。"""
        self.dot.set_active(active)

    def _apply_snapshot(self, s: Snapshot):
        generating = s.state == "generating"
        self._set_dot_active(generating)     # 三形态均脉冲环(T3 后无 QLabel 分支)
        # 主数字 = 全局瞬时吞吐(2026-09-28 语义切换:机器全部会话的流式贡献
        # + 10s 完成重叠窗,不再跟随当前会话;tps_est/tps_exact 仍由引擎维护
        # 但仅供字符比校准等内部用途)。<0.05 视为空闲显 --
        gtps = s.global_tps or 0.0
        if self._bar_form is None:
            # v0.8.0 T5+T2:卡片新结构渲染(_apply_card),此后即返回 ——
            # 下方条分支消费的 in_lbl/plan_ring 等在卡形态是 None(F4 表)
            self._apply_card(s, generating, gtps)
            self._refit_dock()
            return
        # ---- v0.8.0 T3 条形态渲染(F4 属性表横/竖列)----
        # 恒显件每拍显式 setVisible(True):无父构造再 addWidget 的子控件
        # 在『加入已可见父』后不自动 show(无事件循环的 stress 环境恒
        # isHidden),显式置位让显隐状态可断言、也不依赖布局激活时机;
        # setVisible 幂等,200ms 一跳无重绘 churn
        # 恒显件每拍显式 setVisible(True):无父构造再 addWidget 的子控件
        # 在『加入已可见父』后不自动 show(无事件循环的 stress 环境恒
        # isHidden),显式置位让显隐状态可断言、也不依赖布局激活时机;
        # setVisible 幂等,200ms 一跳无重绘 churn。段可关后(v0.8.0 对版期
        # 『靠边停放自定义显示内容』)恒显语义退役 —— 未建段件为 None,
        # 逐件短路,恒显置位只对『建了的段件』做
        if self._bar_form == "h":
            self.tps_lbl.setVisible(True)
            if self.today_lbl is not None:
                self.today_lbl.setVisible(True)
            if self.sep_today is not None:
                self.sep_today.setVisible(True)
        else:
            self.tps_lbl.setVisible(True)
            for w_ in (self.today_lbl, self.in_lbl, self.vbar_in_cap,
                       self.vsep_today, self.vsep_in):
                if w_ is not None:
                    w_.setVisible(True)
        speed_txt = f"{gtps:.1f}" if gtps >= 0.05 else "--"
        if self._bar_form == "h":
            self.tps_lbl.setText(speed_txt)   # 14px Bold 蓝;单位恒显 "t/s" dim
            self.tps_unit_lbl.setVisible(True)
        else:
            self.tps_lbl.setText(speed_txt)            # 单位由『TOK/S』caption 表达
        # sparkline 三形态恒建:<2 点隐藏不闪空(与卡片同口径,T-4 风险);
        # 隐藏控件被 _bar_size 跳过,条宽/高不虚胖;段关 → 未建(None)跳过
        vals = s.recent_speeds or []
        if self.spark is not None:
            self.spark.set_values(vals)
            self.spark.setHidden(len(vals) < 2)
        # 今日段:横条『今X[ ≈¥N]』(金额取整;cost=0 整段省略、partial ≈
        # 前缀 —— 与卡片同守卫,N3);竖条组 v 只保 token(空间受限)
        cost = s.today_cost_cny or 0.0
        if self._bar_form == "h":
            if self.today_lbl is not None:
                # 量↔金额分隔符皮肤化(v0.9 T2):玻璃 today_cost_sep 恒单
                # 空格 → 文案与旧版逐位一致;报纸双空格档(T4)只动这个静态
                # 分隔字符,不碰任何数值格式化(口径红线,risk#1)
                cost_txt = (f"{self._skin().today_cost_sep}"
                            f"{'≈' if s.today_cost_partial else ''}¥{cost:.0f}"
                            if cost else "")
                # 『今』与数字间留空格(用户对版 2026-09-28:CJK 字面贴 mono 数字
                # 过挤;原型『今481M』写法从宽,以用户观感为准)
                self.today_lbl.setText(f"今 {fmt_k(s.today_tokens)}{cost_txt}")
        elif self.today_lbl is not None:
            self.today_lbl.setText(fmt_k(s.today_tokens))
        # ---- 套餐段(plan_on)/燃速段(burn_on):段/组数据缺席时其前
        # sep/vsep 一并隐藏(F4:sep_plan 跟套餐、sep_burn 跟燃速、
        # sep_today/vsep_today/vsep_in 恒显),不残留悬空线 ----
        plan_on = self._plan_pct is not None
        burn = s.burn_tokens_per_hour or 0.0
        burn_on = burn > 0
        cd = format_countdown_hm(self._plan_next_reset)
        cd_bar = cd.replace(" ", "") if cd else cd   # 条形态紧凑档(原型『1h23m』
        if self._bar_form == "h":                    # 无空格;卡片『2h 55m 后重置』保留)
            if self.sep_plan is not None:
                self.sep_plan.setVisible(plan_on)
            if self.sep_burn is not None:
                self.sep_burn.setVisible(burn_on)
            if self.plan_ring is not None:
                self.plan_ring.setVisible(plan_on)
                self.plan_lbl.setVisible(plan_on)
            # cd 缺 → 置空并隐藏(条形态隐藏非空文本纪律,v0.5.1 同款)
            if self.plan_cd_lbl is not None:
                self.plan_cd_lbl.setVisible(plan_on and bool(cd))
            if plan_on and self.plan_ring is not None:
                pct = self._plan_pct
                color = tier_color(pct)
                self.plan_ring.set_pct(pct, color)
                lt = self._plan_left_tok
                self.plan_lbl.setText(
                    f"{pct:.0f}%" + (f" ~{fmt_k(int(lt))}" if lt is not None else ""))
                self.plan_lbl.setStyleSheet(f"color: {color};")
                if cd and self.plan_cd_lbl is not None:
                    self.plan_cd_lbl.setText(cd_bar)
            if burn_on and self.burn_lbl is not None:
                # 瞬时燃速(最近请求吞吐)优先,无单请求数据退回 60min 窗口值;
                # 后接会话平均燃速 —— 口径一字不动(v0.7 既有分支)
                shown = s.burn_instant_per_hour or burn
                t = f"燃速 {fmt_k(int(shown))}/h"
                ab = s.burn_avg_tokens_per_hour
                if ab:
                    t += f" · 均燃 {fmt_k(int(ab))}/h"
                self.burn_lbl.setText(t)
            if self.burn_lbl is not None:
                self.burn_lbl.setVisible(burn_on)
        else:
            if self.vsep_plan is not None:
                self.vsep_plan.setVisible(plan_on)
            if self.vsep_burn is not None:
                self.vsep_burn.setVisible(burn_on)
            if self.plan_lbl is not None:
                self.plan_lbl.setVisible(plan_on)
                self.plan_sub_lbl.setVisible(plan_on)
            if plan_on and self.plan_lbl is not None:
                pct = self._plan_pct
                self.plan_lbl.setText(f"{pct:.0f}%")
                self.plan_lbl.setStyleSheet(f"color: {tier_color(pct)};")
                # k 行段列表 [~lt, cd] 以『 · 』join:空列表 → 置空但组结构
                # 保留(v 行 N% 仍显);lt/cd 缺段自然省略(N3,现状口径明文化)
                lt = self._plan_left_tok
                self.plan_sub_lbl.setText(" · ".join(
                    seg for seg in (f"~{fmt_k(int(lt))}" if lt is not None else None,
                                    cd_bar or None) if seg))
            if self.burn_lbl is not None:
                self.burn_lbl.setVisible(burn_on)
                self.vbar_burn_cap.setVisible(burn_on)
            if burn_on and self.burn_lbl is not None:
                self.burn_lbl.setText(fmt_k(int(burn)) + "/h")
                ab = s.burn_avg_tokens_per_hour
                self.vbar_burn_cap.setText(
                    "燃速" + (f" · 均{fmt_k(int(ab))}" if ab else ""))
            # 入出组(段可关):未建(None)跳过
            if self.in_lbl is not None:
                self.in_lbl.setText(fmt_k(s.session_in))
                self.vbar_in_cap.setText(f"入 · 出{fmt_k(s.session_out)}")
        self._refit_dock()

    def _apply_card(self, s: Snapshot, generating: bool, gtps) -> None:
        """v0.8.0 T5+T2 卡片渲染(预览 .g-card 结构,_build_card ①-⑥ 对应)。
        条形态走 _apply_snapshot 的 T3 新分支;本方法只消费 F4 属性表卡列的
        件,条形态件(plan_ring(横)/in_lbl(竖)等)一律不碰。gtps=全局瞬时
        吞吐(2026-09-28 语义切换,主数字不再用会话级 tps_est/exact)。"""
        # ① 状态行:elapsed 并入状态文本(idle『空闲』);会话标题降级为窗口
        # tooltip(F3 裁决:卡片行数减法由 tooltip+告警气泡补偿);模型名
        # manual 前缀 📌、超宽 elide(固定 150px 钳宽,不反推撑宽卡片)
        self.state_lbl.setText(
            f"生成中 {s.gen_elapsed:.0f}s" if generating else "空闲")
        self.setToolTip(("📌 " if s.manual else "") + (s.title or "当前会话"))
        fm = QFontMetrics(self.model_name_lbl.font())
        name = ("📌 " if s.manual else "") + (s.model or "")
        # 190px:内容宽 289 − 速度列(最宽 "999.9 t/s" 实测 87)− 间距 6 的
        # 余量内取整;模型行不改列宽策略(name label 本就在拉伸侧)
        self.model_name_lbl.setText(fm.elidedText(name, Qt.ElideRight, 190))
        # ② 主数字行:全局瞬时吞吐恒带 ~ 前缀(估算量:流式贡献+10s 重叠窗,
        # README 口径『~=估算』仍成立);<0.05 空闲显 --;sparkline=同口径
        # 逐秒采样(recent_speeds),<2 点隐藏不闪空(T-4 风险)
        self.tps_lbl.setText(f"~{gtps:.1f}" if gtps >= 0.05 else "--")
        vals = s.recent_speeds or []
        self.spark.set_values(vals)
        self.spark.setHidden(len(vals) < 2)
        # ③ 今日 hero 三段:cost=0 省金额段、partial ≈ 前缀(口径逐字沿旧)
        self.today_lbl.setText(fmt_k(s.today_tokens))
        cost = s.today_cost_cny or 0.0
        self.today_cost_lbl.setText(
            f"{'≈' if s.today_cost_partial else ''}¥{cost:.2f}" if cost else "")
        self.today_cost_lbl.setHidden(not cost)
        srcs = s.today_by_source
        src_txt = (" · ".join(f"{n} {fmt_k(t)}" for n, t in srcs)
                   if srcs and len(srcs) > 1 else "")
        # 并入今日行右端(Ignored+elide 130 防撑宽,右对齐):elide 用本
        # label 9px 字体量宽
        src_fm = QFontMetrics(self.today_src_lbl.font())
        self.today_src_lbl.setText(
            src_fm.elidedText(src_txt, Qt.ElideRight, 130) if src_txt else "")
        self.today_src_lbl.setHidden(not src_txt)
        self.today_src_lbl.setToolTip(src_txt if
                                      self.today_src_lbl.text() != src_txt
                                      else "")
        # ④ 套餐 section 容器化:_plan_pct None → 分节线随段整组隐藏(不悬
        # 空);ring 中心 N% tier 色;lt 缺占位『—』;r 行段序与旧 D3 表相反
        # 是有意变更(评审第 3 轮):倒计时在前、『 · 』join、无前导点
        pct = self._plan_pct
        self.plan_section.setHidden(pct is None)
        if pct is None:
            self.plan_lbl.setText("")
            self.plan_lbl.setStyleSheet("")
        else:
            color = tier_color(pct)
            self.plan_ring.set_pct(pct, color)
            # plan_lbl(恒建,卡形态隐藏):同步 % 文本与 tier 色 —— 条形态
            # 的消费载体,也是 stress tier 注入断言的锚
            self.plan_lbl.setText(f"{pct:.0f}%")
            self.plan_lbl.setStyleSheet(f"color: {color};")
            lt = self._plan_left_tok
            self.plan_tok_lbl.setText(
                f"~{fmt_k(int(lt))} tokens" if lt is not None else "—")
            cd = format_countdown_hm(self._plan_next_reset)
            age = format_age_zh(self._plan_fetched_at)
            sub_txt = " · ".join(
                seg for seg in (f"{cd} 后重置" if cd else None, age) if seg)
            self.plan_sub_lbl.setText(sub_txt)
            self.plan_sub_lbl.setHidden(not sub_txt)
            # 右端预算余量:est_hours_left 缺隐藏;≤0『超支』红档(k 换『预算』)。
            # 未配日预算时兜底显示燃速金额版 ¥/h(token 版在 grid 燃速格,
            # 用户 taste『token+¥ 全统一』)—— 两者都缺才隐藏。
            eh = s.est_hours_left
            bc = s.burn_cny_per_hour or 0.0
            if eh is not None:
                self.plan_left_k.setHidden(False)
                self.plan_left_v.setHidden(False)
                if eh <= 0:
                    self.plan_left_k.setText("预算")
                    self.plan_left_v.setText("超支")
                    self.plan_left_v.setStyleSheet(
                        f"color: {C_TIER_DANGER};")
                else:
                    self.plan_left_k.setText("还可撑")
                    self.plan_left_v.setText(f"{eh:.1f}h")
                    self.plan_left_v.setStyleSheet(f"color: {C_FG};")
            elif bc > 0:
                self.plan_left_k.setHidden(False)
                self.plan_left_v.setHidden(False)
                self.plan_left_k.setText("燃速")
                self.plan_left_v.setText(f"¥{bc:.2f}/h")
                self.plan_left_v.setStyleSheet(f"color: {C_FG};")
            else:
                self.plan_left_k.setHidden(True)
                self.plan_left_v.setHidden(True)
        # ⑤ grid 六格:k 恒显、v 缺参 --、按列宽 elide(CARD_GRID_COL_W
        # 宽度策略);燃速格 est_hours_left 降级为 tooltip(F3 取舍:
        # 『还可撑 X 小时』不再占行,悬停补偿;elide 丢失的全量原文同走
        # 该格 tooltip,悬停可回读)
        def _elide(lb, text, maxw):
            fmv = QFontMetrics(lb.font())
            el = fmv.elidedText(text, Qt.ElideRight, maxw)
            lb.setText(el)
            lb.setToolTip(text if el != text else "")

        cw = self.CARD_GRID_COL_W
        # liquid 皮肤:药丸等宽 93、内留白 7×2,文字预算 79 —— 超宽截断
        # 防溢出药丸边界(用户 2026-09-29);其它皮肤按列宽(elide 纪律不变)
        liquid = (self.skin_id == "liquid")
        text_w = (skins.LIQUID_GRID_TEXT_W if liquid else 0)
        in_out_txt = f"{fmt_k(s.session_in)} / {fmt_k(s.session_out)}"
        _elide(self.in_out_lbl, in_out_txt, text_w or cw[0])
        _elide(self.rate_lbl, f"{s.cache_rate:.2f}%", text_w or cw[1])
        tt = f"{s.last_ttft:.1f}" if s.last_ttft is not None else "--"
        du = f"{s.last_duration:.1f}s" if s.last_duration is not None else "--"
        _elide(self.timing_lbl, f"{tt} / {du}", text_w or cw[2])
        burn = s.burn_tokens_per_hour or 0.0
        burn_txt = f"{fmt_k(int(burn))}/h"
        _elide(self.burn_lbl, burn_txt, text_w or cw[0])
        if s.est_hours_left is not None:
            self.burn_lbl.setToolTip(
                burn_txt + (" · 预算已超支" if s.est_hours_left <= 0
                            else f" · 预算还可撑 {s.est_hours_left:.1f}h"))
        else:
            self.burn_lbl.setToolTip("")
        ab = s.burn_avg_tokens_per_hour
        _elide(self.avg_burn_lbl,
               f"{fmt_k(int(ab))}/h" if ab is not None else "--",
               text_w or cw[1])
        _elide(self.avg_lbl, f"{s.tps_avg:.1f} t/s" if s.tps_avg else "--",
               text_w or cw[2])
        # ⑥ 模型列表 rows[:4]:首行不带『均』前缀(v0.7→v0.8 有意变更),
        # 行数不足隐藏(容器高度随可见行收缩)
        rows = (s.speed_by_model or [])[:4]
        for i, (nm, sp_lbl) in enumerate(self._model_rows_items):
            on = i < len(rows)
            nm.setHidden(not on)
            sp_lbl.setHidden(not on)
            if on:
                prov, model, tps_m, _out = rows[i]
                # elide 必须用『本行名 label』的 10px 字体量宽 —— 用状态行
                # 字体的 metrics 会低估,长名穿透上限反推撑宽卡(实测
                # model_rows 462px 假峰的病根)。215:内容宽 281 − 最宽速度
                # "999.9 t/s"≈60 − 行距 6(预览 .m 两端 space-between,名占
                # 满余宽;旧 190 收窄无据)
                fmn = QFontMetrics(nm.font())
                nm.setText(fmn.elidedText(f"{prov} / {model}",
                                          Qt.ElideRight, 215))
                sp_lbl.setText(f"{tps_m:.1f} t/s" if tps_m else "-- t/s")
        # 液态玻璃药丸几何缓存(第十轮):此处布局已可激活 —— 先 activate
        # 落地几何,再量六格 k/v 顶底与列中线存 _pill_geo,paintEvent 的
        # deco 只消费缓存不现读几何(布局惰性激活,现读在形态切换首帧
        # 必读旧值 —— 真机『3 个竖条药丸+灰白遮罩』根因)。激活失败/竞态
        # 首帧缓存放空,deco 跳过药丸只画底,下一帧补上,时序永远正确。
        if self.skin_id == "liquid" and self._bar_form is None:
            # activate 在 --verify 环境挂起(实测);几何量取退化为
            # 『下轮 paint 前置 activate 后的 findChildren 现读』—— 改由
            # paintEvent 侧首帧激活后回填缓存(pill_geo_cb)
            pass
        elif self._bar_form is not None:
            self._pill_geo = None    # 条形态不消费;防切形态后用旧卡几何

    def _refit_dock(self):
        """条模式下数据文字变长时重算条尺寸(防截断);几何统一由
        _apply_dock_geometry(物理坐标)设置 —— 此处只做"变了才设"的判定。"""
        if not self.dock:
            return
        vertical = self.dock in ("left", "right")
        w, h = self._bar_size(vertical)
        if (w, h) == (self.width(), self.height()):   # 逻辑对逻辑:尺寸未变不动几何
            return
        self._apply_dock_geometry()

    def _tick_breath(self):
        """60ms 呼吸相位推进(v0.4.0 起)。v0.8.0 T3:三形态 dot 均脉冲环,
        只推相位(set_phase 内部仅 generating 时 update,idle 零重绘);旧
        QLabel 绿算式分支随 T3 删除(M5 中间态退役)。"""
        self._breath = (self._breath + 0.08) % 1.0
        self.dot.set_phase(self._breath)

    # ---- 自检 ----
    def _verify(self):
        self._apply_snapshot(self.snap)
        n = sum(1 for c in self.findChildren(QLabel))
        print(f"window: {self.width()}x{self.height()} labels={n}")
        # 模拟鼠标触底边:override _pointer_pos 返回物理坐标(与 _settle 判定同系)
        l, t, r_, b_ = monitor_workarea_of(int(self.winId()))
        self._pointer_pos = lambda: QPoint((l + r_) // 2, b_)
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
        self._ref_value: float | None = None   # 参考线值(近7天日均);None=不画
        self._ref_label = ""               # 参考线线顶标注文本
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

    def set_reference(self, value, label: str = ""):
        """水平图参考线(近 7 天日均):_paint_h 画竖向虚线 + 线顶标注,
        value=None 清除。竖柱形态不画(v0.5.2 起三页签统一水平条,竖柱仅供
        未来切换,参考线对其静默无效)。"""
        self._ref_value = float(value) if value else None
        self._ref_label = str(label)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(C_BG))
        if not self._items:
            p.setPen(QColor(C_DIM))
            p.setFont(_yf(11))
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
        # 标签列按内容自适应:取最长标签的实际渲染宽度(上限 150,下限 60),
        # 避免短标签(日期 70px)被 130px 固定列推远柱子
        f_lbl0 = _yf(10)
        fm0 = QFontMetrics(f_lbl0)
        longest = max((fm0.horizontalAdvance(t[0]) for t in self._items), default=40)
        lbl_w = min(150, max(30, longest + 4))   # 紧贴:只留 4px 呼吸,短标签窄列
        x0, right = lbl_w + 3, w - 10          # 标签与条形仅 3px 间距
        val_w = 96                                     # 数值区预留(token+¥)
        bar_max = max(right - x0 - val_w - 6, 20)
        row_h = min(26, max((h - 8) / max(n, 1), 13))
        f_lbl = _yf(10)
        f_val = mk_mono(10)
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
        # 参考线(近 7 天日均,HistoryWindow『本月预计』配套):竖向虚线画在
        # x = x0 + value/vmax*bar_max,与条形同一比例尺 —— 线的落点即可目视
        # 读出『日均约为当日峰值的几成』。x 全按当前 w 现算(上方列宽已按 w
        # 自适应),不缓存像素(v0.3.0 固定尺寸脱节的教训);标注画在线顶右侧,
        # 右缘放不下时换到线顶左侧右对齐。用警示黄虚线与主题绿条区分。
        if self._ref_value and self._ref_value > 0:
            rx = x0 + max(min(self._ref_value / vmax * bar_max, bar_max), 0.0)
            pen = QPen(QColor(C_WARN))
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QLineF(rx, 2.0, rx, min(4 + n * row_h, h - 2)))
            p.setFont(f_lbl)
            tw = fm.horizontalAdvance(self._ref_label)
            if rx + 4 + tw <= w - 4:
                p.drawText(QRectF(rx + 4, 2, tw + 4, row_h),
                           Qt.AlignVCenter | Qt.AlignLeft, self._ref_label)
            else:
                p.drawText(QRectF(4, 2, max(rx - 8, 12), row_h),
                           Qt.AlignVCenter | Qt.AlignRight,
                           fm.elidedText(self._ref_label, Qt.ElideRight,
                                         max(rx - 8, 12)))

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
        f_val, f_lbl = mk_mono(10), _yf(10)
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
            # (faint 档 hex,v0.8.0 与 QSS QLabel#faint 字面同步换值)
            if self._extra and i < len(self._extra) and self._extra[i]:
                p.setPen(QColor("#68696c"))
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

    # 头部口径说明的固定底稿:refresh 在其后追加近 7 天趋势段(有数据时),
    # 零用量/无数据回到纯底稿 —— 底稿集中一处,防 __init__ 与 refresh 两处漂移
    HINT_BASE = ""   # 口径说明按用户要求移除,hint 为纯数据行

    def __init__(self, eng: DataEngine):
        super().__init__(None)
        self.eng = eng
        self.setWindowTitle("zcode-meter · 历史用量")
        self.setObjectName("histRoot")
        self.setStyleSheet(QSS_HIST)
        self.setWindowFlag(Qt.Window, True)
        self.resize(780, 460)

        # v0.5.2:三图统一水平条(用户实测竖柱不便阅读);长标签按空间省略,
        # 悬停 tooltip 显示全量 label+数值 —— 近 7 天日均参考线也只画在
        # 水平形态上(见 BarChart.set_reference)
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
        bl.setContentsMargins(0, SP["s"], 0, 0)
        bl.setSpacing(SP["xs"])
        blk_note = QLabel(
            "每根柱=一个 5 小时窗口:标签为窗口起点~终点,柱高是该时段内的用量"
            "(即上一时刻到终点时刻之间)。块界对齐:已配置 quota(Coding Plan)时"
            "按平台 5h 计费窗对齐(nextResetTime−k×5h,黄色=当前活动块);"
            "未配置或不可用时回退锚点=最早 completed 请求时刻,此时块界为示意、"
            "非平台真实计费窗。聚合口径=query_source 全部 completed in+out"
            "(同今日用量)。")
        blk_note.setObjectName("dim")
        blk_note.setWordWrap(True)
        bl.addWidget(blk_note)
        # 区间标签行多(29桶),固定窗口高装不下会被裁最后一行 —— 放进滚动区,
        # 图表按内容要足高度(scroll 里的 widget 不受窗口高压缩)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self.block_chart)
        self.block_chart.setMinimumHeight(29 * 26 + 12)   # 29桶 × 行高26 + 边距
        bl.addWidget(scroll, 1)
        self.tabs.addTab(blk, "计费块(每5h一桶)")

        head = QHBoxLayout()
        self.hint = QLabel(self.HINT_BASE)
        self.hint.setObjectName("dim")
        self.hint.setWordWrap(True)
        btn = QPushButton("刷新")
        btn.clicked.connect(self.refresh)
        head.addWidget(self.hint, 1)
        head.addWidget(btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(SP["l"], SP["m"], SP["l"], SP["m"])
        root.setSpacing(SP["m"])
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
        # 近 7 天趋势外推:日均虚线画进按天图 + 头部 hint 追加『本月预计』。
        # 窗口零用量(空库/近 7 天没用)→ trend_forecast 返回 None,线与标注
        # 双双不出现(宁缺勿错:0 日均外推的『本月预计 ¥0.00』无信息量);
        # hint = 纯数据行:近7天日均( tokens+¥ ) · 本月预计( tokens+¥ ) · 全部历史
        t_tok, t_cny, t_p = self.eng.fetch_total_usage()
        total_txt = (f"全部历史 {fmt_k(t_tok)} tokens · "
                     + ("≈" if t_p else "") + f"¥{t_cny:,.0f}")
        # partial(窗口内含未知模型)时 ¥ 前加 ≈,金额为下限
        fc = trend_forecast(self.eng.fetch_daily_model_usage(7))
        if fc is None:
            self.daily_chart.set_reference(None, "")
            self.hint.setText(total_txt)
        else:
            self.daily_chart.set_reference(
                fc["avg_tokens"], f"日均 {fmt_k(fc['avg_tokens'])}/天")
            approx = "≈" if fc["partial"] else ""
            # 本月预计 tokens = 日均 × 当月天数(与 forecast_cny 同外推口径)
            days_in_month = calendar.monthrange(
                dt.date.today().year, dt.date.today().month)[1]
            ft_tokens = fc["avg_tokens"] * days_in_month
            self.hint.setText(
                f"近7天日均 {fmt_k(fc['avg_tokens'])}/天 · {approx}¥{fc['avg_cny']:.2f}/天"
                f" · 本月预计 {fmt_k(ft_tokens)} · {approx}¥{fc['forecast_cny']:.2f}"
                f" · {total_txt}")
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
            start = dt.datetime.fromtimestamp(start_ms / 1000)
            end = start + dt.timedelta(hours=5)
            # 区间写法:标签是窗口起点,用量属于 起点~起点+5h 之间(消除歧义)
            items.append((start.strftime("%m-%d %H:%M") + "~"
                          + end.strftime("%H:%M"), tok))
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
