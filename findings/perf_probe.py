#!/usr/bin/env python3
"""T0 性能探针:三条热点 SQL 现形态计时 + EXPLAIN + 空闲 tick SQL 计数框架。

v0.9『Live 实时刷新』批的改前基线固化与复测钦定工具(T0 落地;T1/T2/T3
改完复跑第 1b/2 节对验收线,T4 改完复跑第 3 节看『空闲 0』,T7 取前后两跑
出对照表)。只读红线:全程经 connect_ro(mode=ro) 只读连接,不写 ZCode/
Claude 任何文件;不 import app(UI 零依赖);ZM_NO_STATE=1 守卫 + 强制摘
ZM_DEBUG(否则引擎 dbg() 会往仓库根追加 zm_debug.log,污染工作区)。

== 改前基线(2026-09-30 评审两轮实测,历史锚点;行号为改前 data_engine) ==
SQL1 _global_part_tps 现形态(改前 data_engine.py:1240-1244,『4000 行
     窗口』GROUP BY):两轮 80.70 / 45.95 ms,算术均值 63.3 ms;
     EXPLAIN 关键行 SCAN part USING INDEX part_session_idx —— 窗口被
     计划击穿成 part 全索引扫描(registry open#28)。
SQL2 _global_completed_tps 现形态(改前 data_engine.py:1273-1277,
     completed_at>=? 单前置):两轮 39.70 / 33.71 ms,算术均值 36.7 ms;
     EXPLAIN 关键行 SCAN model_usage(completed_at 无索引,open#29)。
SQL3 recent_sessions 现形态(改前 data_engine.py:1396-1402,全表
     GROUP BY + ORDER BY):两轮 67.80 / 54.88 ms,算术均值 61.3 ms;
     EXPLAIN 关键行 SCAN part USING COVERING INDEX part_session_idx +
     USE TEMP B-TREE FOR ORDER BY(open#30)。
会话聚合段(_poll_stats 的 sums+speed 两 SQL,改前 data_engine.py:
     1070-1085):两轮 48.05+46.63 / 58.70+62.90 ms,段合计
     94.7~121.6 ms(spec 摘记 ~96-121ms;走 model_usage_query_source_idx
     过滤全部 main_turn 行后聚合,本批 spec nonGoals 钉死不修,registry
     另开 open 项)。
字面缓存刷新形态(反面教材,已否决):completed_at>=now-2*LOOKBACK 单
     前置,评审实测 41.37 ms/次、EXPLAIN=SCAN model_usage —— 活跃期闸门
     每拍都开时等于每秒一条全表扫,比改前还慢(B2');T2 的缓存刷新必须
     与主查询同用 started_at>=cut-2*LOOKBACK AND completed_at>=cut 前置
     (评审实测 0.192 ms、SEARCH model_usage_started_model_idx、行集等价
     366==366;2h lookback 依据:本库 MAX(completed_at-started_at)=
     MAX(duration_ms)=3,852,050ms=64.2min,超 2h 行数=0)。

== SQL 计数口径(与 T4 闸门管辖段严格一致,『空闲 0』验收只对本口径) ==
管辖段 = _db_loop(改前 data_engine.py:1059-1063)tick 内经 connect_ro
发出的全部 ZCode DB SQL:_poll_stats 内部 9 条(sums/speed 分组/rlast/
today/cost/burn/global part 探针/global completed 探针/ZCodeSource.
today_usage 分量)+ _check_activity 的 _max_completed_rowid 水位 1 条 +
_poll_new_completed 基线 1 条,改前合计 ~11 条/tick(第 3 节实测验证)。
计数实现:connect_ro 返回的连接挂 sqlite3.set_trace_callback,按发起线程
归因(只统计 _db_loop 线程 = 闸门管辖段;其他线程 SQL 单列报告,恒为 0)。

两条例外(不在『空闲 0』口径内,T4 spec 钉死豁免,输出头部恒打印):
例外1 _refresh_session→_latest_session:run() 主循环每 0.5s 一条的既存
     查询(评审实测 0.011ms;ORDER BY rowid DESC LIMIT 1 反向短路,代价
     随 part 表顶部 dwf/subagent 积压深度波动),不进闸门;本探针空闲窗口
     刻意不启动 run() 主循环,窗口只量管辖段。
例外2 today0 时间闸强制开闸:观测窗跨午夜(today0_ms() 变化)时 T4 强制
     开闸一拍保午夜清零口径(B1')—— 该拍 SQL 属合法刷新,不计『空闲 0』。

闸门模拟(探针侧独立观察,语义与 T4 闸门同源,两态通用):每拍比较
db.sqlite 与 db.sqlite-wal 各一次 os.stat 的 (st_mtime_ns, st_size),
两文件均未变才判『关』;-shm 不进闸门(读者侧假阳性);stat OSError
(文件不存在:DELETE 模式/首启)按该文件『维持原状』处理(N2)。时间闸 =
today0_ms() 较上一拍变化即强制开闸。T4 落地前它是『若闸门存在会怎样』
的对照;落地后与引擎侧闸门互为印证(引擎关 → 探针计数自然归零)。

用法(仓库根):python findings/perf_probe.py [--idle-ticks 60] [--best-of 5]
  --idle-ticks 0 跳过第 3 节(快速冒烟);默认 60 拍,墙钟约 1.5~2 分钟。

复用指南(后续 perf ticket):
- 改前列 = 第 1a 节(票面钉死的三条改前 SQL,T1/T2/T3 落地与否都不变);
  改后列 = 第 1b 节(trace 捕获当前引擎方法实际发出的语句,改形态后无需
  改本探针):T1/T2/T3 验收线 0.5/0.5/1 ms 看 1b 的方法整链计时,EXPLAIN
  应离开 1a 的三种 SCAN 形态;
- T4 落地后复跑第 3 节:『关』批管辖 SQL 应 == 0(首拍/db 变化/时间闸拍
  除外,例外见头部声明);
- T7:改前(头部注释的两轮锚点 + 1a 实测)与终版两跑的输出直接进
  CHANGELOG 对照表。

并行执行现实(落地时实测记录,2026-09-30):本探针编写期间,同批并行
ticket 陆续把 T1/T2/T3/T6 与 T4 写进工作副本(data_engine.py 一日内多次
变更;10:33 的一次探针运行恰撞上 T4 半落地态:_db_loop 线程立即死亡零
SQL,探针的存活守卫如实报『测量无效』,随后 10:34 版复跑正常)。因此
本探针按双状态设计:改前形态在 1a 节按票面钉死(改前列可复现),现形
态按引擎实况捕获(1b 节,改后列自动跟进),第 3 节计数框架对无闸门/
有闸门两种引擎都成立(T4 前:每拍全量 SQL;T4 后:引擎闸门关时探针
计数自然归零)。
"""
from __future__ import annotations

