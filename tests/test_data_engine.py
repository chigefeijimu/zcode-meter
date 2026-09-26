#!/usr/bin/env python3
"""数据层单测 —— 口径断言 / 会话切换 / subagent 排除。无 UI 依赖。

这些断言全部来自项目史上真实翻过的车:
- input_tokens 已含 cache_read(勿重复累加)
- 今日用量含全部来源(与 ZCode 自身口径一致)
- 活跃会话判定不得被子代理劫持
- 频繁切换会话时统计必须立即完整填充
"""
import queue
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_engine import DB_PATH, DataEngine  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def db() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def test_active_session_not_subagent():
    e = DataEngine(queue.Queue(maxsize=1))
    sid = e._latest_session()
    check("活跃会话非空", bool(sid), sid)
    check("活跃会话不是 subagent", not sid.startswith("sess_subagent"), sid)
    e.stop()


def test_today_usage_matches_full_scope():
    """今日用量 = 全来源(含 subagent) completed 的 in+out。"""
    e = DataEngine(queue.Queue(maxsize=1))
    e.start()
    time.sleep(2.5)
    e.stop()
    s = e.snap
    import datetime as dt
    today0 = int(dt.datetime.now().replace(hour=0, minute=0, second=0,
                                           microsecond=0).timestamp() * 1000)
    con = db()
    (expect,) = con.execute(
        "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
        " FROM model_usage WHERE status='completed' AND started_at>=?",
        (today0,)).fetchone()
    con.close()
    check("今日用量口径=全来源",
          s.today_tokens <= expect and expect - s.today_tokens < 10_000_000,
          f"got {s.today_tokens} expect {expect}")
    # 注:引擎采样与断言重查之间可能有新请求完成,故允许 got 落后少量而非严格相等


def test_session_stats_scoped_main_turn():
    """会话级出入 = 当前会话 + main_turn,不含 subagent/其他会话。"""
    e = DataEngine(queue.Queue(maxsize=1))
    e.start()
    time.sleep(2.5)
    e.stop()
    s = e.snap
    con = db()
    (i, c, o) = con.execute(
        "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read_input_tokens),0),"
        " COALESCE(SUM(output_tokens),0) FROM model_usage"
        " WHERE status='completed' AND session_id=? AND query_source='main_turn'",
        (e.session_id,)).fetchone()
    con.close()
    check("会话输入口径", s.session_in == i, f"got {s.session_in} expect {i}")
    check("会话缓存口径", s.session_cache == c, f"got {s.session_cache} expect {c}")
    check("缓存率=cache/in(input已含cache)",
          abs(s.cache_rate - (c / i * 100 if i else 0)) < 0.05,
          f"got {s.cache_rate:.2f}")


def test_rapid_session_switch_fills_immediately():
    """mock 强制切换:每次切换统计立即完整,无空值。"""
    e = DataEngine(queue.Queue(maxsize=1))
    e.start()
    con = db()
    sids = [r[0] for r in con.execute(
        "SELECT session_id, COUNT(*) n FROM model_usage WHERE status='completed'"
        " AND session_id NOT LIKE 'sess_subagent%' GROUP BY session_id"
        " ORDER BY n DESC LIMIT 3").fetchall()]
    con.close()
    check("有可切换的会话样本", len(sids) >= 2, str(len(sids)))
    for sid in sids:
        e._latest_session = lambda s=sid: s
        e._refresh_session()
        s = e.snap
        ok = (s.speed_by_model is not None and s.session_in is not None
              and s.title is not None)
        check(f"切换后完整填充 {sid[:18]}…", ok,
              f"in={s.session_in} models={s.speed_by_model}")
    e.stop()


