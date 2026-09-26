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


# ================= v0.4.0 新增:金额/燃速/计费块/多源/quota/告警 =================

def test_cost_three_paths():
    """金额三路径(纯函数,合成价格表):已知全档 / 已知缺 in_cache 按 in
    全价 / 未知模型 ¥0+partial。cache_read 实测占 input ~98%,三条规则
    混淆会静默错价一个数量级 —— 本测是唯一防线,数值全部手算。"""
    from data_engine import cost_of
    prices = {
        "M-A": {"in": 8.0, "in_cache": 2.0, "out": 28.0},
        "M-B": {"in": 4.0, "out": 16.0},            # 已知但缺 in_cache 档
    }
    # 路径1:完整三档。miss=1M、cache=1M、out=0.5M → 8 + 2 + 14 = ¥24
    c, p = cost_of(prices, "M-A", 2_000_000, 500_000, 1_000_000)
    check("金额:完整三档", not p and abs(c - 24.0) < 1e-9, f"{c} partial={p}")
    # 路径2:缺 in_cache → cache_read 按 in 全价:全部 in(含cache)×4 = ¥8
    c, p = cost_of(prices, "M-B", 2_000_000, 0, 1_000_000)
    check("金额:缺 in_cache 按 in 全价", not p and abs(c - 8.0) < 1e-9,
          f"{c} partial={p}")
    # 路径3:未知模型 → ¥0 + partial(UI 显示 ≈)
    c, p = cost_of(prices, "M-C", 2_000_000, 500_000, 1_000_000)
    check("金额:未知模型 ¥0+partial", p and c == 0.0, f"{c} partial={p}")


def test_today_cost_matches_grouped_sql():
    """卡片今日金额 = 今日分组 SQL × DEFAULT_PRICES 手算对账(公式在测试里
    重写,不调 cost_of,防实现自证);partial 与是否存在未知模型一致。"""
    import data_engine as de
    e = DataEngine(queue.Queue(maxsize=1))
    # 钉死默认表:开发机若有 zm_prices.json 覆盖会导致对账漂移
    e.prices = {m: dict(t) for m, t in de.DEFAULT_PRICES.items()}
    e.start()
    time.sleep(2.5)
    e.stop()
    con = db()
    rows = con.execute(
        "SELECT model_id, SUM(input_tokens), SUM(cache_read_input_tokens),"
        " SUM(output_tokens) FROM model_usage"
        " WHERE status='completed' AND started_at>=?"
        " GROUP BY model_id",
        (de.today0_ms(),)).fetchall()
    con.close()
    manual, partial = 0.0, False
    for model, i_, c_, o_ in rows:
        p = de.DEFAULT_PRICES.get(model)
        if not p:
            partial = True
            continue
        miss = max((i_ or 0) - (c_ or 0), 0)
        manual += (miss * p["in"] + (c_ or 0) * p["in_cache"]
                   + (o_ or 0) * p["out"]) / 1_000_000
    s = e.snap
    # 采样与手算之间可能有新请求完成 → 允许 got 落后少量;引擎金额保留 4 位
    # 小数(舍入最多 +5e-5),下界放 1e-3
    check("今日金额=分组手算",
          s.today_cost_cny <= manual + 1e-3 and manual - s.today_cost_cny < 5.0,
          f"got {s.today_cost_cny} manual {manual}")
    check("partial 与未知模型一致", s.today_cost_partial == partial,
          f"got {s.today_cost_partial} manual {partial}")


def test_burn_window_and_est_hours():
    """燃速 = trailing-60min 窗口手算;est_hours_left 纯函数口径
    (燃速 0 → None、未配预算 → None,除零是历史雷区)。"""
    from data_engine import est_hours_left
    check("est:常规公式", abs(est_hours_left(20.0, 5.0, 2.5) - 6.0) < 1e-9,
          f"{est_hours_left(20.0, 5.0, 2.5)}")
    check("est:燃速 0 → None", est_hours_left(20.0, 5.0, 0.0) is None)
    check("est:未配预算 → None", est_hours_left(None, 5.0, 2.5) is None)
    e = DataEngine(queue.Queue(maxsize=1))
    e.start()
    time.sleep(2.5)
    e.stop()
    con = db()
    (t,) = con.execute(
        "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
        " FROM model_usage WHERE status='completed' AND started_at>=?",
        (int(time.time() * 1000) - 3_600_000,)).fetchone()
    con.close()
    got = e.snap.burn_tokens_per_hour
    # 窗口随采样时刻整体右移,边界行进出允许 2% 抖动
    check("燃速=trailing-60min 手算",
          abs(got - t) <= max(t * 0.02, 100_000), f"got {got} manual {t}")