import argparse
import os
import queue
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

# --- 守卫必须先于包导入(顺序是契约)---
# ZM_NO_STATE:与 tests/run_all.py 的 STATE_OFF 同纪律 —— 引擎侧一切落盘/
# 网络路径(QuotaMonitor、zm_alerts.json)在探针进程内恒关闭;
# ZM_DEBUG 强制摘除:DEBUG 在 data_engine 模块级读环境变量,带着 1 运行会
# 让引擎 dbg() 往仓库根追加 zm_debug.log —— 探针是只读工具,不得新增
# zm_* 运行期文件(T7 验收『工作区无 zm_* 污染』)。
os.environ.setdefault("ZM_NO_STATE", "1")
os.environ.pop("ZM_DEBUG", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import zcode_meter.data_engine as de      # noqa: E402  只进数据层,UI 零依赖
import zcode_meter.sources.zcode as zsrc  # noqa: E402
from zcode_meter.sources.zcode import DB_PATH, today0_ms  # noqa: E402

# 重定向/管道下非控制台 stdout 走 locale ANSI(cp936)严格编码:与 __main__
# 同款 errors="replace" 兜底,最坏单个字符变 '?',绝不让整份报告丢在 print。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

IDLE_TICKS = 60       # 空闲窗口拍数(票面口径:空闲 60s tick)
BENCH_N = 5           # 每条 SQL best-of-N(票面口径 best-of-5)
TICK_GAP_SPLIT = 0.7  # 拍批次切分阈值:_db_loop 拍间隔实测 >=0.83s
                      # (wait(POLL_DB)=1s 起步,实测略有提前),拍内 SQL 相邻
                      # 毫秒级(claude TTL 冷扫最坏 ~0.2s),取两者之间 ——
                      # 启发式,第 3 节输出注明
WATCH_DT = 0.2        # 闸门模拟的 stat 采样粒度(归因误差 ≤ 本值,输出注明)

_ORIG_CONNECT = zsrc.connect_ro   # 唯一定义的原始只读连接工厂,patch 后还原用

# 改前基线(两轮实测,来源见模块 docstring)—— 与本轮实测并排打印,T7
# 对照表直接取两列;均值在代码里算,防手算抄错。
BASELINE = {
    "SQL1": (80.70, 45.95),
    "SQL2": (39.70, 33.71),
    "SQL3": (67.80, 54.88),
}
# spec 验收线(T1/T2/T3 落地后复跑第 1b 节应低于;SQL3 是菜单暖读线 1ms)
ACCEPT = {"SQL1": 0.5, "SQL2": 0.5, "SQL3": 1.0}

# 已否决的字面缓存刷新形态(B2' 反面教材):与改前 _global_completed_tps
# (=PRE_SQL2)逐字同文,唯参数从 cut(now-10s)换成 now-2*LOOKBACK ——
# 评审实测该形态 41.37 ms/次、EXPLAIN=SCAN model_usage。此处保留计时是
# 给 T2/T7 的可复现反面对照,不是推荐形态。
REJ_CACHE_REFRESH_SQL = (
    "SELECT output_tokens, first_token_at, completed_at"
    " FROM model_usage WHERE status='completed' AND completed_at>=?"
    " AND output_tokens>0 AND first_token_at IS NOT NULL")
REJ_LOOKBACK_MS = 2 * 3600_000   # T2 COMPLETED_LOOKBACK_MS 计划值(2h 裕量
                                 # 依据见模块 docstring:本库最长请求 64.2min)

# 改前形态三条(T0 票面钉死):SQL 文本逐字取自改前 data_engine.py
# (1240-1244 / 1273-1277 / 1396-1402),与头部基线注释同源。钉死的意义:
# 引擎现形态会随 T1/T2/T3 改写(落地时工作副本已改),而票面验收点名的
# 三种关键计划行(SCAN part USING INDEX part_session_idx / SCAN
# model_usage / COVERING INDEX part_session_idx + TEMP B-TREE)必须『各就
# 各位』地出现在改前列 —— 这列证据不能依赖引擎当前长什么样。改后 SQL1
# 仍活在 T1 回退重建路径(_rebuild_part_baselines)、SQL3 仍活在 T3 冷
# 回退(_recent_sessions_sql),SQL2 仅存于本探针反面形态对照(逐字同文)。
PRE_SQL1 = ("SELECT session_id, MAX(rowid), length(data) FROM part"
            " WHERE data LIKE '{\"type\":\"text\"%'"
            " AND rowid > (SELECT MAX(rowid) FROM part) - 4000"
            " GROUP BY session_id")
PRE_SQL2 = ("SELECT output_tokens, first_token_at, completed_at"
            " FROM model_usage WHERE status='completed' AND completed_at>=?"
            " AND output_tokens>0 AND first_token_at IS NOT NULL")
PRE_SQL3 = ("SELECT p.session_id, s.title FROM"
            " (SELECT session_id, MAX(rowid) AS mr FROM part"
            "  WHERE session_id NOT LIKE 'sess_subagent%'"
            "    AND session_id NOT LIKE 'sess_dwf-%' GROUP BY session_id) p"
            " LEFT JOIN session s ON s.id = p.session_id"
            " ORDER BY p.mr DESC LIMIT ?")

_STR_RE = re.compile(r"'(?:[^']|'')*'")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def sql_shape(sql: str) -> str:
    """展开形 SQL → 形状键:字符串/数字字面量归一为 '?'/?。
    今日用量在 _poll_stats 与 ZCodeSource.today_usage 是逐字同文双跑
    (open#20),cost/burn 是同文不同参 —— 不归一就无法把它们各并成一行
    并数出每拍真实条数。"""
    s = " ".join(sql.split())
    s = _STR_RE.sub("'?'", s)
    s = _NUM_RE.sub("?", s)
    return s


def annotate(shape: str) -> str:
    """形状键 → 人话标签(尽力而为;匹配不上返回空串、原样打印,证据以
    SQL 文本为准)。模式全部针对归一后的形状键(含 '?'/? 占位)。先匹配
    T1/T2/T3 落地后的新形态,再匹配改前形态 —— 同一探针要在改前/改后
    两种引擎状态下都能读得懂。"""
    # ---- T1/T2/T3 后的新形态 ----
    if "COALESCE(MAX(rowid),?) FROM part" in shape:
        return "T1 水位(MAX(rowid) FROM part)"
    if "substr(data,?,?)" in shape and "FROM part" in shape:
        return "T1 走读(rowid>?,只读新行)"
    if "WHERE rowid IN (?" in shape and "length(data)" in shape:
        return "T1 长度探针(rowid IN 定点重读)"
    if "SELECT session_id, MAX(rowid) FROM part GROUP BY session_id" in shape:
        return "T1/T3 全量重建 GROUP BY(session_id,MAX(rowid))"
    if "SELECT id, title FROM session WHERE id IN" in shape:
        return "T3 title 批查(session.id IN)"
    if ("first_token_at IS NOT NULL" in shape
            and "started_at>=?" in shape):
        return "SQL2/T2 现形态缓存换行(started_at 前置)"
    # ---- 改前形态(票面三条;改前 SQL2 = completed_at-only 单前置)----
    if "data LIKE" in shape and "FROM part" in shape:
        return "SQL1 改前形态(4000 行窗口 GROUP BY)"
    if "first_token_at IS NOT NULL" in shape and "completed_at>=?" in shape:
        return "SQL2 改前形态(completed_at>=?)"
    if "SELECT p.session_id" in shape:
        return "SQL3 改前形态(全表 GROUP BY+LEFT JOIN)"
    # ---- 两条既有不变段 ----
    if "AND rowid>? ORDER BY rowid" in shape:
        return "_poll_new_completed 基线"
    if "COALESCE(MAX(rowid),?) FROM model_usage WHERE status='?'" in shape:
        return "_check_activity 水位(_max_completed_rowid)"
    if "COALESCE(duration_ms,?) - COALESCE(time_to_first_token_ms,?)" in shape:
        return "rlast 瞬时燃速"
    if "started_at>=?" in shape and "GROUP BY provider_id, model_id" in shape:
        return "cost/burn 分组(同文双参数)"
    if "GROUP BY provider_id, model_id" in shape:
        return "会话 speed 分组(open 项)"
    if shape.startswith("SELECT COALESCE(SUM(input_tokens),?),"):
        return "会话 sums(open 项)"
    if ("COALESCE(SUM(input_tokens),?)+COALESCE(SUM(output_tokens),?)"
            in shape and "GROUP BY" not in shape):
        return "今日用量(_poll_stats 与 ZCodeSource 同文双跑,open#20)"
    if "FROM part" in shape and "ORDER BY rowid DESC LIMIT ?" in shape:
        return "_latest_session(例外1)"
    return ""


def bench_ms(fn, n: int) -> float:
    """best-of-N 毫秒计时:取最小值(票面口径 best-of-5)—— 同机噪声
    (页缓存抖动/后台写)只会把单拍变慢,取最小才量得出 SQL 本征成本。"""
    best = float("inf")
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best * 1000.0


def _run_traced(fn) -> list:
    """跑一次引擎方法并捕获其发出的 SQL 文本。trace callback 给出参数已
    内联的展开形,可直接回放计时/EXPLAIN —— 探针因此不硬编码引擎 SQL,
    T1/T2/T3 改形态后无需改本文件即可复测。捕获只跑一次且不计时:计时阶
    段不挂 callback,免得把 Python 回调开销算进 SQL 成本。"""
    texts: list = []

    def cap_connect():
        con = _ORIG_CONNECT()
        con.set_trace_callback(texts.append)
        return con

    # 两个模块全局都要 patch:data_engine 顶部 re-import 了 connect_ro,
    # 引擎 _connect/recent_sessions 走 data_engine 名字空间,而
    # ZCodeSource.today_usage 走 zcode.py 名字空间 —— 单 patch 一个会漏计数。
    de.connect_ro = zsrc.connect_ro = cap_connect
    try:
        fn()
    finally:
        de.connect_ro = zsrc.connect_ro = _ORIG_CONNECT
    return texts


def _dedupe_shapes(texts: list) -> list:
    """trace 捕获文本 → [(形状键, 首个原文, 出现次数)](保序)。"""
    order, agg = [], {}
    for sql in texts:
        sh = sql_shape(sql)
        if sh not in agg:
            agg[sh] = [0, sql]
            order.append(sh)
        agg[sh][0] += 1
    return [(sh, agg[sh][1], agg[sh][0]) for sh in order]


def print_plan(con, sql: str, indent: str = "    ", params=()) -> None:
    """EXPLAIN QUERY PLAN 全行打印;SCAN/SEARCH/TEMP B-TREE 关键行加 >>
    (票面验收点名三种关键计划行,标记让肉眼对表免翻行)。"""
    try:
        rows = con.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    except sqlite3.Error as exc:
        print(f"{indent}EXPLAIN 失败: {exc}")
        return
    for _id, _parent, _notused, detail in rows:
        key = ("SCAN " in detail or "SEARCH " in detail
               or "TEMP B-TREE" in detail)
        print(f"{indent}{'>> ' if key else '   '}{detail}")


def stat_pair():
    """T4 文件闸门的同源输入:db.sqlite 与 db.sqlite-wal 各一次 os.stat 的
    (st_mtime_ns, st_size)。stat OSError(文件不存在)→ None,按该文件
    『维持原状』处理(N2);-shm 不进闸门(读者侧假阳性)。"""
    out = []
    for p in (DB_PATH, DB_PATH + "-wal"):
        try:
            st = os.stat(p)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


# ------------------------------------------------------------- 头部声明 ---

def print_header(eng, con) -> None:
    print("=" * 74)
    print("zcode-meter perf_probe(T0)· 三条热点 SQL + 空闲 SQL 计数 · 只读")
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"DB: {DB_PATH}")
    print(f"  db.sqlite {stat_pair()[0]} | db.sqlite-wal {stat_pair()[1]}"
          "(-shm 不进闸门)")
    try:
        sizes = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("part", "model_usage", "session")}
        print(f"  表规模: part {sizes['part']:,} 行 / "
              f"model_usage {sizes['model_usage']:,} 行 / "
              f"session {sizes['session']:,} 行")
    except sqlite3.Error as exc:
        print(f"  表规模查询失败: {exc}")
    print(f"  引擎常量: POLL_DB={eng.POLL_DB}s "
          f"THROUGHPUT_WINDOW_S={eng.THROUGHPUT_WINDOW_S}s")
    print(f"  跟随会话(会话聚合段参数): {eng.session_id or '(空)'}")
    print("-" * 74)
    print("SQL 计数口径(= T4 闸门管辖段):_db_loop tick 内经 connect_ro 发出的")
    print("  全部 ZCode DB SQL —— _poll_stats 内部各条(sums/speed/rlast/today/cost/")
    print("  burn/global part 探针/global completed 探针/ZCodeSource.today_usage)")
    print("  + _check_activity 水位 + _poll_new_completed 基线;改前形态合计")
    print("  ~11 条/tick,T1/T2/T3 落地后为水位/走读/探针/缓存换行等新组合,")
    print("  实际条数一律以第 3 节实测为准。")
    print("两条例外(不在『空闲 0』口径内,恒声明):")
    print("  例外1 _refresh_session→_latest_session:run() 主循环每 0.5s 一条既存查询")
    print("        (评审实测 0.011ms),不进闸门;本探针空闲窗口刻意不启动该循环。")
    print("  例外2 today0 时间闸强制开闸:观测窗跨午夜(today0_ms 变化)时 T4 强制")
    print("        开闸一拍保午夜清零口径,该拍 SQL 不计入『空闲 0』。")
    print("=" * 74)


