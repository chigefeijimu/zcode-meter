"""zcode-meter 数据层:日志 tail + SQLite 轮询 + 流式估算(UI 无关,tk/Qt 共用)。"""
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
from dataclasses import dataclass, field


def app_dir() -> str:
    """运行目录:PyInstaller frozen 时取 exe 所在目录(zm_*.log/zm_state.json
    都落这),脚本模式取源码目录 —— dev 行为不变。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# 崩溃追踪:pythonw 无控制台,access violation 等原生崩溃的 traceback 落盘。
# 打不开不能连启动都崩:exe 可能被放进只读目录(如未提权的 Program Files)
_CRASH_LOG = os.path.join(app_dir(), "zm_crash.log")
try:
    faulthandler.enable(open(_CRASH_LOG, "a", encoding="utf-8"))
except Exception:
    pass

ZCODE_DIR = os.path.expanduser("~/.zcode/cli")
DB_PATH = os.path.join(ZCODE_DIR, "db", "db.sqlite")
LOG_DIR = os.path.join(ZCODE_DIR, "log")
ROLL_DIR = os.path.join(ZCODE_DIR, "rollout")

DEBUG = os.environ.get("ZM_DEBUG") == "1"
DBG_PATH = os.path.join(app_dir(), "zm_debug.log")


def connect_ro() -> sqlite3.Connection:
    """只读连接:引擎轮询与 UI 侧图表/菜单查询共用,统一 open 参数。"""
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)


def today0_ms() -> int:
    """本地午夜毫秒时间戳:今日用量(_poll_stats)与按天图表(fetch_daily_usage)
    必须共用同一午夜口径,否则两张图对不上账。"""
    return int(dt.datetime.now().replace(hour=0, minute=0, second=0,
                                         microsecond=0).timestamp() * 1000)


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
    tps_avg: float | None = None        # 主力模型的会话平均 tok/s
    speed_by_model: list = None         # [(provider, model, tps, out_tokens)] 按用量降序
    today_tokens: int = 0               # 今日全部会话 token 总用量(in+out)
    last_ttft: float | None = None      # 最近完成请求的首字等待(s)
    last_duration: float | None = None  # 最近完成请求的整体耗时(s)
    session_in: int = 0                 # 总输入(input_tokens 已含缓存命中,勿再加 cache)
    session_cache: int = 0              # 其中缓存命中的部分
    session_out: int = 0
    cache_rate: float = 0.0             # cache / in
    gen_elapsed: float = 0.0
    manual: bool = False                # 统计对象被手动固定(卡片标题前缀 📌)
    updated: float = field(default_factory=time.time)


class DataEngine(threading.Thread):
    POLL_DB = 1.0
    POLL_PART = 0.5
    CHAR_PER_TOKEN_INIT = 3.2

    def __init__(self, out: "queue.Queue[Snapshot]"):
        super().__init__(daemon=True)
        self.out = out
        self.stop_flag = threading.Event()
        self.snap_lock = threading.RLock()         # 可重入:保护 snap 读写一致(多线程)
        self.manual_session: str | None = None     # 手动固定统计会话;None=自动跟随
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
        """活跃会话 = part 表最新写入行的 session(part 是流式实时写的,
        生成一开始就能识别;rollout mtime 要等请求完成,会滞后一整轮)。
        必须排除 subagent 会话:子代理的流式输出同样写 part 表,
        会把统计劫持到子代理头上。"""
        try:
            con = self._connect()
            (sid,) = con.execute(
                "SELECT session_id FROM part"
                " WHERE session_id NOT LIKE 'sess_subagent%'"
                " ORDER BY rowid DESC LIMIT 1").fetchone()
            con.close()
            return sid or ""
        except (sqlite3.Error, TypeError):
            # 兜底:part 不可用时退回 rollout mtime
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
        return connect_ro()

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
            with self.snap_lock:                    # 与会话切换互斥,防半更新快照
                con = self._connect()
                sums = con.execute(
                    "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read_input_tokens),0),"
                    " COALESCE(SUM(output_tokens),0)"
                    " FROM model_usage WHERE status='completed' AND session_id=?"
                    " AND query_source='main_turn'",
                    (self.session_id,)).fetchone()
                # 按 提供商+模型 分组的均速(Σ输出token ÷ Σ净生成时长),按总用量降序
                speed_rows = con.execute(
                    "SELECT provider_id, model_id, SUM(output_tokens),"
                    " COALESCE(SUM(MAX(duration_ms - COALESCE(time_to_first_token_ms,0), 1)),0)"
                    " FROM model_usage WHERE status='completed' AND session_id=?"
                    " AND query_source='main_turn' AND duration_ms IS NOT NULL"
                    " GROUP BY provider_id, model_id"
                    " ORDER BY SUM(input_tokens)+SUM(output_tokens) DESC",
                    (self.session_id,)).fetchall()
                speed_by_model = [
                    (p, m, (o / (d / 1000)) if o and d else None, o)
                    for p, m, o, d in speed_rows]
                today0 = today0_ms()
                # 今日用量=全部真实消耗(main_turn+subagent 等所有来源),
                # 与 ZCode 自身统计口径一致;cancelled 请求 token 为 0 无影响
                (today,) = con.execute(
                    "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
                    " FROM model_usage WHERE status='completed' AND started_at>=?",
                    (today0,)).fetchone()
                con.close()
                self.snap.session_in, self.snap.session_cache, self.snap.session_out = sums
                self.snap.speed_by_model = speed_by_model
                self.snap.tps_avg = speed_by_model[0][2] if speed_by_model else None
                self.snap.today_tokens = today
                # input_tokens 已含 cache_read:命中率 = cache / in,分母不再加 cache
                self.snap.cache_rate = (self.snap.session_cache / self.snap.session_in * 100
                                        if self.snap.session_in else 0.0)
                if speed_by_model:
                    self.snap.model = speed_by_model[0][1]
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
                self.snap.last_ttft = (ttft_ms or 0) / 1000
                self.snap.last_duration = (dur_ms or 0) / 1000

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
                self.snap.last_ttft = (ttft_ms or 0) / 1000
                self.snap.last_duration = (dur_ms or 0) / 1000
                if self._exact_out_chars:
                    self._chars_per_token = max(self._exact_out_chars / out_tok, 0.5)
        except sqlite3.Error:
            pass
        self.snap.tps_est = None
        self._push()

    # ---- 会话跟随:part 最新写入的会话变了 = 活跃会话切换 ----
    def _refresh_session(self):
        # 整体持锁(含 manual 检查与 _latest_session 查询):否则 UI 线程
        # set_manual_session 在「检查 manual→查最新会话」之间挤入,固定会被
        # 一次已过检查的自动切换覆盖,且永不自愈。RLock 可重入,_switch_session
        # 内再取锁不死锁;_latest_session 热缓存 ~1ms,持锁代价可接受。
        with self.snap_lock:
            if self.manual_session:                 # 手动固定期间不跟随
                return
            new = self._latest_session()
            if not new or new == self.session_id:
                return
            self._switch_session(new)

    def _switch_session(self, new: str):
        """切到指定会话(自动跟随/手动固定共用)。序列不可乱:
        - 先重建 Snapshot:防数据线程读到半更新快照(v0.2.0 修过的老 bug)
        - _last_len=None:否则用两会话 part 长度差算出错误 tps_est
        - _prev_max_rowid 重置:否则旧会话基线带进新会话,漏读/重读完成请求"""
        with self.snap_lock:
            self.session_id = new
            self.snap = Snapshot(state=self.snap.state, model=self.snap.model,
                                 tps_exact=self.snap.tps_exact,
                                 manual=bool(self.manual_session))
            self._last_len = None
            self._prev_max_rowid = self._max_usage_rowid()
            self.snap.title = self._session_title()
            self._poll_stats()
            self._init_tps_for_session()
            self._push()

    # ---- 手动固定 / 恢复自动(UI 线程调用) ----
    def set_manual_session(self, sid: str):
        """固定统计对象为 sid。与当前相同的会话也走完整切换:snap.manual
        标记(📌)必须随重建生效。"""
        with self.snap_lock:
            if not sid:
                return
            self.manual_session = sid
            self._switch_session(sid)

    def clear_manual_session(self):
        """恢复自动跟随,并立即对齐当前最新活跃会话。"""
        with self.snap_lock:
            self.manual_session = None
            self._switch_session(self._latest_session() or self.session_id)

    # ---- UI 侧查询(右键会话菜单 / 历史图表窗口,均只读独立连接) ----
    def recent_sessions(self, limit: int = 8) -> list:
        """最近会话(手动切换菜单用):按 part 每会话最新写入倒序、排除
        subagent,与 _latest_session 跟随口径同源。LEFT JOIN 取标题:
        标题缺失时列出空标题而非丢会话(与 _session_title 行为一致)。"""
        try:
            con = connect_ro()
            rows = con.execute(
                "SELECT p.session_id, s.title FROM"
                " (SELECT session_id, MAX(rowid) AS mr FROM part"
                "  WHERE session_id NOT LIKE 'sess_subagent%' GROUP BY session_id) p"
                " LEFT JOIN session s ON s.id = p.session_id"
                " ORDER BY p.mr DESC LIMIT ?", (limit,)).fetchall()
            con.close()
            return [(sid, (title or "").strip()[:16]) for sid, title in rows]
        except sqlite3.Error:
            return []

    def fetch_daily_usage(self, days: int = 30) -> list:
        """按天 token 用量(历史图表):completed 全来源 in+out,与今日用量
        同口径(input 已含 cache_read 勿重复加);起点=(days-1) 天前本地午夜,
        days=1 时与今日用量 SQL 完全同界。天界用 SQLite 'localtime',依赖
        OS 时区设置。"""
        try:
            con = connect_ro()
            rows = con.execute(
                "SELECT date(started_at/1000, 'unixepoch', 'localtime') AS d,"
                " COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
                " FROM model_usage WHERE status='completed' AND started_at>=?"
                " GROUP BY d ORDER BY d",
                (today0_ms() - (days - 1) * 86_400_000,)).fetchall()
            con.close()
            return rows
        except sqlite3.Error:
            return []

    def fetch_session_usage(self, limit: int = 20) -> list:
        """按会话 token 用量(历史图表):completed + main_turn、排除 subagent
        会话 —— 与卡片统计逐字对齐(README 口径表)。返回 (sid,title,tokens,
        请求数),按会话最近请求时间倒序。注意:普通会话内也混有 compact/
        workflow_child 等非 main_turn 来源,不加 query_source 过滤必与卡片
        对不上而被当 bug 报。"""
        try:
            con = connect_ro()
            rows = con.execute(
                "SELECT mu.session_id, COALESCE(s.title,''),"
                " COALESCE(SUM(mu.input_tokens),0)+COALESCE(SUM(mu.output_tokens),0),"
                " COUNT(*)"
                " FROM model_usage mu LEFT JOIN session s ON s.id = mu.session_id"
                " WHERE mu.status='completed' AND mu.query_source='main_turn'"
                " AND mu.session_id NOT LIKE 'sess_subagent%'"
                " GROUP BY mu.session_id"
                " ORDER BY MAX(mu.started_at) DESC LIMIT ?", (limit,)).fetchall()
            con.close()
            return rows
        except sqlite3.Error:
            return []

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
                self.snap.last_ttft = (row[2] or 0) / 1000
                self.snap.last_duration = (row[1] or 0) / 1000
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