def test_billing_blocks_buckets():
    """计费块:回退锚点(无 quota)下块界等分 5h、桶内总和=区间 completed
    总量(query_source 全部)、当前块唯一且含 now;quota 锚点形态对齐
    nextResetTime-k*5h。"""
    e = DataEngine(queue.Queue(maxsize=1))
    blocks = e.fetch_billing_blocks(29)
    check("计费块非空", bool(blocks))
    if not blocks:
        return
    check("计费块数=29", len(blocks) == 29, str(len(blocks)))
    check("块界等分 5h",
          all(blocks[i + 1][0] - blocks[i][0] == 18_000_000
              for i in range(len(blocks) - 1)))
    cur = [b for b in blocks if b[2]]
    now_ms = int(time.time() * 1000)
    check("当前块唯一", len(cur) == 1, str(len(cur)))
    check("当前块含 now", bool(cur) and cur[0][0] <= now_ms < cur[0][0] + 18_000_000)
    con = db()
    (total,) = con.execute(
        "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
        " FROM model_usage WHERE status='completed' AND started_at>=?",
        (blocks[0][0],)).fetchone()
    con.close()
    check("桶内总和=区间 completed 总量",
          sum(b[1] for b in blocks) == total,
          f"got {sum(b[1] for b in blocks)} expect {total}")
    # quota 锚点:nextResetTime 在未来 → 当前块=最后一块,块界从锚点回退 5h
    e.quota_hint = now_ms + 2 * 3600 * 1000
    b2 = e.fetch_billing_blocks(29)
    check("quota 锚:当前块=最后一块", bool(b2) and b2[-1][2])
    check("quota 锚:块界对齐锚点",
          bool(b2) and (e.quota_hint - (b2[-1][0] + 18_000_000)) == 0
          and all(b2[i + 1][0] - b2[i][0] == 18_000_000 for i in range(len(b2) - 1)))