# ------------------------------------------------- 第 1a 节:改前形态钉死 ---

def section1_pre(con, n: int, window_s: float) -> bool:
    """改前列:票面钉死的三条改前 SQL(文本见 PRE_SQL* 常量注释)。钉死
    的意义:验收点名的三种关键计划行必须在此『各就各位』,不受引擎后续
    改写影响。"""
    print(f"\n== 第 1a 节:三条改前形态计时(best-of-{n},票面钉死 SQL)==")
    cut_ms = int((time.time() - window_s) * 1000)   # 与改前 _global_completed_
    # tps 同参:cut = now - THROUGHPUT_WINDOW_S(10s)
    ok = True
    for tag, sql, params in (("SQL1", PRE_SQL1, ()),
                             ("SQL2", PRE_SQL2, (cut_ms,)),
                             ("SQL3", PRE_SQL3, (8,))):
        b0, b1 = BASELINE[tag]
        try:
            ms = bench_ms(lambda: con.execute(sql, params).fetchall(), n)
        except sqlite3.Error as exc:
            print(f"-- {tag} 改前形态执行失败: {exc}")
            ok = False
            continue
        print(f"-- {tag} 改前形态 best-of-{n}: {ms:.2f} ms"
              f"(评审两轮 {b0:.2f}/{b1:.2f},均值 {(b0 + b1) / 2:.1f} ms)")
        print("   EXPLAIN QUERY PLAN:")
        print_plan(con, sql, "     ", params)
    return ok


