#!/usr/bin/env python3
"""zcode-meter — ZCode token 用量与输出速度桌面小工具。

贴边吸附:拖到屏幕边缘松手 → 变成常驻信息条(不隐藏);拖离边缘松手 → 恢复卡片。
数据源(全部只读):
  - ~/.zcode/cli/db/db.sqlite          model_usage 累计/精确速度, part 流式文本长度
  - ~/.zcode/cli/log/zcode-<date>.jsonl  model.request.started/completed 实时事件

用法:
  python zcode_meter.py            正常启动
  python zcode_meter.py --headless 无界面跑 8 秒,打印数据层状态(测试用)
  python zcode_meter.py --verify   启动界面,1.5 秒后自检渲染并打印报告,退出
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime as dt
import faulthandler
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, field

# 崩溃追踪:pythonw 无控制台,access violation 等原生崩溃的 traceback 落盘
_CRASH_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zm_crash.log")
faulthandler.enable(open(_CRASH_LOG, "a", encoding="utf-8"))

ZCODE_DIR = os.path.expanduser("~/.zcode/cli")
DB_PATH = os.path.join(ZCODE_DIR, "db", "db.sqlite")
LOG_DIR = os.path.join(ZCODE_DIR, "log")
ROLL_DIR = os.path.join(ZCODE_DIR, "rollout")

DEBUG = os.environ.get("ZM_DEBUG") == "1"
DBG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zm_debug.log")


def dbg(msg: str):
    if DEBUG:
        with open(DBG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} {msg}\n")

# ---------------------------------------------------------------- 数据层 ---

@dataclass
class Snapshot:
    """数据线程推给 UI 的一份快照。"""
    state: str = "idle"                 # idle | generating
    model: str = ""
    title: str = ""                     # 会话标题(跟随切换)
    tps_est: float | None = None        # 生成中估算 tok/s(实时)
    tps_exact: float | None = None      # 最近完成的请求精确 tok/s(实时)
    tps_avg: float | None = None        # 会话平均:Σ输出token ÷ Σ(duration-ttft)
    session_in: int = 0                 # 总输入(input_tokens 已含缓存命中,勿再加 cache)
    session_cache: int = 0              # 其中缓存命中的部分
    session_out: int = 0
    cache_rate: float = 0.0             # cache / in
    gen_elapsed: float = 0.0
    updated: float = field(default_factory=time.time)


class DataEngine(threading.Thread):
    POLL_DB = 1.0
    POLL_PART = 0.5
    CHAR_PER_TOKEN_INIT = 3.2

    def __init__(self, out: "queue.Queue[Snapshot]"):
        super().__init__(daemon=True)
        self.out = out
        self.stop_flag = threading.Event()
        self.session_id = self._latest_session()
        self.snap = Snapshot()
        self._running = False
        self._gen_start = 0.0
        self._last_len: int | None = None
        self._last_len_t = 0.0
        self._chars_per_token = self.CHAR_PER_TOKEN_INIT
        self._exact_out_chars = 0
        self._prev_max_rowid = self._max_usage_rowid()

    def _latest_session(self) -> str:
        try:
            best, best_t = "", 0
            for name in os.listdir(ROLL_DIR):
                if name.startswith("model-io-sess_") and name.endswith(".jsonl"):
                    p = os.path.join(ROLL_DIR, name)
                    t = os.path.getmtime(p)
                    if t > best_t:
                        best_t, best = t, name[len("model-io-"):-len(".jsonl")]
            return best
        except OSError:
            return ""

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)

    def _max_usage_rowid(self) -> int:
        try:
            con = self._connect()
            (n,) = con.execute("SELECT COALESCE(MAX(rowid),0) FROM model_usage").fetchone()
            con.close()
            return n
        except sqlite3.Error:
            return 0

    # ---- 日志 tail ----
    def _tail_loop(self):
        path = self._log_path()
        f = None
        pos = os.path.getsize(path) if os.path.exists(path) else 0
        while not self.stop_flag.is_set():
            try:
                newpath = self._log_path()
                if newpath != path:
                    path = newpath
                    if f: f.close()
                    f, pos = None, 0
                if not os.path.exists(path):
                    time.sleep(0.4); continue
                if f is None:
                    f = open(path, "r", encoding="utf-8", errors="replace")
                    f.seek(pos)
                line = f.readline()
                if not line:
                    time.sleep(0.25); continue
                pos = f.tell()
                self._handle_log_line(line)
            except OSError:
                time.sleep(1)

    def _log_path(self) -> str:
        day = dt.date.today().strftime("%Y-%m-%d")
        return os.path.join(LOG_DIR, f"zcode-{day}.jsonl")

    def _handle_log_line(self, line: str):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        ev = obj.get("event", "")
        if ev == "model.request.started":
            self._running = True
            self._gen_start = time.time()
            self._last_len = None
        elif ev in ("model.request.completed", "model.sdk.stream.completed"):
            if self._running:
                self._running = False
                self._on_request_done()

    # ---- db 轮询 ----
    def _db_loop(self):
        while not self.stop_flag.is_set():
            self._poll_stats()
            if not self._running:
                self._poll_new_completed()
            self.stop_flag.wait(self.POLL_DB)

    def _poll_stats(self):
        try:
            con = self._connect()
            sums = con.execute(
                "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read_input_tokens),0),"
                " COALESCE(SUM(output_tokens),0)"
                " FROM model_usage WHERE status='completed' AND session_id=?"
                " AND query_source='main_turn'",
                (self.session_id,)).fetchone()
            # 会话平均输出速率 = 累计输出 token ÷ 累计净生成时长(扣除首 token 延迟)
            avg = con.execute(
                "SELECT COALESCE(SUM(output_tokens),0),"
                " COALESCE(SUM(MAX(duration_ms - COALESCE(time_to_first_token_ms,0), 1)),0)"
                " FROM model_usage WHERE status='completed' AND session_id=?"
                " AND query_source='main_turn'"
                " AND duration_ms IS NOT NULL",
                (self.session_id,)).fetchone()
            # 主力模型按 token 用量取,不能按字典序(否则 Flash 类小模型会盖过主模型)
            main_model = con.execute(
                "SELECT model_id FROM model_usage WHERE status='completed' AND session_id=?"
                " AND query_source='main_turn' GROUP BY model_id"
                " ORDER BY SUM(input_tokens)+SUM(output_tokens) DESC LIMIT 1",
                (self.session_id,)).fetchone()
            con.close()
            self.snap.session_in, self.snap.session_cache, self.snap.session_out = sums
            self.snap.tps_avg = (avg[0] / (avg[1] / 1000)) if avg[0] and avg[1] else None
            # input_tokens 已含 cache_read:命中率 = cache / in,分母不再加 cache
            self.snap.cache_rate = (self.snap.session_cache / self.snap.session_in * 100
                                    if self.snap.session_in else 0.0)
            if main_model and main_model[0]:
                self.snap.model = main_model[0]
        except sqlite3.Error:
            pass

    def _poll_new_completed(self):
        try:
            con = self._connect()
            rows = con.execute(
                "SELECT rowid, output_tokens, duration_ms, time_to_first_token_ms"
                " FROM model_usage WHERE status='completed' AND session_id=?"
                " AND query_source='main_turn' AND rowid>?"
                " ORDER BY rowid", (self.session_id, self._prev_max_rowid)).fetchall()
            con.close()
        except sqlite3.Error:
            return
        for rid, out_tok, dur_ms, ttft_ms in rows:
            self._prev_max_rowid = max(self._prev_max_rowid, rid)
            gen_ms = max((dur_ms or 0) - (ttft_ms or 0), 1)
            if out_tok:
                self.snap.tps_exact = out_tok / (gen_ms / 1000)

    # ---- part 轮询(生成中估算) ----
    def _part_loop(self):
        while not self.stop_flag.is_set():
            if self._running:
                self._poll_part()
            else:
                self._last_len = None
            self.stop_flag.wait(self.POLL_PART)

    def _poll_part(self):
        try:
            con = self._connect()
            row = con.execute(
                "SELECT length(data) FROM part"
                " WHERE session_id=? AND data LIKE '{\"type\":\"text\"%'"
                " ORDER BY rowid DESC LIMIT 1", (self.session_id,)).fetchone()
            con.close()
        except sqlite3.Error:
            return
        if not row or row[0] is None:
            return
        ln, now = row[0], time.time()
        if self._last_len is not None and ln > self._last_len:
            cps = (ln - self._last_len) / max(now - self._last_len_t, 1e-3)
            self.snap.tps_est = cps / self._chars_per_token
        self._last_len, self._last_len_t = ln, now

    # ---- 请求完成 ----
    def _on_request_done(self):
        if self._last_len:
            self._exact_out_chars = self._last_len
        try:
            con = self._connect()
            row = con.execute(
                "SELECT output_tokens, duration_ms, time_to_first_token_ms FROM model_usage"
                " WHERE status='completed' AND session_id=?"
                " ORDER BY rowid DESC LIMIT 1", (self.session_id,)).fetchone()
            con.close()
            if row and row[0]:
                out_tok, dur_ms, ttft_ms = row
                gen_ms = max((dur_ms or 0) - (ttft_ms or 0), 1)
                self.snap.tps_exact = out_tok / (gen_ms / 1000)
                if self._exact_out_chars:
                    self._chars_per_token = max(self._exact_out_chars / out_tok, 0.5)
        except sqlite3.Error:
            pass
        self.snap.tps_est = None
        self._push()

    # ---- 会话跟随:rollout 最新 mtime 的文件变了 = 活跃会话切换 ----
    def _refresh_session(self):
        new = self._latest_session()
        if not new or new == self.session_id:
            return
        self.session_id = new
        self.snap = Snapshot(state=self.snap.state, model=self.snap.model,
                             tps_exact=self.snap.tps_exact)
        self._last_len = None
        self._prev_max_rowid = self._max_usage_rowid()
        self.snap.title = self._session_title()
        self._poll_stats()
        self._init_tps_for_session()
        self._push()

    def _session_title(self) -> str:
        try:
            con = self._connect()
            row = con.execute("SELECT title FROM session WHERE id=?",
                              (self.session_id,)).fetchone()
            con.close()
            return (row[0] or "").strip()[:16] if row else ""
        except sqlite3.Error:
            return ""

    def _init_tps_for_session(self):
        """切到新会话时,用该会话最近一次完成请求的速度作为初始精确值。"""
        try:
            con = self._connect()
            row = con.execute(
                "SELECT output_tokens, duration_ms, time_to_first_token_ms FROM model_usage"
                " WHERE status='completed' AND session_id=? AND query_source='main_turn'"
                " ORDER BY rowid DESC LIMIT 1", (self.session_id,)).fetchone()
            con.close()
            if row and row[0]:
                gen_ms = max((row[1] or 0) - (row[2] or 0), 1)
                self.snap.tps_exact = row[0] / (gen_ms / 1000)
        except sqlite3.Error:
            pass

    # ---- 主循环 ----
    def run(self):
        if not self.session_id:
            return
        self.snap.title = self._session_title()
        self._poll_stats()
        self._init_tps_for_session()
        self._push()
        for target in (self._tail_loop, self._db_loop, self._part_loop):
            threading.Thread(target=target, daemon=True).start()
        last_state = None
        while not self.stop_flag.is_set():
            self._refresh_session()                  # 跟随 ZCode 会话切换
            state = "generating" if self._running else "idle"
            if state != last_state:
                last_state = state
                self.snap.state = state
                self._push()
            if self._running:
                self.snap.gen_elapsed = time.time() - self._gen_start
                self._push()
            time.sleep(0.5)

    def _push(self):
        self.snap.updated = time.time()
        try:
            self.out.put_nowait(self.snap)
        except queue.Full:
            pass

    def stop(self):
        self.stop_flag.set()


def headless_test():
    q: "queue.Queue[Snapshot]" = queue.Queue(maxsize=10)
    eng = DataEngine(q)
    eng.start()
    t0, last = time.time(), None
    while time.time() - t0 < 8:
        try:
            s = q.get(timeout=0.5)
            last = s
        except queue.Empty:
            continue
        print(f"[{time.strftime('%H:%M:%S')}] state={s.state:<10} "
              f"est={s.tps_est and f'{s.tps_est:.1f}' or '-':>6} "
              f"exact={s.tps_exact and f'{s.tps_exact:.1f}' or '-':>6} "
              f"in={s.session_in:>9,} cache={s.session_cache:>12,} "
              f"out={s.session_out:>7,} rate={s.cache_rate:.1f}% "
              f"elapsed={s.gen_elapsed:.1f}s model={s.model}")
    eng.stop()
    print("session:", eng.session_id)
    print("headless test done, last snapshot ok:", last is not None)


# ---------------------------------------------------------------- Win32 ---

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x80000
WM_NCLBUTTONDOWN, HTCAPTION = 0xA1, 0x2
HWND_TOPMOST = ctypes.c_void_p(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
SWP_NOSENDCHANGING = 0x400
DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_CORNER = 20, 33

# 消息常量(子类化用)
WM_NCHITTEST, HTCAPTION, HTTRANSPARENT = 0x84, 0x2, -1
WM_WINDOWPOSCHANGING, WM_EXITSIZEMOVE = 0x46, 0x232
WM_NCLBUTTONDOWN, WM_NCRBUTTONUP = 0xA0, 0xA5

comctl32 = ctypes.windll.comctl32
LRESULT = ctypes.c_longlong
SUBCLASSPROC = ctypes.WINFUNCTYPE(
    LRESULT, ctypes.c_void_p, ctypes.c_uint,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
comctl32.SetWindowSubclass.argtypes = [ctypes.c_void_p, SUBCLASSPROC,
                                       ctypes.c_void_p, ctypes.c_void_p]
comctl32.DefSubclassProc.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                     ctypes.c_void_p, ctypes.c_void_p]
comctl32.DefSubclassProc.restype = LRESULT
comctl32.RemoveWindowSubclass.argtypes = [ctypes.c_void_p, SUBCLASSPROC, ctypes.c_void_p]


class WINDOWPOS(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("hwndInsertAfter", ctypes.c_void_p),
                ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("cx", ctypes.c_int), ("cy", ctypes.c_int),
                ("flags", ctypes.c_uint)]

# 64 位下必须声明指针类型,否则句柄被按 32 位截断(MonitorFromWindow 返回错值)
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, wt.DWORD]
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wt.UINT]
user32.SendMessageW.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.c_void_p, ctypes.c_void_p]
user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetWindowLongW.restype = wt.LONG
user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.LONG]
user32.SetWindowLongW.restype = wt.LONG
dwmapi.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD]
user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT]
user32.GetCursorPos.argtypes = [ctypes.c_void_p]


def _enable_dpi_awareness():
    """必须在创建 Tk 窗口之前调用:声明每显示器 DPI 感知,否则被系统位图拉伸(模糊)。"""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):   # PER_MONITOR_AWARE_V2
            return
    except AttributeError:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def work_area() -> tuple[int, int, int, int]:
    rc = wt.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rc), 0)
    return rc.left, rc.top, rc.right, rc.bottom


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD)]


def monitor_workarea_of(hwnd) -> tuple[int, int, int, int]:
    """窗口当前所在显示器的工作区(多显示器下拖动/贴边都以此为准)。"""
    hmon = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), 1)   # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return work_area()
    return mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom


# ------------------------------------------------------------------- UI ---

C_BG, C_BG2, C_BORDER = "#16171c", "#1e2027", "#2c2f3a"
C_FG, C_DIM, C_ACCENT = "#e8eaf0", "#8b8f9c", "#5ad6a0"
C_WARN, C_MONO = "#e8c268", "Consolas"


class MeterApp:
    CARD_W, CARD_H = 250, 168
    BAR_H, BAR_V = 34, 46
    EDGE_NEAR = 30                    # 松手时距边缘小于此值 → 吸附

    def __init__(self):
        self.q: "queue.Queue[Snapshot]" = queue.Queue(maxsize=20)
        self.eng = DataEngine(self.q)
        self.dock: str | None = None
        self.snap = Snapshot()
        self._breath = 0.0
        self._dragging = False

        self.root = tk.Tk()
        self.scale = max(self.root.winfo_fpixels("1i") / 96.0, 1.0)   # 1.25=125% 缩放
        self.root.title("zcode-meter")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=C_BG, padx=0, pady=0)
        self.root.maxsize(16000, 16000)     # 放开 Tk 的几何钳制,防"小方块"(见子类注释)
        l, t, r, b = work_area()
        self.root.geometry(
            f"{self._s(self.CARD_W)}x{self._s(self.CARD_H)}+{r - self._s(self.CARD_W) - 24}+{t + 90}")

        self._make_fonts()
        self._build_card()
        self._rebind()
        self.root.update_idletasks()
        self.hwnd = self.root.winfo_id()

        self.root.after(80, self._win_init)       # 窗口 map 后再设 Win32 样式,Tk map 时会覆盖提前的设置
        self.root.after(100, self._poll_queue)
        self.root.after(300, self._keep_topmost)
        self.root.after(60, self._tick_breath)
        self.eng.start()

    # ---- 像素尺寸按 DPI 缩放(字体走 point 单位,Tk 自动适配) ----
    def _s(self, v: int) -> int:
        return int(round(v * self.scale))

    # ---- Win32 ----
    def _win_init(self):
        self.hwnd = self.root.winfo_id()
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        # 只加 TOOLWINDOW(不进任务栏)+TOPMOST;绝不能加 LAYERED —— 未设 alpha 的
        # 分层窗口不参与重绘,会导致黑块/残影/内容空白
        style = (style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_LAYERED
        # 不设 WS_EX_TOPMOST:exstyle 该位与 Tk 的 z-order 置顶管理拉锯,
        # 会周期性重设样式,表现为"静置几秒后第一次点击不响应"。置顶交给
        # Tk attributes(-topmost) + 低频 _assert_top 兜底。
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style)
        v = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(self.hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(v), 4)
        v2 = ctypes.c_int(2)
        dwmapi.DwmSetWindowAttribute(self.hwnd, DWMWA_CORNER, ctypes.byref(v2), 4)
        self._assert_top()

    def _assert_top(self):
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def _keep_topmost(self):
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        if not (style & WS_EX_TOOLWINDOW) or not (style & WS_EX_NOACTIVATE):
            self._win_init()                        # 只补真正会丢的样式位,不与 Tk 拉锯
        self._assert_top()
        if not getattr(self, "_dragging", False):
            self._rescue_if_offscreen()             # 拖动中不干预
        self.root.after(8000, self._keep_topmost)   # 低频:z 序兜底即可

    def _rescue_if_offscreen(self):
        """窗口完全跑出虚拟屏幕(补正叠加等 bug 的后果)则拉回主屏中央。"""
        rc = wt.RECT()
        user32.GetWindowRect(ctypes.c_void_p(self.hwnd), ctypes.byref(rc))
        vl = user32.GetSystemMetrics(76)            # SM_XVIRTUALSCREEN(本进程 DPI aware,物理像素)
        vt = user32.GetSystemMetrics(77)
        vr = vl + user32.GetSystemMetrics(78)
        vb = vt + user32.GetSystemMetrics(79)
        if rc.right <= vl or rc.bottom <= vt or rc.left >= vr or rc.top >= vb:
            l, t, r, b = work_area()
            cx = (l + r) // 2 - (rc.right - rc.left) // 2
            cy = (t + b) // 2 - (rc.bottom - rc.top) // 2
            user32.SetWindowPos(self.hwnd, None, cx, cy, 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            self.root.geometry(f"+{cx}+{cy}")

    # ---- 拖动:纯 Tk 单系统管理(v1 架构,无子类化 —— ctypes 回调在高频消息下
    # 与 Tk/GC 交锋会访问违例崩溃,已彻底弃用)。迟滞靠三点压到最低:
    # C 层直调 wm geometry + 拖动中暂停一切界面刷新 + 松手才做贴边判定 ----
    def _on_press(self, event):
        if getattr(event, "num", 1) != 1:
            return
        if self.dock:                       # 条状被按住:先切回卡片,锚在鼠标处
            self._detach_to_pointer()
        self._drag_off = (event.x_root - self.root.winfo_x(),
                          event.y_root - self.root.winfo_y())
        self._dragging = True
        dbg(f"press root=({event.x_root},{event.y_root}) win=({self.root.winfo_x()},{self.root.winfo_y()})")

    def _on_motion(self, event):
        off = getattr(self, "_drag_off", None)
        if not off:
            return
        self.root.tk.call("wm", "geometry", self.root._w,
                          f"+{event.x_root - off[0]}+{event.y_root - off[1]}")
        self._dbg_n = getattr(self, "_dbg_n", 0) + 1
        if self._dbg_n % 20 == 1:
            dbg(f"motion#{self._dbg_n} root=({event.x_root},{event.y_root})")

    def _on_release(self, event):
        if getattr(self, "_drag_off", None):
            self._drag_off = None
            self._dragging = False
            dbg(f"release win=({self.root.winfo_x()},{self.root.winfo_y()})")
            self.root.after(30, self._settle)

    def _on_enter(self, event):
        dbg(f"enter w={event.widget.winfo_class()}@({event.x},{event.y}) win=({self.root.winfo_x()},{self.root.winfo_y()})")

    def _on_leave(self, event):
        dbg(f"leave w={event.widget.winfo_class()}@({event.x},{event.y}) win=({self.root.winfo_x()},{self.root.winfo_y()})")

    def _detach_to_pointer(self):
        # 注意:本函数在 WM_NCLBUTTONDOWN 回调栈内执行,严禁调用 update_idletasks
        # 等 pump 类方法(重入 Tk 事件循环会崩溃);刷新类工作全部交给 after
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        l, t, r, b = monitor_workarea_of(self.hwnd)
        w, h = self._s(self.CARD_W), self._s(self.CARD_H)
        x = min(max(pt.x - w // 2, l + 2), r - w - 2)
        y = min(max(pt.y - self._s(12), t + 2), b - h - 2)
        self.dock = None
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self._build_card()
        self._win_init()
        self.root.after(60, lambda: self._apply_snapshot(self.snap))

    def _settle(self):
        # 原生拖动结束后:位置记录已由系统消息同步,直接读真实矩形做贴边判定
        self.root.update_idletasks()
        rc = wt.RECT()
        user32.GetWindowRect(ctypes.c_void_p(self.hwnd), ctypes.byref(rc))
        x, y, w, h = rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top
        l, t, r, b = monitor_workarea_of(self.hwnd)      # 以窗口所在显示器为准,不再夹回主屏
        near = self._s(self.EDGE_NEAR)
        want = None
        if abs(y - t) < near: want = "top"
        elif abs(y + h - b) < near: want = "bottom"
        elif abs(x - l) < near: want = "left"
        elif abs(x + w - r) < near: want = "right"
        if want and want != self.dock:
            self._set_dock(want)
        elif not want and self.dock:
            self._unset_dock()

    # ---- 布局 ----
    def _make_fonts(self):
        self.f_big = tkfont.Font(family=C_MONO, size=20, weight="bold")
        self.f_mid = tkfont.Font(family="Microsoft YaHei UI", size=9)
        self.f_num = tkfont.Font(family=C_MONO, size=9)
        self.f_lbl = tkfont.Font(family="Microsoft YaHei UI", size=8)
        self.f_vnum = tkfont.Font(family=C_MONO, size=10, weight="bold")

    def _clear(self):
        for ch in self.root.winfo_children():
            ch.destroy()

    def _rebind(self):
        stack = list(self.root.winfo_children())
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            w.bind("<Button-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_motion)
            w.bind("<ButtonRelease-1>", self._on_release)
            w.bind("<Button-3>", self._popup_menu)
            w.bind("<Enter>", self._on_enter, add="+")
            w.bind("<Leave>", self._on_leave, add="+")

    def _popup_menu(self, event):
        m = tk.Menu(self.root, tearoff=0, bg=C_BG2, fg=C_FG,
                    activebackground=C_BORDER, activeforeground=C_FG)
        m.add_command(label="贴到顶部", command=lambda: self._set_dock("top"))
        m.add_command(label="贴到底部", command=lambda: self._set_dock("bottom"))
        m.add_command(label="贴到左侧", command=lambda: self._set_dock("left"))
        m.add_command(label="贴到右侧", command=lambda: self._set_dock("right"))
        m.add_command(label="恢复卡片", command=self._unset_dock)
        m.add_separator()
        m.add_command(label="退出", command=self._quit)
        m.tk_popup(event.x_root, event.y_root)

    def _build_card(self):
        self._clear()
        r = self.root
        r.configure(bg=C_BG)
        head = tk.Frame(r, bg=C_BG); head.pack(fill="x", padx=14, pady=(12, 2))
        self.dot = tk.Label(head, text="●", bg=C_BG, fg=C_DIM, font=("Segoe UI", 9))
        self.dot.pack(side="left")
        self.state_lbl = tk.Label(head, text="空闲", bg=C_BG, fg=C_DIM, font=self.f_mid)
        self.state_lbl.pack(side="left", padx=(5, 0))
        self.title_lbl = tk.Label(head, text="当前会话", bg=C_BG, fg=C_DIM, font=self.f_lbl)
        self.title_lbl.pack(side="right")
        big = tk.Frame(r, bg=C_BG); big.pack(fill="x", padx=14, pady=(4, 0))
        self.tps_lbl = tk.Label(big, text="-- tok/s", bg=C_BG, fg=C_ACCENT, font=self.f_big)
        self.tps_lbl.pack(side="left")
        self.est_lbl = tk.Label(big, text="", bg=C_BG, fg=C_DIM, font=self.f_lbl)
        self.est_lbl.pack(side="left", padx=(4, 10), pady=(8, 0))
        self.elapsed_lbl = tk.Label(big, text="", bg=C_BG, fg=C_WARN, font=self.f_num)
        self.elapsed_lbl.pack(side="right", pady=(10, 0))
        tk.Frame(r, bg=C_BORDER, height=1).pack(fill="x", padx=10, pady=(10, 6))
        grid = tk.Frame(r, bg=C_BG); grid.pack(fill="x", padx=14)
        self.in_lbl = self._kv(grid, 0, 0, "输入")
        self.cache_lbl = self._kv(grid, 0, 2, "缓存")
        self.out_lbl = self._kv(grid, 1, 0, "输出")
        self.rate_lbl = self._kv(grid, 1, 2, "命中率")
        self.avg_lbl = self._kv(grid, 2, 0, "平均速度")
        self.model_lbl = tk.Label(r, text="", bg=C_BG, fg="#565a66", font=self.f_lbl)
        self.model_lbl.pack(anchor="w", padx=14, pady=(2, 8))
        tk.Frame(r, bg=C_BORDER, height=1).pack(fill="x", side="bottom", padx=10, pady=(0, 0))
        self._rebind()

    def _kv(self, parent, row, col, label):
        tk.Label(parent, text=label, bg=C_BG, fg=C_DIM, font=self.f_lbl).grid(
            row=row, column=col, sticky="w", padx=(0, 4), pady=1)
        v = tk.Label(parent, text="--", bg=C_BG, fg=C_FG, font=self.f_num)
        v.grid(row=row, column=col + 1, sticky="w", padx=(0, 16))
        return v

    def _build_bar(self):
        self._clear()
        r = self.root
        r.configure(bg=C_BG)
        if self.dock in ("top", "bottom"):
            box = tk.Frame(r, bg=C_BG); box.place(relx=0.5, rely=0.5, anchor="center")
            self.dot = tk.Label(box, text="●", bg=C_BG, fg=C_DIM, font=("Segoe UI", 8))
            self.dot.pack(side="left")
            self.tps_lbl = tk.Label(box, text="-- tok/s", bg=C_BG, fg=C_ACCENT, font=self.f_num)
            self.tps_lbl.pack(side="left", padx=(6, 8))
            self.avg_lbl = tk.Label(box, text="", bg=C_BG, fg=C_DIM, font=self.f_num)
            self.avg_lbl.pack(side="left", padx=(0, 14))
            self.in_lbl = tk.Label(box, text="", bg=C_BG, fg=C_DIM, font=self.f_num)
            self.in_lbl.pack(side="left", padx=(0, 14))
            self.out_lbl = tk.Label(box, text="", bg=C_BG, fg=C_DIM, font=self.f_num)
            self.out_lbl.pack(side="left", padx=(0, 14))
            self.rate_lbl = tk.Label(box, text="", bg=C_BG, fg=C_DIM, font=self.f_num)
            self.rate_lbl.pack(side="left", padx=(0, 14))
            self.elapsed_lbl = tk.Label(box, text="", bg=C_BG, fg=C_WARN, font=self.f_num)
            self.elapsed_lbl.pack(side="left")
            self.state_lbl = self.model_lbl = self.est_lbl = self.cache_lbl = None
            self.title_lbl = None
        else:
            box = tk.Frame(r, bg=C_BG); box.pack(expand=True, fill="both")
            self.dot = tk.Label(box, text="●", bg=C_BG, fg=C_DIM, font=("Segoe UI", 8))
            self.dot.pack(pady=(4, 0))
            self.tps_lbl = tk.Label(box, text="--", bg=C_BG, fg=C_ACCENT, font=self.f_vnum)
            self.tps_lbl.pack()
            tk.Label(box, text="tok/s", bg=C_BG, fg=C_DIM, font=self.f_lbl).pack()
            tk.Frame(box, bg=C_BORDER, height=1, width=24).pack(pady=4)
            self.avg_lbl = tk.Label(box, text="--", bg=C_BG, fg=C_FG, font=self.f_num)
            self.avg_lbl.pack()
            tk.Label(box, text="avg", bg=C_BG, fg=C_DIM, font=self.f_lbl).pack()
            self.in_lbl = tk.Label(box, text="--", bg=C_BG, fg=C_FG, font=self.f_num)
            self.in_lbl.pack()
            self.out_lbl = tk.Label(box, text="--", bg=C_BG, fg=C_FG, font=self.f_num)
            self.out_lbl.pack(pady=(2, 0))
            self.rate_lbl = tk.Label(box, text="", bg=C_BG, fg=C_DIM, font=self.f_lbl)
            self.rate_lbl.pack(pady=(3, 0))
            self.state_lbl = self.model_lbl = self.est_lbl = self.cache_lbl = None
            self.title_lbl = None
            self.elapsed_lbl = None
        self._rebind()

    # ---- dock ----
    def _set_dock(self, side: str):
        self.dock = side
        l, t, r, b = monitor_workarea_of(self.hwnd)
        sw, sh = r - l, b - t
        bh, bv = self._s(self.BAR_H), self._s(self.BAR_V)
        if side == "top":
            self.root.geometry(f"{sw}x{bh}+{l}+{t}")
        elif side == "bottom":
            self.root.geometry(f"{sw}x{bh}+{l}+{b - bh}")
        elif side == "left":
            self.root.geometry(f"{bv}x{sh}+{l}+{t}")
        else:
            self.root.geometry(f"{bv}x{sh}+{r - bv}+{t}")
        self._build_bar()
        self.root.update_idletasks()
        self._win_init()               # geometry 变化后重申样式与置顶
        self._apply_snapshot(self.snap)

    def _unset_dock(self):
        l, t, r, b = monitor_workarea_of(self.hwnd)
        self.dock = None
        w, h = self._s(self.CARD_W), self._s(self.CARD_H)
        x = min(max(self.root.winfo_x(), l + 8), r - w - 8)
        y = min(max(self.root.winfo_y(), t + 60), b - h - 8)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self._build_card()
        self.root.update_idletasks()
        self._win_init()
        self._apply_snapshot(self.snap)

    # ---- 菜单 ----
    # ---- 数据 ----
    def _poll_queue(self):
        if self._dragging:                          # 拖动中不做文本刷新,把重绘带宽让给窗口移动
            self.root.after(200, self._poll_queue)
            return
        try:
            while True:
                self.snap = self.q.get_nowait()
        except queue.Empty:
            pass
        self._apply_snapshot(self.snap)
        self.root.after(200, self._poll_queue)

    @staticmethod
    def _fmt_k(n: int) -> str:
        """自适应缩写:<1K 原样,之后 K/M/B。"""
        if n < 1000:
            return str(n)
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if n >= div:
                return f"{n / div:.1f}{suf}"
        return str(n)

    @staticmethod
    def _set_lbl(lbl: tk.Label | None, **kw):
        """值未变化时跳过 configure —— 消除每 200ms 的无差别微重绘。"""
        if lbl is None:
            return
        for k, v in kw.items():
            if lbl.cget(k) != v:
                lbl.configure(**{k: v})
                break

    def _apply_snapshot(self, s: Snapshot):
        generating = s.state == "generating"
        self._set_lbl(self.dot, fg=C_ACCENT if generating else C_DIM)
        self._set_lbl(self.state_lbl,
                      text=("生成中" if generating else "空闲"),
                      fg=(C_ACCENT if generating else C_DIM))
        if s.model:
            self._set_lbl(self.model_lbl, text=s.model)
        tps = s.tps_est if (generating and s.tps_est) else s.tps_exact
        txt = f"{tps:.1f}" if tps else "--"
        unit = " tok/s" if self.dock in (None, "top", "bottom") else ""
        self._set_lbl(self.tps_lbl, text=txt + unit)
        if self.avg_lbl is not None:
            if self.dock in ("top", "bottom"):
                self._set_lbl(self.avg_lbl, text=(f"均 {s.tps_avg:.1f}" if s.tps_avg else ""))
            elif self.dock in ("left", "right"):
                self._set_lbl(self.avg_lbl, text=(f"{s.tps_avg:.1f}" if s.tps_avg else "--"))
            else:
                self._set_lbl(self.avg_lbl,
                              text=(f"{s.tps_avg:.1f} tok/s" if s.tps_avg else "--"))
        self._set_lbl(self.est_lbl, text="~估算" if (generating and s.tps_est) else "")
        self._set_lbl(self.elapsed_lbl, text=f"{s.gen_elapsed:.0f}s" if generating else "")
        self._set_lbl(self.in_lbl, text=f"入 {self._fmt_k(s.session_in)}")
        self._set_lbl(self.cache_lbl, text=self._fmt_k(s.session_cache))
        self._set_lbl(self.out_lbl, text=f"出 {self._fmt_k(s.session_out)}")
        rate = (f"缓存 {s.cache_rate:.0f}%" if self.dock in (None, "top", "bottom")
                else f"{s.cache_rate:.0f}%")
        self._set_lbl(self.rate_lbl, text=rate)
        self._set_lbl(self.title_lbl, text=s.title or "当前会话")

    def _tick_breath(self):
        if self._dragging:                          # 拖动中暂停呼吸动画重绘
            self.root.after(60, self._tick_breath)
            return
        self._breath = (self._breath + 0.08) % 1.0
        if self.snap.state == "generating" and self.dot:
            a = 0.45 + 0.55 * abs((2 * self._breath) - 1)
            g = int(0x60 + 0x40 * a)
            r_ = int(0x30 + 0x18 * a)
            self.dot.configure(fg=f"#{r_:02x}{g:02x}{0x70:02x}")
        self.root.after(60, self._tick_breath)

    # ---- 自检 ----
    def _verify(self):
        self._apply_snapshot(self.snap)
        self.root.update_idletasks()
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        layered = bool(style & WS_EX_LAYERED)
        tk_top = bool(self.root.attributes("-topmost"))   # Tk 置顶走 z-order,不落 exstyle 位
        print(f"window: {self.root.winfo_width()}x{self.root.winfo_height()}"
              f" dpi-scale={self.scale:.2f} exstyle: layered={layered} topmost(tk)={tk_top}")
        stack, count_mapped = list(self.root.winfo_children()), 0
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            if isinstance(w, tk.Label):
                ok = w.winfo_ismapped() and w.winfo_width() > 2
                count_mapped += ok
                print(f"  label mapped={w.winfo_ismapped()} w={w.winfo_width():>3}"
                      f" h={w.winfo_height():>2} text={w.cget('text')!r}")
        print(f"render check: {count_mapped} labels visible, layered must be False ->",
              "PASS" if (count_mapped >= 6 and not layered and tk_top) else "FAIL")

        # 贴边判定诊断:把窗口放到当前显示器底部边缘 5px 处,松手判定应吸附为 bottom
        # 拖动绑定诊断:纯 Tk 拖动要求控件树携带 Button-1/Motion/Release 绑定
        n_bind = 0
        stack = list(self.root.winfo_children())
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            if w.bind("<Button-1>"):
                n_bind += 1
        print(f"bind check: {n_bind} widgets carry <Button-1>",
              "PASS" if n_bind >= 5 else "FAIL")
        l2, t2, r2, b2 = monitor_workarea_of(self.hwnd)
        print(f"monitor workarea: ({l2},{t2})-({r2},{b2})")
        rc = wt.RECT()
        user32.GetWindowRect(ctypes.c_void_p(self.hwnd), ctypes.byref(rc))
        vx, vy = l2 + 100, b2 - (rc.bottom - rc.top) - 5
        user32.SetWindowPos(self.hwnd, None, vx, vy, 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        self.root.geometry(f"+{vx}+{vy}")
        self.root.after(120, self._verify_dock)

    def _verify_dock(self):
        rc = wt.RECT()
        user32.GetWindowRect(ctypes.c_void_p(self.hwnd), ctypes.byref(rc))
        l, t, r, b = monitor_workarea_of(self.hwnd)
        print(f"rect before settle: ({rc.left},{rc.top})-({rc.right},{rc.bottom})"
              f" workarea ({l},{t})-({r},{b}) near={self._s(self.EDGE_NEAR)}")
        self._settle()
        print("dock after settle near bottom ->", self.dock,
              "PASS" if self.dock == "bottom" else "FAIL")
        self._quit()
        self._quit()

    def _quit(self):
        self.eng.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    if "--headless" in sys.argv:
        headless_test()
        return
    _enable_dpi_awareness()            # 必须在 Tk() 创建前
    app = MeterApp()
    if "--verify" in sys.argv:
        app.root.after(1500, app._verify)
    app.run()


if __name__ == "__main__":
    main()