def test_claude_source_synthetic():
    """ClaudeSource 合成 jsonl(不依赖真实 ~/.claude):in=input+cache_read+
    cache_creation、message.id 去重不清零(error 行先跳过)、isSidechain 计入、
    UTC→local 天界、目录缺失优雅降级。"""
    import datetime as dtmod
    import json as jsonmod
    import os as osmod
    import shutil
    import tempfile
    from data_engine import ClaudeSource

    root = tempfile.mkdtemp(prefix="zm_claude_")
    try:
        proj = osmod.path.join(root, "p1", "sub")
        osmod.makedirs(proj)
        t0 = dtmod.datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0)
        t_today = t0 + dtmod.timedelta(minutes=30)       # 本地刚过午夜
        t_yday = t0 - dtmod.timedelta(milliseconds=1)    # 本地昨天最后一毫秒

        def iso(local_dt):
            return (local_dt.astimezone(dtmod.timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")

        def line(ts, mid, i, cr, cc, o, err=False, side=False, typ="assistant"):
            obj = {"type": typ, "timestamp": iso(ts),
                   "message": {"id": mid, "usage": {
                       "input_tokens": i, "cache_read_input_tokens": cr,
                       "cache_creation_input_tokens": cc, "output_tokens": o}}}
            if err:
                obj["isApiErrorMessage"] = True
            if side:
                obj["isSidechain"] = True
            return jsonmod.dumps(obj, ensure_ascii=False)

        rows = [
            # msg_1 正常:in=1000+200+50 out=300
            line(t_today, "msg_1", 1000, 200, 50, 300),
            # 同 message.id 重复(流式多行)→ 只计一次
            line(t_today, "msg_1", 1000, 200, 50, 300),
            # 后到的 error 零用量行(id 同 msg_1)→ 跳过,keep-last 会清零真实用量
            line(t_today, "msg_1", 0, 0, 0, 0, err=True),
            # 零用量无标记行 → 同样跳过
            line(t_today, "msg_zero", 0, 0, 0, 0),
            # isSidechain 计入(与 ZCode 含 subagent 对齐):in=10 out=5
            line(t_today, "msg_2", 10, 0, 0, 5, side=True),
            # user 行忽略
            line(t_today, "u1", 0, 0, 0, 0, typ="user"),
            # 本地昨天 23:59:59.999(UTC 表示可能是前一天)→ 不进今日
            line(t_yday, "msg_4", 700, 0, 0, 90),
            # 坏 JSON 行忽略
            "{not json",
        ]
        with open(osmod.path.join(proj, "s.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        src = ClaudeSource(projects_dir=root)
        check("Claude 源可用", src.is_available())
        expect_today = (1000 + 200 + 50 + 300) + (10 + 5)
        got = src.today_usage()
        check("今日聚合含 cache 补齐", got == expect_today,
              f"got {got} expect {expect_today}")
        daily = dict(src.daily_usage(2))
        today_iso = dtmod.date.today().isoformat()
        yday_iso = (dtmod.date.today() - dtmod.timedelta(days=1)).isoformat()
        check("按天含今天", daily.get(today_iso) == expect_today,
              f"{daily.get(today_iso)}")
        # UTC→local:本地午夜前的最后一毫秒必须归昨天(直接取 UTC 日期会错)
        check("UTC→local 天界归昨天", daily.get(yday_iso) == 790,
              f"{daily.get(yday_iso)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    src2 = ClaudeSource(projects_dir=osmod.path.join(root, "none"))
    check("目录缺失→不可用且不炸", not src2.is_available()
          and src2.today_usage() == 0 and src2.daily_usage(3) == [])


def test_parse_quota_payload():
    """quota 载荷解析(社区逆向结构,文档样例):percentage=已用%、
    remaining=100-percentage、number 兼容字符串/数字、选 number==5 的 5h 窗、
    nextResetTime 透传;坏载荷一律 None。"""
    from data_engine import parse_quota_payload
    sample = {"code": 200, "data": {"limits": [
        {"type": "TOKENS_LIMIT", "number": "5", "percentage": "42.5",
         "nextResetTime": 1760000000000},
        {"type": "TOKENS_LIMIT", "number": 30, "percentage": 10.0},
        {"type": "CREDIT_LIMIT", "number": 5, "percentage": 33.0},
    ]}}
    r = parse_quota_payload(sample)
    check("选中 5h 窗(字符串形态)", r is not None and r.get("window_hours") == 5)
    check("remaining=100-percentage",
          r is not None and abs(r["remaining_pct"] - 57.5) < 1e-9,
          f"{r and r.get('remaining_pct')}")
    check("nextResetTime 透传",
          r is not None and r.get("next_reset_ms") == 1760000000000)
    r2 = parse_quota_payload({"data": {"limits": [
        {"type": "TOKENS_LIMIT", "number": 5, "percentage": 80}]}})
    check("数字形态 5h 窗", r2 is not None and abs(r2["remaining_pct"] - 20.0) < 1e-9)
    check("坏载荷→None",
          parse_quota_payload({}) is None
          and parse_quota_payload({"data": {"limits": []}}) is None
          and parse_quota_payload({"data": {"limits": [
              {"type": "CREDIT_LIMIT", "number": 5, "percentage": 1}]}}) is None
          and parse_quota_payload(None) is None)


def test_budget_alerts_dedup_and_reset():
    """BudgetAlerts:同级别同日只提醒一次、跨日重置、双轨隔离、持久化后
    重启仍去重。持久化写盘需绕过守卫(run_all 注入 ZM_NO_STATE=1 时
    _save 会被守卫拦下,测试内显式 patch 为 False 验证真实写读路径)。"""
    import shutil
    import tempfile
    import data_engine as de
    from pathlib import Path as _Path
    tmp = _Path(tempfile.mkdtemp(prefix="zm_alerts_"))
    a = de.BudgetAlerts([20, 10], state_path=str(tmp / "a.json"))
    check("首触 20% 提醒", a.evaluate("budget", 15.0, "2026-01-01") is not None)
    check("同级别同日不重复", a.evaluate("budget", 15.0, "2026-01-01") is None)
    check("跌破 10% 再提醒", a.evaluate("budget", 8.0, "2026-01-01") is not None)
    check("10% 同日不重复", a.evaluate("budget", 7.0, "2026-01-01") is None)
    check("跨日重置", a.evaluate("budget", 15.0, "2026-01-02") is not None)
    b = de.BudgetAlerts([20, 10], state_path=str(tmp / "b.json"))
    check("双轨隔离", b.evaluate("quota", 15.0, "2026-01-01") is not None
          and b.evaluate("quota", 15.0, "2026-01-01") is None)
    # 持久化:patch 掉守卫后走真实写盘,重开实例同日仍去重(前面的评估都在
    # 守卫环境下没落盘,这里独立地先写再读,两种运行方式结果一致)
    orig = de._no_persist
    de._no_persist = lambda: False
    try:
        p = str(tmp / "c.json")
        c0 = de.BudgetAlerts([20, 10], state_path=p)
        c0.evaluate("budget", 15.0, "2026-01-03")     # 触发写盘
        c1 = de.BudgetAlerts([20, 10], state_path=p)  # 重开,状态自盘恢复
        check("重启后同日仍去重(状态自盘恢复)",
              c1.evaluate("budget", 15.0, "2026-01-03") is None)
    finally:
        de._no_persist = orig
    shutil.rmtree(tmp, ignore_errors=True)


def test_today_by_source():
    """多源聚合:ZCode 源=今日用量同口径(今日用量本体保持 ZCode-DB-only,
    多源只能落在新字段 today_by_source —— 历史回归重灾区)。"""
    e = DataEngine(queue.Queue(maxsize=1))
    e.start()
    time.sleep(2.5)
    e.stop()
    srcs = dict(e.snap.today_by_source or [])
    check("ZCode 源在列", "ZCode" in srcs, str(srcs))
    if "ZCode" in srcs:
        # 两个数来自同一轮 _poll_stats 内先后两次同口径查询,采样瞬间的活跃
        # 请求会造成小幅双向差(实测生成中 ~0.2%),对账看量级不看严格相等
        diff = abs(srcs["ZCode"] - e.snap.today_tokens)
        check("ZCode 源=今日用量口径",
              diff <= max(e.snap.today_tokens * 0.02, 100_000),
              f"src {srcs['ZCode']} today {e.snap.today_tokens}")


# ================= v0.5.0 新增:save_config(设置窗落盘路径) =================

def test_save_config_roundtrip_and_normalize():
    """save_config:显式临时 path 写读回环;规范化与 load_config 同规则
    (key strip / 非法 budget→None / pct 正数降序去重、非法项剔除、
    整字段坏形回退默认)—— 写读必须得到同一结果,否则设置窗『保存后
    下次打开变样』。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    import data_engine as de
    tmp = _Path(tempfile.mkdtemp(prefix="zm_cfg_"))
    orig_path = de.CONFIG_PATH
    try:
        p = str(tmp / "c1.json")
        cfg = {"quota_api_key": "  sk-test-123  ", "daily_budget_cny": 25,
               "alert_pct": [10, 30, 10, "x", -5]}
        check("save:显式 path 返回 True", de.save_config(cfg, path=p) is True)
        de.CONFIG_PATH = p                   # load_config 只认模块级路径
        back = de.load_config()
        check("回环:key 已 strip", back["quota_api_key"] == "sk-test-123",
              back["quota_api_key"])
        check("回环:budget 透传", back["daily_budget_cny"] == 25.0,
              str(back["daily_budget_cny"]))
        check("回环:pct 正数降序去重、非法项剔除",
              back["alert_pct"] == [30.0, 10.0], str(back["alert_pct"]))
        # 非法字段整体规范化为默认(load_config 坏值回退的同一口径)
        p2 = str(tmp / "c2.json")
        check("save:坏形 cfg 仍可落盘(规范化后写)",
              de.save_config({"quota_api_key": 123, "daily_budget_cny": -3,
                              "alert_pct": "nope"}, path=p2) is True)
        de.CONFIG_PATH = p2
        back2 = de.load_config()
        check("坏形规范化为默认",
              back2 == {"quota_api_key": "", "daily_budget_cny": None,
                        "alert_pct": [20.0, 10.0]}, str(back2))
    finally:
        de.CONFIG_PATH = orig_path
        shutil.rmtree(tmp, ignore_errors=True)


def test_save_config_guard_and_failures():
    """save_config 失败语义三分支:
    ①守卫命中(默认路径 + _no_persist)→ False 且零文件(返回 True 会让
      设置窗热生效+关窗而 key 从未落盘,重启即无声丢失);
    ②temp 创建失败(目录不存在)→ False、无 .tmp 残留;
    ③replace 失败(Windows 打开中目标抛 PermissionError⊂OSError)→
      False、.tmp 已清理、原文件内容不变。"""
    import os as osmod
    import shutil
    import tempfile
    from pathlib import Path as _Path
    import data_engine as de
    tmp = _Path(tempfile.mkdtemp(prefix="zm_cfg_"))
    orig_path, orig_np = de.CONFIG_PATH, de._no_persist
    cfg = {"quota_api_key": "sk-x", "daily_budget_cny": 5, "alert_pct": [20, 10]}
    try:
        # ①守卫:默认路径 + _no_persist=True → False 且不产生任何文件
        guard = tmp / "guard.json"
        de.CONFIG_PATH = str(guard)
        de._no_persist = lambda: True
        check("守卫命中:默认路径返回 False", de.save_config(cfg) is False)
        check("守卫命中:不产生任何文件",
              not guard.exists() and not (tmp / "guard.json.tmp").exists())
        # ②temp 创建失败:目标目录不存在(FileNotFoundError⊂OSError)
        ghost = str(tmp / "no_dir" / "x.json")
        check("temp 创建失败:返回 False", de.save_config(cfg, path=ghost) is False)
        check("temp 创建失败:无 .tmp 残留", not osmod.path.exists(ghost + ".tmp"))
        # ③replace 失败:两条 OSError 路径里更隐蔽的一条(临时文件已写成,
        # 清理逻辑必须执行,否则留下 zm_config.json.tmp 残骸)
        if osmod.name == "nt":
            held = tmp / "held.json"
            held.write_text('{"orig": 1}', encoding="utf-8")
            f = open(held, "r", encoding="utf-8")     # 占住目标句柄
            try:
                check("replace 失败(nt 打开句柄):返回 False",
                      de.save_config(cfg, path=str(held)) is False)
            finally:
                f.close()
            check("replace 失败:原文件内容不变",
                  held.read_text(encoding="utf-8") == '{"orig": 1}')
            check("replace 失败:.tmp 已清理",
                  not osmod.path.exists(str(held) + ".tmp"))
        else:
            # POSIX 无法用句柄锁 replace,用目录占位强制走同一条 OSError 路径
            (tmp / "as_dir").mkdir()
            target = str(tmp / "as_dir")
            check("replace 失败(posix 目录占位):返回 False",
                  de.save_config(cfg, path=target) is False)
            check("replace 失败:.tmp 已清理",
                  not osmod.path.exists(target + ".tmp"))
        # 守卫不拦显式 path(单测绕过守卫的既定通道)
        de._no_persist = orig_np
        ok = tmp / "ok.json"
        check("守卫外显式 path 可写",
              de.save_config(cfg, path=str(ok)) is True and ok.exists())
    finally:
        de.CONFIG_PATH, de._no_persist = orig_path, orig_np
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print("== test_active_session_not_subagent =="); test_active_session_not_subagent()
    print("== test_today_usage_matches_full_scope ==");  test_today_usage_matches_full_scope()
    print("== test_session_stats_scoped_main_turn ==");  test_session_stats_scoped_main_turn()
    print("== test_rapid_session_switch ==");            test_rapid_session_switch_fills_immediately()
    print("== test_manual_pin_resumes_auto ==");         test_manual_pin_resumes_auto()
    print("== test_recent_sessions_no_subagent ==");     test_recent_sessions_no_subagent()
    print("== test_daily_usage_matches_today_scope =="); test_daily_usage_matches_today_scope()
    print("== test_session_usage_scope ==");             test_session_usage_scope()
    # ---- v0.4.0 新增(全部无网络、不依赖真实 ~/.claude) ----
    print("== test_cost_three_paths ==");                test_cost_three_paths()
    print("== test_today_cost_matches_grouped_sql ==");  test_today_cost_matches_grouped_sql()
    print("== test_burn_window_and_est_hours ==");       test_burn_window_and_est_hours()
    print("== test_billing_blocks_buckets ==");          test_billing_blocks_buckets()
    print("== test_claude_source_synthetic ==");         test_claude_source_synthetic()
    print("== test_parse_quota_payload ==");             test_parse_quota_payload()
    print("== test_budget_alerts_dedup_and_reset ==");   test_budget_alerts_dedup_and_reset()
    print("== test_today_by_source ==");                 test_today_by_source()
    # ---- v0.5.0 新增(save_config,全部临时目录,不碰真实 zm_config.json) ----
    print("== test_save_config_roundtrip_and_normalize =="); test_save_config_roundtrip_and_normalize()
    print("== test_save_config_guard_and_failures ==");      test_save_config_guard_and_failures()
    if FAILED:
        print(f"\nFAILED: {FAILED}")
        sys.exit(1)
    print("\nDATA ENGINE TESTS ALL PASS")