# ------------------------------------------------- 第 1b 节:现形态(trace) ---

def section1_live(eng, con, n: int, timed: dict) -> bool:
    """改后列:trace 捕获当前引擎方法实际发出的语句并逐条计时/EXPLAIN ——
    计时走引擎方法本身,T1/T2/T3/T7 改形态后无需改本探针即可复测。捕获跑
    的是该方法的首拍(SQL1 首拍会触发基线重建路径,含旧窗口 SQL 与全量
    GROUP BY),常态路径的语句组合见第 2 节 _poll_stats 捕获。"""
    print(f"\n== 第 1b 节:三条现形态计时(best-of-{n},trace 捕获当前引擎)==")
    targets = [
        ("SQL1", "全局吞吐·流式贡献 _global_part_tps(registry open#28)",
         lambda: eng._global_part_tps(time.time())),
        ("SQL2", "全局吞吐·完成贡献 _global_completed_tps(registry open#29)",
         lambda: eng._global_completed_tps(time.time(),
                                           eng.THROUGHPUT_WINDOW_S)),
        ("SQL3", "右键会话菜单 recent_sessions(8)(registry open#30)",
         lambda: eng.recent_sessions(8)),
    ]
    ok = True
    for tag, desc, fn in targets:
        b0, b1 = BASELINE[tag]
        print(f"\n-- {tag} {desc} --")
        try:
            texts = _run_traced(fn)      # 捕获(首拍;含基线重建/冷缓存路径)
            method_ms = bench_ms(fn, n)  # 方法整链 = 生产每秒实付(新开连接)
        except Exception as exc:
            print(f"   执行失败: {type(exc).__name__}: {exc}")
            ok = False
            continue
        print(f"   方法整链 best-of-{n}(含每次新开只读连接): {method_ms:.2f} ms")
        print(f"   改前基线: {b0:.2f}/{b1:.2f} ms(两轮均值 {(b0 + b1) / 2:.1f})"
              f" | 验收线: <= {ACCEPT[tag]:g} ms")
        for sh, sql, cnt in _dedupe_shapes(texts):
            label = annotate(sh)
            try:
                ms = bench_ms(lambda s=sql: con.execute(s).fetchall(), n)
            except sqlite3.Error as exc:
                print(f"   [{label or 'SQL'}] 回放计时失败: {exc}")
                ok = False
                continue
            timed[sh] = ms
            print(f"   [{label or 'SQL'}] x{cnt} 裸 SQL best-of-{n}: {ms:.2f} ms"
                  "(共享一条热连接)")
            print(f"      {sql}")
            print("      EXPLAIN QUERY PLAN:")
            print_plan(con, sql, "        ")
    return ok