def test_manual_pin_resumes_auto():
    """手动固定后引擎刷新不得劫持;清除后立即恢复自动跟随。"""
    e = DataEngine(queue.Queue(maxsize=1))     # 不 start:同步路径已完整,免线程竞态
    con = db()
    sids = [r[0] for r in con.execute(
        "SELECT session_id FROM model_usage WHERE status='completed'"
        " AND session_id NOT LIKE 'sess_subagent%'"
        " GROUP BY session_id ORDER BY MAX(started_at) DESC LIMIT 2").fetchall()]
    con.close()
    check("有可固定的会话样本", len(sids) >= 2, str(len(sids)))
    if len(sids) < 2:
        return
    sidA, sidB = sids
    e.set_manual_session(sidB)
    check("固定后 manual 标记生效", e.manual_session == sidB and e.snap.manual,
          f"{e.manual_session} snap.manual={e.snap.manual}")
    # mock 最新会话变成 sidA:固定期间引擎刷新必须无动于衷
    e._latest_session = lambda: sidA
    e._refresh_session()
    check("手动固定后不跟随切换", e.session_id == sidB, e.session_id)
    con = db()
    (i,) = con.execute(
        "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
        " FROM model_usage WHERE status='completed' AND session_id=?"
        " AND query_source='main_turn'", (sidB,)).fetchone()
    con.close()
    check("固定会话统计=该会话 main_turn 手算",
          e.snap.session_in + e.snap.session_out == i,
          f"got {e.snap.session_in + e.snap.session_out} expect {i}")
    e.clear_manual_session()
    e._refresh_session()
    check("恢复自动跟随", e.manual_session is None and not e.snap.manual,
          f"{e.manual_session}")
    check("恢复后对齐最新活跃会话", e.session_id == sidA, e.session_id)
    e.stop()


def test_recent_sessions_no_subagent():
    """右键会话菜单列表:非空且口径与跟随一致(排除 subagent)。"""
    e = DataEngine(queue.Queue(maxsize=1))
    rows = e.recent_sessions(8)
    check("最近会话列表非空", bool(rows), str(rows[:2]))
    check("最近会话无 subagent",
          all(not sid.startswith("sess_subagent") for sid, _ in rows))
    check("最近会话数不超过 limit", len(rows) <= 8, str(len(rows)))
    e.stop()


def test_daily_usage_matches_today_scope():
    """按天图表的当天桶与今日用量同口径(全来源 in+out、本地午夜天界)。"""
    e = DataEngine(queue.Queue(maxsize=1))
    e.start()
    time.sleep(2.5)
    e.stop()
    rows = e.fetch_daily_usage(1)
    check("按天查询含当天桶", len(rows) >= 1, str(rows))
    if not rows:
        return
    got = rows[-1][1]     # ORDER BY d:最后一天即今天
    import datetime as dt
    today0 = int(dt.datetime.now().replace(hour=0, minute=0, second=0,
                                           microsecond=0).timestamp() * 1000)
    con = db()
    (expect,) = con.execute(
        "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
        " FROM model_usage WHERE status='completed' AND started_at>=?",
        (today0,)).fetchone()
    con.close()
    # 引擎采样与断言重查之间可能有新请求完成,允许 got 落后少量而非严格相等
    check("按天当天桶=今日用量口径",
          got <= expect and expect - got < 10_000_000,
          f"got {got} expect {expect}")


def test_session_usage_scope():
    """按会话图表口径:main_turn、无 subagent 会话,首行与手算逐值相等。"""
    e = DataEngine(queue.Queue(maxsize=1))
    rows = e.fetch_session_usage(20)
    check("按会话查询非空", bool(rows))
    check("按会话无 subagent",
          all(not r[0].startswith("sess_subagent") for r in rows))
    if rows:
        sid, title, tok, cnt = rows[0]
        con = db()
        (etok, ecnt) = con.execute(
            "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0),"
            " COUNT(*) FROM model_usage"
            " WHERE status='completed' AND query_source='main_turn' AND session_id=?",
            (sid,)).fetchone()
        con.close()
        check("首行 tokens=main_turn 手算", tok == etok,
              f"got {tok} expect {etok}")
        check("首行请求数=同口径 COUNT", cnt == ecnt,
              f"got {cnt} expect {ecnt}")
    e.stop()


if __name__ == "__main__":
    print("== test_active_session_not_subagent =="); test_active_session_not_subagent()
    print("== test_today_usage_matches_full_scope ==");  test_today_usage_matches_full_scope()
    print("== test_session_stats_scoped_main_turn ==");  test_session_stats_scoped_main_turn()
    print("== test_rapid_session_switch ==");            test_rapid_session_switch_fills_immediately()
    print("== test_manual_pin_resumes_auto ==");         test_manual_pin_resumes_auto()
    print("== test_recent_sessions_no_subagent ==");     test_recent_sessions_no_subagent()
    print("== test_daily_usage_matches_today_scope =="); test_daily_usage_matches_today_scope()
    print("== test_session_usage_scope ==");             test_session_usage_scope()
    if FAILED:
        print(f"\nFAILED: {FAILED}")
        sys.exit(1)
    print("\nDATA ENGINE TESTS ALL PASS")
