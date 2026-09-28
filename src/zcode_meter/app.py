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
import datetime as dt
import json
import math
import os
import queue
import re
import sys
import time
from pathlib import Path

from PySide6.QtCore import QLineF, QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QCursor, QColor, QDoubleValidator, QFont, QFontMetrics, QGuiApplication,
    QPainter, QPen,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPushButton, QScrollArea, QStyle, QSystemTrayIcon, QTabWidget,
    QToolTip, QVBoxLayout, QWidget,
)

# 脚本直跑(python src/zcode_meter/app.py)时 __package__ 为空:补 src 进
# sys.path 再走同一 continue 执行,本文件既是包模块 zcode_meter.app、也可作
# __main__ 直跑 —— 刻意不做「re-import 自身为 zcode_meter.app」,__main__ 副本
# 与测试导入的包模块会是两套类对象(常量/样式双份、isinstance 失配)。
# frozen(exe)时 PyInstaller 已内置包,无需引导。
if __package__ in (None, "") and not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # .../src

from zcode_meter.data_engine import (
    BudgetAlerts, DataEngine, QuotaMonitor, Snapshot, app_dir, dbg,
    format_age_zh, format_countdown_hm, load_config, quota_reset_event,
    save_config, trend_forecast,
)

C_BG, C_BORDER = "#16171c", "#2c2f3a"
C_FG, C_DIM, C_ACCENT, C_WARN = "#e8eaf0", "#8b8f9c", "#5ad6a0", "#e8c268"
C_MONO = "Consolas"
# T-B 间距标度(8pt 栅格半步档):替换全部布局魔法间距,语义就近映射
# (内容行间=xs/s,分组间=m/l,区块边距=l/xl);豁免点就地注释标明
SP = {"xs": 4, "s": 6, "m": 8, "l": 12, "xl": 16}
C_BORDER_SUB = "#232631"   # 次分节符(弱于 C_BORDER 主分节,层级可辨)

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

# 导出的用量明细落点(右键『导出 CSV』):与 STATE_PATH 同锚 app_dir,
# frozen 时落 exe 旁。内容是模型名+token+金额(无密钥),.gitignore 已追加
# 防用户本地导出物随仓库误提交。
EXPORT_PATH = os.path.join(app_dir(), "zm_usage_export.csv")


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
        self._ov_plan.setText(f"套餐剩余 {pct:.0f}%" if pct is not None
                              else "套餐剩余 —")
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