# --------------------------------------- 第 1c 节:例外1 实测 + 反面形态 ---

def section1_extras(eng, con, n: int) -> bool:
    print(f"\n== 第 1c 节:例外1 实测 + 反面形态对照(best-of-{n})==")
    ok = True
    print("\n-- 例外1(_latest_session;声明项,不进闸门、不计入空闲窗口)--")
    try:
        texts = _run_traced(eng._latest_session)
        if texts:
            sql = texts[0]
            bare = bench_ms(lambda: con.execute(sql).fetchone(), n)
            meth = bench_ms(eng._latest_session, n)
            print(f"   裸 SQL best-of-{n}: {bare:.3f} ms | "
                  f"方法整链(新开连接): {meth:.3f} ms")
            print("   评审实测 0.011ms;run() 主循环每 0.5s 一条既存查询,"
                  "代价随 part 顶部 dwf/subagent 积压深度波动")
            print(f"      {sql}")
    except Exception as exc:
        print(f"   执行失败: {type(exc).__name__}: {exc}")
        ok = False

    # 反面形态对照(已否决):字面缓存刷新 completed_at>=now-2h
    print("\n-- 反面形态(已否决,勿采用):字面缓存刷新 completed_at>=now-2h --")
    try:
        rej_param = (int(time.time() * 1000) - REJ_LOOKBACK_MS,)
        rej_ms = bench_ms(
            lambda: con.execute(REJ_CACHE_REFRESH_SQL, rej_param).fetchall(), n)
        print(f"   裸 SQL best-of-{n}: {rej_ms:.2f} ms(评审实测 41.37 ms)"
              " | EXPLAIN:")
        print_plan(con, REJ_CACHE_REFRESH_SQL, "     ", rej_param)
        print("   已否决(B2'):活跃期闸门每拍开 → 每秒一条全表扫,比改前还慢;")
        print("   T2 缓存刷新必须与主查询同用 started_at>=cut-2h AND "
              "completed_at>=cut 前置")
        print("   (评审实测 0.192 ms、SEARCH model_usage_started_model_idx、"
              "行集等价 366==366)")
    except sqlite3.Error as exc:
        print(f"   执行失败: {exc}")
        ok = False
    return ok


