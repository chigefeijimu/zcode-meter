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


if __name__ == "__main__":
    print("== test_active_session_not_subagent =="); test_active_session_not_subagent()
    print("== test_today_usage_matches_full_scope ==");  test_today_usage_matches_full_scope()
    print("== test_session_stats_scoped_main_turn ==");  test_session_stats_scoped_main_turn()
    print("== test_rapid_session_switch ==");            test_rapid_session_switch_fills_immediately()
    if FAILED:
        print(f"\nFAILED: {FAILED}")
        sys.exit(1)
    print("\nDATA ENGINE TESTS ALL PASS")