class MeterWindow(QWidget):
    # v0.4.0:新增 燃速/套餐剩余 两行 + 今日用量可能多源第二行,自然高度
    # 实测 314(单源)。v0.5.1 套餐剩余改两行文案(第二行重置倒计时),注入
    # 实测:0~2 行模型 → 328/328/342,3 行模型 356 —— CARD_H 336 会把 2 行
    # 模型(342)截断,提到 350(2 行可容、3 行起 356>350 仍截断,与旧 336
    # 的截断点同点,无回退)。CARD_H 须 ≥ 布局自然高度 ——
    # _unset_dock/_restore_state/_detach_to_pointer 用它 setGeometry,偏小会
    # 静默截断(ui-verify 只打印不校验,需人工目视)。
    # v0.7 T-A 信息层级重排(today/plan 两级 hero + timing 合并单格):grid
    # 少了 今日/首字/整体 三组 caption 行,新增两级 hero 反而更矮,注入实测
    # (findings/measure_card_baseline.py,原生平台+processEvents):满载
    # 0~4 行模型 → 303/303/317/331/345,全矩阵 max=345 <400 全容纳,按
    # ceil(max/2)*2 规则 350→346(多源/plan_sub 行常驻不再撑高,0/1 行同高)。
    CARD_W, CARD_H = 250, 346          # 逻辑像素(DIP),Qt 自动做 DPI 换算
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
            line.setFixedSize(76, 1)      # 竖条内容区等宽(条宽100-边距16-边框2)
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
        root.setContentsMargins(SP["l"], SP["m"], SP["l"], SP["s"])
        root.setSpacing(SP["xs"])

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

        # ---- v0.7 T-A(提案#3):today 从 grid 搬出到 big 行正下方,升为
        # 第二主数字。hero 显式传 cls="normal"(空 objectName → QSS 基础
        # QLabel 色 C_FG;_mk_lbl 缺省 dim,漏传会落灰字);字体两步写与
        # 上方 tps_lbl 同款(先 _mk_lbl 带字号、再 setFont 补 Bold 权重)
        self.today_lbl = self._mk_lbl("--", "normal", C_MONO, 13)
        self.today_lbl.setFont(QFont(C_MONO, 13, QFont.Bold))
        root.addWidget(self.today_lbl)
        # 多源拆分行:>1 源才有文案(8pt 副文本),文案口径沿旧 today 第二行
        self.today_src_lbl = self._mk_lbl("", "dim", "Microsoft YaHei UI", 8)
        root.addWidget(self.today_src_lbl)

        sep = QFrame(); sep.setObjectName("sep")
        root.addWidget(sep)

        grid = QHBoxLayout()
        left = QVBoxLayout(); right = QVBoxLayout()
        left.setSpacing(2); right.setSpacing(2)   # 豁免:字段矩阵行距刻意<xs,密度优先
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
        right.addWidget(self._mk_lbl("命中率", "dim", "Microsoft YaHei UI", 8))
        right.addWidget(self.rate_lbl)
        self.avg_lbl = self._mk_lbl("--", "", C_MONO, 9)
        left.addWidget(self._mk_lbl("平均速度", "dim", "Microsoft YaHei UI", 8))
        left.addWidget(self.avg_lbl)
        # ttft/dur 合并单格:右列第 3 槽与左列 3 行配平,无 caption;分段
        # 占位、整串结构恒保留(缺数据显示 --,不做整行隐藏)—— ⏱ 字形
        # Consolas 缺字时走 Qt 字体回退(Segoe UI Symbol)
        self.timing_lbl = self._mk_lbl("⏱ 首字 -- · 总 --", "", C_MONO, 9)
        right.addWidget(self.timing_lbl)
        grid.addLayout(left, 1)
        grid.addLayout(right, 1)
        root.addLayout(grid)

        # 卡片专属两行(有数据才显示):燃速+耗尽预估 / 套餐剩余。
        # v0.5.0 起条形态也有预算段(横条 plan+burn、竖条紧凑 plan),由
        # _build_bar 各自创建 —— 尾部置 None 纪律只保留真正不创建的 label。
        # v0.7 T-A:plan 两级化 —— hero 主数字(warn 色,C_MONO 13 Bold,
        # 基线宽度按 Consolas 13 Bold 量得)+ faint 副文本(比 dim 更淡);
        # burn 文案与样式不动,仅随新序移到 plan 两级之后
        self.plan_lbl = self._mk_lbl("", "warn", C_MONO, 13)
        self.plan_lbl.setFont(QFont(C_MONO, 13, QFont.Bold))
        root.addWidget(self.plan_lbl)
        self.plan_sub_lbl = self._mk_lbl("", "faint", "Microsoft YaHei UI", 8)
        root.addWidget(self.plan_sub_lbl)
        self.burn_lbl = self._mk_lbl("", "dim", "Microsoft YaHei UI", 8)
        root.addWidget(self.burn_lbl)

        # 次分节:内联样式自诞生即渲染(D4 裁决)—— 零 QSS/objectName 依赖,
        # 与 _mk_sep 的内联机制同款;T-B 仅把 C_BORDER 字面换成分级色,
        # 跨 ticket 无中间态(主 sep 维持 QSS #sep 现机制不动)
        sep2 = QFrame()
        sep2.setStyleSheet(f"background: {C_BORDER_SUB}; border: none; max-height: 1px;")
        root.addWidget(sep2)

        self.model_lbl = self._mk_lbl("", "faint", "Microsoft YaHei UI", 8)
        root.addWidget(self.model_lbl)
        # 卡片不再建 ttft/dur(已并入 timing_lbl):显式置 None —— 残留上次
        # 布局的已销毁对象引用会让 is-not-None 分支摸炸(v0.4.0 None 纪律)
        self.ttft_lbl = self.dur_lbl = None

    def _build_bar(self, vertical: bool = False):
        self._clear()
        root = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        root.setContentsMargins(SP["m"], 1, SP["m"], 1)   # 垂直 1px 豁免:横条高度≤30 红线
        root.setSpacing(SP["s"])
        self._bar_form = "v" if vertical else "h"
        if vertical:
            root.addStretch(1)   # 首尾对称弹性:条高富余时内容整体垂直居中(用户要求)
        if not vertical:
            self.dot = self._mk_lbl("●", "dim", "Segoe UI", 8)
            root.addWidget(self.dot)
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
            # v0.7.x 竖条改分组结构:主数字/今日/套餐 三组,组间分隔线,
            # 组内『数值+小字说明』节奏 —— 替代旧一列直排的密集堆叠。
            # 宽度 90→100(stress 断言同步),空间足够补回燃速与重置倒计时。
            def vnum(txt="", cls="", size=9):
                lb = self._mk_lbl(txt, cls, C_MONO, size)
                lb.setAlignment(Qt.AlignCenter)
                return lb
            def vcap(txt):
                lb = self._mk_lbl(txt, "dim", "Microsoft YaHei UI", 8)
                lb.setAlignment(Qt.AlignCenter)
                return lb
            # 组1 主数字:实时速度(状态点紧贴其上,状态与"速度在变"同义)
            self.dot = self._mk_lbl("●", "dim", "Segoe UI", 8)
            self.dot.setAlignment(Qt.AlignCenter)
            root.addWidget(self.dot)
            self.tps_lbl = self._mk_lbl("--", "accent", C_MONO, 12)
            self.tps_lbl.setFont(QFont(C_MONO, 12, QFont.Bold))
            self.tps_lbl.setAlignment(Qt.AlignCenter)
            root.addWidget(self.tps_lbl)
            root.addWidget(vcap("tok/s"))
            # 组2 今日消耗
            sep = self._mk_sep(True); root.addWidget(sep, 0, Qt.AlignHCenter)
            self.today_lbl = vnum("--")
            root.addWidget(self.today_lbl)
            root.addWidget(vcap("今日"))
            # 组3 套餐状态:plan(百分比)+plan_sub(倒计时)两 label 与今日组
            # 同构,行距走布局 spacing —— 合并多行 label 的行内距不齐的病根
            sep = self._mk_sep(True); root.addWidget(sep, 0, Qt.AlignHCenter)
            self.plan_lbl = vnum("", "warn")
            root.addWidget(self.plan_lbl)
            self.plan_sub_lbl = vnum("", "faint")   # 倒计时行(独立 label)
            root.addWidget(self.plan_sub_lbl)
            self.burn_lbl = vnum("", "dim")
            root.addWidget(self.burn_lbl)
            # 组4 会话累计
            sep = self._mk_sep(True); root.addWidget(sep, 0, Qt.AlignHCenter)
            self.in_lbl = vnum("--")
            root.addWidget(self.in_lbl)
            self.out_lbl = vnum("--")
            root.addWidget(self.out_lbl)
            root.addWidget(vcap("会话"))
            self.rate_lbl = self._mk_lbl("", "dim", "Microsoft YaHei UI", 8)
            self.rate_lbl.setAlignment(Qt.AlignCenter)
            root.addWidget(self.rate_lbl)
            self._budget_sep = None
            root.addStretch(1)   # 保留:把内容顶对齐,底部留白由条高决定
            # 竖条分组结构不显示 avg/elapsed/ttft/dur:显式置 None,
            # 否则保留已销毁旧对象的悬空引用(历史 bug)
            self.avg_lbl = self.elapsed_lbl = self.ttft_lbl = self.dur_lbl = None
        # 横条与竖条共通:卡片专属 label 两种条形态都不创建,统一置 None
        # (只在一种形态置 None 会让另一形态的压力循环摸到已销毁 QLabel
        #  —— test_stress.py 的存在理由;burn/plan 已改由各形态自行创建)
        self.state_lbl = self.model_lbl = self.est_lbl = self.cache_lbl = None
        self.title_lbl = None
        # v0.7 T-A 新增的三个卡片专属 label(今日多源副文本/计时合并单格/
        # 套餐副文本)同样两形态都不建,同点显式置 None
        self.today_src_lbl = self.timing_lbl = None

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
        m.addAction("导出 CSV", self._export_csv)
        m.addAction("复制今日摘要", self._copy_today_summary)
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
        monitor.refresh 热更(不重启线程),②④新实例以新档位构造。"""
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
        quota 未配置/未出数据时本方法早退,事件自然不触发。"""
        m = self.quota_monitor
        if m is None:
            return
        data = m.latest()
        if not data:
            return
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
                # v0.7 T-A:卡片 today 升 hero —— 加『今日』前缀;金额段守卫
                # (cost=0 整段省略)与 partial ≈ 口径逐字沿旧实现
                self.today_lbl.setText(f"今日 {fmt_k(s.today_tokens)}{cost_txt}")
                # 多源聚合(>1 源才显示,避免"只有 ZCode"的噪音行)从 hero
                # 第二行拆到独立 8pt 副文本,join 文案不变
                if self.today_src_lbl is not None:
                    srcs = s.today_by_source
                    self.today_src_lbl.setText(
                        " · ".join(f"{n} {fmt_k(t)}" for n, t in srcs)
                        if srcs and len(srcs) > 1 else "")
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
                    # v0.7 T-A 两级化:hero 只留主数字,新鲜度/重置倒计时降
                    # 为 plan_sub_lbl 副文本(faint 色,明显弱于正文)
                    self.plan_lbl.setText(f"套餐剩余 {self._plan_pct:.0f}%")
            if self.plan_sub_lbl is not None:
                if self._plan_pct is None:
                    self.plan_sub_lbl.setText("")
                else:
                    # 副文本拼段规则(D3 钉死表):对在场段各拼『· 』前缀、段
                    # 间以空格相连 —— 双在场『· 3分钟前 · 1h 30m 后重置』,
                    # 仅 age『· 3分钟前』,仅 cd『· 1h 30m 后重置』;缺
                    # fetched_at/next_reset 该段自然省略;pct 缺席则整行置空
                    # (与 hero 同语义,v0.5.1 单 label 置空语义同源)
                    age = format_age_zh(self._plan_fetched_at)
                    cd = format_countdown_hm(self._plan_next_reset)
                    self.plan_sub_lbl.setText(" ".join(
                        f"· {seg}" for seg in
                        (age, f"{cd} 后重置" if cd else None) if seg))
        else:
            plan_on = self._plan_pct is not None
            burn_on = burn > 0
            if self.plan_lbl is not None:
                self.plan_lbl.setVisible(plan_on)
                if plan_on:
                    # 重置倒计时一并入条(纯本地计算,零请求);无数据自然省略
                    cd = format_countdown_hm(self._plan_next_reset)
                    if self._bar_form == "h":
                        t = f"套餐剩余 {self._plan_pct:.0f}%"
                        if cd:
                            t += f" · {cd}后重置"
                        self.plan_lbl.setText(t)
                    else:
                        # 竖条分组结构:plan 主数字行 + plan_sub 倒计时行
                        # (独立 label,行距走布局 spacing,与今日组同构)
                        self.plan_lbl.setText(f"{self._plan_pct:.0f}%")
                        if self.plan_sub_lbl is not None:
                            self.plan_sub_lbl.setText(cd or "")
            if self.burn_lbl is not None:
                self.burn_lbl.setVisible(burn_on)
                if burn_on:
                    # 竖条分组:纯数值(组语义由上方套餐组的延续性表达)
                    self.burn_lbl.setText(
                        fmt_k(int(burn)) + "/h" if self._bar_form == "v"
                        else f"燃速 {fmt_k(int(burn))}/h")
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
        if self.timing_lbl is not None:
            # v0.7 T-A 卡片专属合并单格:分段占位、整串结构恒保留 —— 卡片
            # 不做整行隐藏,缺数据显示 --(与条形态 ttft/dur 各自『--』同
            # 语义);⏱ 字形 Consolas 缺字走 Qt 字体回退
            tt = f"{s.last_ttft:.1f}s" if s.last_ttft is not None else "--"
            du = f"{s.last_duration:.1f}s" if s.last_duration is not None else "--"
            self.timing_lbl.setText(f"⏱ 首字 {tt} · 总 {du}")
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
        # 标签列按内容自适应:取最长标签的实际渲染宽度(上限 150,下限 60),
        # 避免短标签(日期 70px)被 130px 固定列推远柱子
        f_lbl0 = QFont("Microsoft YaHei UI", 8)
        fm0 = QFontMetrics(f_lbl0)
        longest = max((fm0.horizontalAdvance(t[0]) for t in self._items), default=40)
        lbl_w = min(150, max(30, longest + 4))   # 紧贴:只留 4px 呼吸,短标签窄列
        x0, right = lbl_w + 3, w - 10          # 标签与条形仅 3px 间距
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