# ------------------------------------------- 第 2 节:会话聚合段 + 整段 ---

def section2(eng, con, n: int, timed: dict) -> None:
    print(f"\n== 第 2 节:会话聚合段(open 项,本批不修)+ _poll_stats 整段 ==")
    if not eng.session_id:
        print("   (跟随会话为空:会话级 SQL 匹配不到行,计时只反映空扫,"
              "不进对照表)")
    texts = _run_traced(eng._poll_stats)
    print(f"   _poll_stats 一次 trace 捕获 {len(texts)} 条 SQL,"
          f"按形状去重计时(EXPLAIN 逐条):")
    sess_pair = []
    for sh, sql, cnt in _dedupe_shapes(texts):
        label = annotate(sh)
        if sh in timed:
            ms, src = timed[sh], "(已在第 1b 节计时,不重复)"
        else:
            try:
                ms = bench_ms(lambda s=sql: con.execute(s).fetchall(), n)
            except sqlite3.Error as exc:
                print(f"   - {label or sh[:60]} x{cnt}: 回放计时失败 {exc}")
                continue
            timed[sh] = ms
            src = ""
        if label.startswith("会话 sums") or label.startswith("会话 speed"):
            sess_pair.append(ms)
        print(f"   - {label or sh[:60]} x{cnt}: {ms:.2f} ms best-of-{n} {src}")
        print_plan(con, sql, "       ")
    if len(sess_pair) == 2:
        print(f"   会话聚合段(sums+speed)合计: {sum(sess_pair):.2f} ms"
              "(改前基线段合计 94.7~121.6 ms,")
        print("   两轮 48.05+46.63 / 58.70+62.90;spec nonGoals 钉死本批不修,"
              "registry 另开 open 项)")
    try:
        poll_ms = bench_ms(eng._poll_stats, n)
        print(f"   _poll_stats 方法整链 best-of-{n}(每秒一拍的 snap_lock 内"
              f"成本): {poll_ms:.2f} ms(registry open#29:改前评审实测 224-261 ms)")
    except Exception as exc:
        print(f"   _poll_stats 计时失败: {type(exc).__name__}: {exc}")


# --------------------------------------------- 第 3 节:空闲窗口计数框架 ---

def _split_batches(gov_events: list) -> list:
    """拍批次切分(启发式):相邻事件间隔 > TICK_GAP_SPLIT 即断批 ——
    _db_loop 拍间隔恒 >= wait(POLL_DB)=1s,拍内 SQL 相邻毫秒级。"""
    batches, cur = [], []
    for ev in gov_events:
        if cur and ev[0] - cur[-1][0] > TICK_GAP_SPLIT:
            batches.append(cur)
            cur = []
        cur.append(ev)
    if cur:
        batches.append(cur)
    return batches


def _sample_at(samples: list, t: float):
    """t 时刻的闸门输入 = t 之前最近一次采样(采样粒度 WATCH_DT,归因误差
    ≤ WATCH_DT —— T4 现场是拍起点 os.stat,这里用最近采样近似)。"""
    hit = samples[0]
    for s in samples:
        if s[0] <= t:
            hit = s
        else:
            break
    return hit


def _diff_detail(pa, pb) -> str:
    ch = [name for idx, name in ((0, "db.sqlite"), (1, "db.sqlite-wal"))
          if pa[idx] != pb[idx]]
    return "、".join(ch) + " 变化" if ch else ""


def _classify_batches(batches: list, samples: list, t0: float) -> list:
    """逐拍闸门判定:首拍无基线按开闸(与 T4 启动语义一致);其后按
    (stat 对, today0) 与上一拍比较 —— db 变化=开、today0 变化=时间闸
    强制开闸(例外2)、均未变=关。"""
    rows, prev = [], None
    for i, b in enumerate(batches):
        start = b[0][0]
        cur = _sample_at(samples, start)
        if i == 0:
            state, detail = "首拍", "无基线,按开闸"
        else:
            db_d = _diff_detail(prev[1], cur[1])
            if cur[2] != prev[2]:
                state, detail = "时间闸", "today0 变化→强制开闸(例外2)"
            elif db_d:
                state, detail = "开", db_d
            else:
                state, detail = "关", "db.sqlite/-wal 均未变"
        rows.append((i + 1, start - t0, len(b),
                     b[-1][0] - b[0][0], state, detail))
        prev = cur
    return rows


def section3(eng, idle_ticks: int) -> bool:
    print(f"\n== 第 3 节:空闲 tick SQL 计数与闸门命中(目标 {idle_ticks} 拍)==")
    print("   口径:只统计 _db_loop 线程经 connect_ro 发出的 ZCode DB SQL(闸门")
    print("   管辖段,含 _check_activity/_poll_new_completed);例外1/例外2 见文件")
    print("   头声明。闸门为探针侧独立模拟,与引擎侧 T4 闸门互为对照(T4 前:")
    print("   引擎每拍照发全量 SQL;T4 后:引擎闸门关时探针计数自然归零)。")
    events: list = []

    def cb(sql):
        events.append((time.perf_counter(), threading.get_ident(), sql))

    def counting_connect():
        con = _ORIG_CONNECT()
        con.set_trace_callback(cb)
        return con

    if eng.on_activity is None:
        # 生产形态对齐:on_activity 接线后 _check_activity 才发水位 SQL
        # (未接线时入口短路零 SQL,口径要求含它,计数会恒少一条)。
        eng.on_activity = lambda: None
    base_sample = (time.perf_counter(), stat_pair(), today0_ms())
    t_db = threading.Thread(target=eng._db_loop, daemon=True,
                            name="probe-dbloop")
    de.connect_ro = zsrc.connect_ro = counting_connect
    t_db.start()
    t0 = time.perf_counter()
    samples = [base_sample]
    # 墙钟预算:正常每拍 ≈ 拍内工作 + POLL_DB;T4 落地后闸门关 → 零 SQL →
    # 批次不再增长,只能按钟收口(再留 2×POLL_DB 给在飞拍跑完)。
    budget = idle_ticks * (eng.POLL_DB + 0.75) + 2 * eng.POLL_DB
    alive_at_break = True
    try:
        while True:
            time.sleep(WATCH_DT)
            now_t = time.perf_counter()
            samples.append((now_t, stat_pair(), today0_ms()))
            gov = [e for e in events if e[1] == t_db.ident]
            batches = _split_batches(gov)
            # 收口 A:目标拍数已观测且末批已闭合(距末事件 0.3s —— 高于拍内
            # 毫秒级语句间隔、低于拍间隔 ~0.8s+,抢在下一拍开跑前落 stop)
            if (len(batches) >= idle_ticks and gov
                    and now_t - gov[-1][0] > 0.3):
                break
            # 收口 B:墙钟预算尽
            if now_t - t0 >= budget:
                break
        alive_at_break = t_db.is_alive()
    finally:
        # 收口只置 stop_flag、刻意不置 _wake(不走 eng.stop()):T4 起
        # _db_loop 结构是『先查 stop → wait → tick』,wait 在飞时 stop 必然
        # 多跑一拍;若用 _wake.set() 立即打断,该过渡拍会紧跟窗口末拍(间隔
        # ~0.3s)被批次切分并进末批污染计数(实测末批 25 条 = 12+13 两拍)。
        # 只置 stop_flag 让过渡拍在自然拍点(≥1s 后)发生,落在 t_stop 之后,
        # 由下方窗口边界剔除。老引擎(查 stop 在 wait 后)无过渡拍,同兼容。
        t_stop = time.perf_counter()
        eng.stop_flag.set()
        t_db.join(timeout=15.0)
        de.connect_ro = zsrc.connect_ro = _ORIG_CONNECT

    wall = t_stop - t0
    all_db = [e for e in events if e[1] == t_db.ident]
    other = [e for e in events if e[1] != t_db.ident]
    # 窗口边界:t_stop 之前开始的批次才计入(关停过渡拍整批剔除;按批次
    # 而非按事件剔除 —— t_stop 恰落在窗口拍中段时,该拍的语句完整保留)
    dropped = [b for b in _split_batches(all_db) if b[0][0] > t_stop]
    gov = [ev for b in _split_batches(all_db) if b[0][0] <= t_stop for ev in b]
    batches = _split_batches(gov)
    rows = _classify_batches(batches, samples, t0)

    print(f"\n   逐拍明细(批次切分启发式:事件间隔 > {TICK_GAP_SPLIT}s 断批;"
          f"闸门归因用")
    print(f"   {WATCH_DT}s 采样近似 T4 的拍起点 stat,误差 <= {WATCH_DT}s):")
    for idx, rel, cnt, span, state, detail in rows:
        print(f"   拍 {idx:>3}  +{rel:6.1f}s  SQL {cnt:>3} 条  "
              f"拍内跨度 {span:5.2f}s  闸门:{state}({detail})")
    if not rows:
        print("   (零批次:窗口内 _db_loop 未发出任何管辖 SQL)")

    census: dict = {}
    for _t, _tid, sql in gov:
        sh = sql_shape(sql)
        c = census.setdefault(sh, [0, sql])
        c[0] += 1
    print("\n   管辖 SQL 形态普查(全窗口,按条数降序):")
    for sh, (cnt, sql) in sorted(census.items(), key=lambda kv: -kv[1][0]):
        print(f"     x{cnt:>4}  {annotate(sh) or '(未注记形状)'}")
        print(f"            例:{sql[:100]}")

    total = len(gov)
    closed_sql = sum(r[2] for r in rows if r[4] == "关")
    open_sql = total - closed_sql
    n_closed = sum(1 for r in rows if r[4] == "关")
    n_open = sum(1 for r in rows if r[4] == "开")
    n_first = sum(1 for r in rows if r[4] == "首拍")
    n_tg = sum(1 for r in rows if r[4] == "时间闸")
    mean = total / len(rows) if rows else 0.0
    print(f"\n   == 空闲窗口汇总 ==")
    print(f"   墙钟 {wall:.1f}s | 观测 {len(rows)} 批(目标 {idle_ticks} 拍;"
          f"_db_loop 线程窗口内{'存活' if alive_at_break else '提前退出!'})")
    if dropped:
        print(f"   (引擎关停过渡拍 {len(dropped)} 批 / {sum(len(b) for b in dropped)} 条 SQL "
              "落在窗口边界外,已剔除 —— T4 _db_loop 先查 stop 再 wait,"
              "在飞 wait 必多跑一拍)")
    print(f"   闸门模拟:关 {n_closed} · 开(db 变化){n_open} · 首拍 {n_first}"
          f" · 时间闸 {n_tg}")
    print(f"   管辖 SQL 总数: {total}(每拍均值 {mean:.1f} 条)")
    print(f"   其中『关』批 SQL: {closed_sql} 条  <- 改前=空闲期纯浪费;")
    print("        T4 落地后复跑此数应为 0(『空闲 0』验收口径,"
          "例外1/例外2 见头部声明)")
    print(f"   其中『开/首拍/时间闸』批 SQL: {open_sql} 条  <- 改前后均合法")
    if other:
        print(f"   非 _db_loop 线程 SQL: {len(other)} 条(本窗口不应有,备查):")
        for _t, tid, sql in other[:10]:
            print(f"     tid={tid}: {sql[:100]}")
    else:
        print("   非 _db_loop 线程 SQL: 0 条(例外1 刻意未运行 —— 见头部声明)")
    if not alive_at_break:
        print("   [!] _db_loop 线程在窗口结束前自行退出(非 stop_flag)——"
              "零 SQL 可能是轮询线程死亡而非闸门关闭,")
        print("       本次窗口测量无效(registry#16 家族:轮询线程死=整卡冻结)。")
        return False
    return True


# ------------------------------------------------------------------- main ---

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="perf_probe",
        description="T0 性能探针:三条热点 SQL 计时+EXPLAIN+空闲 tick SQL 计数"
                    "(只读真实库;口径与例外声明见输出头部)")
    ap.add_argument("--idle-ticks", type=int, default=IDLE_TICKS,
                    help=f"空闲窗口 _db_loop 拍数(默认 {IDLE_TICKS};0=跳过第 3 节)")
    ap.add_argument("--best-of", type=int, default=BENCH_N,
                    help=f"每条 SQL 的 best-of-N 计时(默认 {BENCH_N})")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    n = max(1, args.best_of)
    idle_ticks = max(0, args.idle_ticks)

    if not os.path.exists(DB_PATH):
        # 『打不开库』与『窗口内无数据』必须分开报告、分开退出码
        # (registry open#3 的教训:故障被吞成真空窗)。
        print(f"DB 不可用: {DB_PATH} —— 探针无对象,rc=1")
        return 1

    eng = de.DataEngine(queue.Queue(maxsize=1))   # 只构造不 start:不起
    # run() 主循环/其余轮询线程 —— 第 3 节只跑 _db_loop(管辖段本身),
    # 例外1(run() 每 0.5s 的 _latest_session)刻意不运行,靠头部声明豁免。
    con = _ORIG_CONNECT()
    timed: dict = {}
    try:
        print_header(eng, con)
        ok_pre = section1_pre(con, n, eng.THROUGHPUT_WINDOW_S)
        ok_live = section1_live(eng, con, n, timed)
        ok_extra = section1_extras(eng, con, n)
        section2(eng, con, n, timed)
        if idle_ticks > 0:
            ok3 = section3(eng, idle_ticks)
        else:
            print("\n(第 3 节跳过:--idle-ticks 0)")
            ok3 = True
    finally:
        con.close()
    ok_all = ok_pre and ok_live and ok_extra and ok3
    print("\nRESULT:", "OK" if ok_all else "PROBE FAILURES PRESENT")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
