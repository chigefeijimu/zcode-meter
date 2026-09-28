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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from zcode_meter.data_engine import DB_PATH, DataEngine  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def db() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def test_fetch_total_usage():
    """全部历史总计:合成 temp 库对账 —— tokens=全库 completed in+out 之和
    (cancelled 不计,全 query_source),金额按模型分组刊例价,未知模型
    置 partial。硬编码手算,不调 cost_of 防自证。"""
    import shutil, tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc
    tmp = _Path(tempfile.mkdtemp(prefix="zm_tot_"))
    orig = zsrc.DB_PATH
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        con.execute(
            "CREATE TABLE model_usage (started_at INTEGER, model_id TEXT,"
            " status TEXT, query_source TEXT, input_tokens INTEGER,"
            " cache_read_input_tokens INTEGER, output_tokens INTEGER)")
        t0 = de.today0_ms()
        con.executemany(
            "INSERT INTO model_usage VALUES (?,?,?,?,?,?,?)",
            [
                # GLM-5.3: in 2M(含 cache 1M) out 0.5M →
                # (1M*8 + 1M*2 + 0.5M*28)/1M = ¥24.0, tokens 2.5M
                (t0, "GLM-5.3", "completed", "main_turn",
                 2_000_000, 1_000_000, 500_000),
                # GLM-5.3-Flash: in 1M(含 cache 0.8M) out 0.5M →
                # (0.2M*0.8 + 0.8M*0.23 + 0.5M*2.8)/1M = ¥1.744, tokens 1.5M
                (t0, "GLM-5.3-Flash", "completed", "subagent",
                 1_000_000, 800_000, 500_000),
                # 未知模型:¥0 + partial, tokens 0.15M
                (t0, "mystery", "completed", "main_turn",
                 100_000, 0, 50_000),
                # cancelled 不计
                (t0, "GLM-5.3", "cancelled", "main_turn",
                 9_000_000, 0, 9_000_000),
            ])
        con.commit(); con.close()
        zsrc.DB_PATH = tdb; de.DB_PATH = tdb
        e = de.DataEngine(queue.Queue(maxsize=1))
        tok, cny, partial = e.fetch_total_usage()
        # 手算:tokens = 2.5M + 1.5M + 0.15M = 4.15M
        # 金额 = 24.0 + 1.744 + 0 = ¥25.744 → round(…,2)=25.74
        check("总计 tokens=全库 in+out", tok == 4_150_000, str(tok))
        check("总计金额=分组计价之和", cny == 25.74, str(cny))
        check("总计 partial(含未知模型)", partial is True)
        e.stop()
    finally:
        zsrc.DB_PATH = orig; de.DB_PATH = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_active_session_not_subagent():
    e = DataEngine(queue.Queue(maxsize=1))
    sid = e._latest_session()
    check("活跃会话非空", bool(sid), sid)
    check("活跃会话不是 subagent", not sid.startswith("sess_subagent"), sid)
    # v0.7.x:工作流会话(sess_dwf-dwfrun-*)同样不得劫持 —— 实测其 part 最新行
    # 在工作流运行期间持续抢占,而其 query_source=workflow_child 在会话统计
    # (main_turn)下一行不中,导致出入/缓存/均速全掉零
    check("活跃会话不是 dwf 工作流会话", not sid.startswith("sess_dwf-"), sid)
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
    from zcode_meter.data_engine import cost_of
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
    from zcode_meter import data_engine as de
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
    from zcode_meter.data_engine import est_hours_left
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
    from zcode_meter.data_engine import ClaudeSource

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
    from zcode_meter.data_engine import parse_quota_payload
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
    from zcode_meter import data_engine as de
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
    from zcode_meter import data_engine as de
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
    from zcode_meter import data_engine as de
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


# ================= v0.5.1 新增:quota 事件驱动节流 / 本地倒计时 / 活动信号 =================

def test_quota_fetch_decision():
    """节流器决策纯函数四场景 + 整数边界(先紧后松闸门链):
    立即查(force)/ 最短间隔 60s 硬闸 / 活跃期常态 3min / 静默期暂停。"""
    from zcode_meter.data_engine import quota_fetch_decision
    now = 1_000_000.0
    # 立即查:force 放行 —— 即使 <60s 且完全静默(启动首查/换 key 重启)
    check("决策:force 放行(压过间隔与静默)",
          quota_fetch_decision(now, now - 10.0, None, force=True)
          and quota_fetch_decision(now, None, None, force=True))
    # 兜底:last_fetch 未知 → 放行
    check("决策:last_fetch=None 放行",
          quota_fetch_decision(now, None, now - 30.0))
    # 最短间隔硬闸:59.9s 拒;60s 整点过硬闸但仍 <3min 常态闸 → 拒
    check("决策:59.9s 拒(硬闸)",
          not quota_fetch_decision(now, now - 59.9, now - 30.0))
    check("决策:60s 整点仍拒(硬闸过、<3min 闸拦)",
          not quota_fetch_decision(now, now - 60.0, now - 30.0))
    # 活跃期(过去 1h 内有活动):179.9 拒、180 整点放行、远超 3min 放行
    check("决策:活跃 179.9s 拒",
          not quota_fetch_decision(now, now - 179.9, now - 30.0))
    check("决策:活跃 180s 整点放行",
          quota_fetch_decision(now, now - 180.0, now - 30.0))
    check("决策:活跃 600s 放行",
          quota_fetch_decision(now, now - 600.0, now - 30.0))
    # 静默期(>1h 无活动)暂停:恰好 3600s 仍算活跃(边界归活跃),>3600 拒
    check("决策:活动距今 3600s 整仍算活跃",
          quota_fetch_decision(now, now - 600.0, now - 3600.0))
    check("决策:静默 3600.1s 拒",
          not quota_fetch_decision(now, now - 600.0, now - 3600.1))
    check("决策:last_activity=None 非 force 拒",
          not quota_fetch_decision(now, now - 600.0, None))
    # 静默期下 force 仍放行:『立即查』是最强信号
    check("决策:静默期 force 仍放行",
          quota_fetch_decision(now, now - 600.0, now - 7200.0, force=True))


def test_quota_countdown_and_age():
    """倒计时本地化与新鲜度文案(纯函数,零 API 请求):分钟向上取整、
    过期/缺参 → None;<60s 刚刚 / <60min N分钟前 / N小时前 / None→None。"""
    from zcode_meter.data_engine import format_age_zh, format_countdown_hm
    now_ms = 1_000_000_000_000
    # 倒计时:82.5min → ceil 83min = 1h 23m;41.5min → ceil 42min
    check("倒计时:1h 23m",
          format_countdown_hm(now_ms + 82.5 * 60 * 1000, now_ms) == "1h 23m",
          str(format_countdown_hm(now_ms + 82.5 * 60 * 1000, now_ms)))
    check("倒计时:42m",
          format_countdown_hm(now_ms + 41.5 * 60 * 1000, now_ms) == "42m",
          str(format_countdown_hm(now_ms + 41.5 * 60 * 1000, now_ms)))
    check("倒计时:剩 30s 进位 1m(不闪 0m)",
          format_countdown_hm(now_ms + 30 * 1000, now_ms) == "1m")
    check("倒计时:恰 60min 边界",
          format_countdown_hm(now_ms + 60 * 60 * 1000, now_ms) == "1h 0m")
    check("倒计时:已重置(过期)→ None",
          format_countdown_hm(now_ms - 1000, now_ms) is None
          and format_countdown_hm(now_ms, now_ms) is None)
    check("倒计时:缺参/0/负 → None",
          format_countdown_hm(None, now_ms) is None
          and format_countdown_hm(0, now_ms) is None
          and format_countdown_hm(-5, now_ms) is None)
    check("倒计时:now_ms 缺省走本地时钟不炸",
          format_countdown_hm(time.time() * 1000 + 3600 * 1000) == "1h 0m",
          str(format_countdown_hm(time.time() * 1000 + 3600 * 1000)))
    # 新鲜度(ts/now 单位为秒,与 next_reset 的毫秒不同)
    now = 2_000_000.0
    check("新鲜度:刚刚(<60s)", format_age_zh(now - 30.0, now) == "刚刚")
    check("新鲜度:3分钟前", format_age_zh(now - 180.0, now) == "3分钟前")
    check("新鲜度:59分钟前边界", format_age_zh(now - 3599.0, now) == "59分钟前")
    check("新鲜度:1小时前(3600 整)", format_age_zh(now - 3600.0, now) == "1小时前")
    check("新鲜度:2小时前", format_age_zh(now - 7200.0, now) == "2小时前")
    check("新鲜度:None/非法 → None",
          format_age_zh(None, now) is None and format_age_zh(0, now) is None)


def test_engine_activity_signal():
    """活动信号:completed 水位前进恰触发一次回调,不前进不触发;未接线
    (on_activity=None)短路不查水位、不炸;坏回调不拖死调用方。引擎不
    start、不发网络;回拨 _act_rowid 只动内存,不写用户 DB。"""
    e = DataEngine(queue.Queue(maxsize=1))
    calls = []
    e.on_activity = lambda: calls.append(1)
    e._act_rowid = 0                     # 回拨水位:下一查必视为前进
    e._check_activity()
    check("活动信号:水位前进回调恰一次", len(calls) == 1, str(len(calls)))
    e._check_activity()
    check("活动信号:水位不动不再触发", len(calls) == 1, str(len(calls)))
    # 未接线:_check_activity 短路,连水位查询都不做(计数探针验证)
    e.on_activity = None
    nq = []
    orig_max = e._max_completed_rowid
    e._max_completed_rowid = lambda: (nq.append(1), orig_max())[1]
    e._check_activity()
    check("活动信号:未接线短路且不查询", not nq, str(len(nq)))
    check("活动信号:未接线不炸", True)
    # 坏回调:外层 Exception 兜住,绝不拖死 _db_loop
    def boom():
        raise RuntimeError("bad callback")
    e2 = DataEngine(queue.Queue(maxsize=1))
    e2.on_activity = boom
    e2._act_rowid = 0
    try:
        e2._check_activity()
        ok = True
    except Exception:
        ok = False
    check("活动信号:坏回调被兜住不外抛", ok)
    check("活动信号:坏回调后水位已推进(不重复触发)", e2._act_rowid > 0)


def test_quota_monitor_throttle_bookkeeping():
    """QuotaMonitor 无网络小测(不 start 线程):notify_activity 写活动
    时间戳(显式 ts 可注入);_fetch_and_record 成功附 fetched_at 入
    _latest,失败也推进 _last_fetch_ts —— 失败占频率预算,防 1s tick 对
    故障端点加密重试。"""
    from zcode_meter.data_engine import QuotaMonitor
    m = QuotaMonitor("sk-test")          # 仅构造,绝不 start
    check("monitor:初始无活动/无抓取记录",
          m._last_activity_ts is None and m._last_fetch_ts is None)
    m.notify_activity()
    check("monitor:notify_activity 写时间戳", m._last_activity_ts is not None)
    m.notify_activity(ts=12345.0)
    check("monitor:显式 ts 注入生效", m._last_activity_ts == 12345.0)
    m._fetch_once = lambda: {"window_hours": 5.0, "used_pct": 42.0,
                             "remaining_pct": 58.0, "next_reset_ms": 1}
    m._fetch_and_record()
    d = m.latest()
    check("monitor:成功抓取 latest 可读", d is not None
          and abs(d.get("remaining_pct", 0) - 58.0) < 1e-9)
    check("monitor:成功抓取附加 fetched_at",
          d is not None and isinstance(d.get("fetched_at"), float)
          and d["fetched_at"] > 0)
    t_ok = m._last_fetch_ts
    check("monitor:成功抓取推进 _last_fetch_ts", t_ok is not None)
    m._fetch_once = lambda: None         # 模拟接口失败
    m._fetch_and_record()
    check("monitor:失败仍推进 _last_fetch_ts(占频率预算)",
          m._last_fetch_ts is not None and m._last_fetch_ts >= t_ok,
          f"{m._last_fetch_ts} vs {t_ok}")
    check("monitor:失败不覆盖上次成功结果",
          abs(m.latest().get("remaining_pct", 0) - 58.0) < 1e-9)


# ========== 导出 CSV 数据源:按天×模型明细(口径与金额双防线) ==========

def test_fetch_daily_model_usage_cost_scope():
    """fetch_daily_model_usage(导出 CSV/趋势外推数据源),合成 temp 库钉三件事:
    ①口径 = completed 全部 query_source(subagent 也计入)且与
      fetch_daily_usage_cost 同 WHERE —— 两处对不上,导出明细与按天图表
      互相矛盾,会被用户当 bug;
    ②cny/partial 与手写价格公式逐值对账(expect 硬编码手算数值,不调
      cost_of,防实现自证;input 已含 cache_read 的双计雷区一并钉死);
    ③天界窗口(days 起点外/cancelled 不计)与 GROUP BY d,model + ORDER BY
      d,model 的形状。DB_PATH 打模块级单点(connect_ro 读模块全局,同既有
      CONFIG_PATH patch 先例),finally 恢复,绝不碰真实 ~/.zcode 库。"""
    import datetime as dtmod
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc   # T5 后 DB_PATH 唯一定义在
    # sources/zcode(connect_ro 读它自己的模块全局),patch 打单点才生效

    tmp = _Path(tempfile.mkdtemp(prefix="zm_fdmu_"))
    orig_path = zsrc.DB_PATH
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        con.execute(
            "CREATE TABLE model_usage (started_at INTEGER, model_id TEXT,"
            " status TEXT, query_source TEXT, input_tokens INTEGER,"
            " cache_read_input_tokens INTEGER, output_tokens INTEGER)")
        t0 = de.today0_ms()
        # started_at 全部锚定本地午夜相对偏移,测试任意时刻跑天界都不漂移
        con.executemany(
            "INSERT INTO model_usage VALUES (?,?,?,?,?,?,?)",
            [
                # 今日 GLM-5.3 main_turn(in 已含 cache 1M):
                # (1M*8 + 1M*2 + 0.5M*28)/1M = ¥24.0
                (t0 + 60_000, "GLM-5.3", "completed", "main_turn",
                 2_000_000, 1_000_000, 500_000),
                # 今日 GLM-5.3 subagent 行(口径=全部 query_source,必须计入):
                # (0.5M*8 + 0 + 0.25M*28)/1M = ¥11.0 → 与上行 GROUP BY d,model 合并
                (t0 + 120_000, "GLM-5.3", "completed", "subagent",
                 500_000, 0, 250_000),
                # 今日未知模型:不在价格表 → ¥0 + partial
                (t0 + 180_000, "unknown-model", "completed", "main_turn",
                 100_000, 0, 50_000),
                # 今日 cancelled:状态不符,不计(哪怕 token 巨大)
                (t0 + 240_000, "GLM-5.3", "cancelled", "main_turn",
                 9_000_000, 0, 9_000_000),
                # 昨天 GLM-4.7-Flash(免费档全 0):¥0 且非 partial
                (t0 - 3_600_000, "GLM-4.7-Flash", "completed", "subagent",
                 1_000, 500, 200),
                # 31 天前:days=30 窗口起点外,不得计入
                (t0 - 31 * 86_400_000, "GLM-5.3", "completed", "main_turn",
                 7_000_000, 0, 7_000_000),
            ])
        con.commit()
        con.close()

        de.DB_PATH = tdb
        zsrc.DB_PATH = tdb
        e = DataEngine(queue.Queue(maxsize=1))     # 不 start,同步路径已完整
        # 钉死默认价格表:开发机 zm_prices.json 覆盖会让对账漂移(既有先例)
        e.prices = {m: dict(t) for m, t in de.DEFAULT_PRICES.items()}

        today = dtmod.date.today().isoformat()
        yday = (dtmod.date.today() - dtmod.timedelta(days=1)).isoformat()
        got = e.fetch_daily_model_usage(30)
        # 手算 expect:GLM-5.3 今日两行合并 in=2.5M cache=1M out=0.75M ¥35;
        # 顺序 = ORDER BY d, model_id(日期升序,同日模型名升序)
        expect = [
            (yday, "GLM-4.7-Flash", 1_000, 500, 200, 0.0, False),
            (today, "GLM-5.3", 2_500_000, 1_000_000, 750_000, 35.0, False),
            (today, "unknown-model", 100_000, 0, 50_000, 0.0, True),
        ]
        check("明细行数=3(窗口外/cancelled 不计)", len(got) == 3, str(got))
        check("逐值对账(token/cny 手算/partial)",
              [tuple(r) for r in got] == expect,
              f"got {[tuple(r) for r in got]}")
        # 与 fetch_daily_usage_cost 同 WHERE 的交叉对账:按天聚合 tokens 严格
        # 相等、金额一致(CSV 明细与图表两张皮是本函数的存在性风险)
        cost_rows = {d: (t, c) for d, t, c in e.fetch_daily_usage_cost(30)}
        agg = {}
        for d, model, i_, c_, o_, cny, _p in got:
            tok, c_sum = agg.get(d, (0, 0.0))
            agg[d] = (tok + i_ + o_, c_sum + cny)
        check("与按天图表 tokens 逐天相等",
              all(cost_rows.get(d, (None, None))[0] == t
                  for d, (t, _c) in agg.items()),
              f"{agg} vs {cost_rows}")
        check("与按天图表金额逐天一致",
              all(abs(cost_rows[d][1] - c) < 1e-6 for d, (_t, c) in agg.items()),
              f"{agg} vs {cost_rows}")
        # days=1:起点=今日午夜,只剩今日两行(合并后的 GLM-5.3 与 unknown)
        got1 = e.fetch_daily_model_usage(1)
        check("days=1 只含今日", bool(got1)
              and all(d == today for d, *_ in got1) and len(got1) == 2,
              str(got1))
        e.stop()
    finally:
        de.DB_PATH = orig_path
        zsrc.DB_PATH = orig_path
        shutil.rmtree(tmp, ignore_errors=True)


# ========== sources 包(Provider 配置化):源发现机制 ==========

def test_discover_sources():
    """discover_sources():①内置两源顺序钉死 [ZCode, Claude](order 0/10,
    卡片 today_by_source 显示顺序与迁移前硬编码列表一致);②data_engine
    re-import 的旧导入路径全通且指向同一对象(双定义/双实例防线);
    ③枚举规则用临时假源包(patch __path__,不写仓库)验证 —— `_` 前缀
    模块跳过、无 Source 属性模块跳过、Source=UsageSource 基类本体不算源、
    (order, 模块名) 排序中 order 压过模块名字典序。"""
    import shutil
    import tempfile
    import os as osmod
    import zcode_meter.sources as srcs
    import zcode_meter.sources.zcode as zsrc
    from zcode_meter import data_engine as de
    from zcode_meter.sources import UsageSource, discover_sources

    names = [s.name for s in discover_sources()]
    check("发现:内置顺序 [ZCode, Claude]", names == ["ZCode", "Claude"], str(names))
    check("发现:均为 UsageSource 实例",
          all(isinstance(s, UsageSource) for s in discover_sources()))
    check("发现:重复调用顺序稳定",
          [s.name for s in discover_sources()] == names)
    check("re-import:data_engine 与 sources 同一对象(无双定义)",
          de.ZCodeSource is srcs.ZCodeSource and de.ClaudeSource is srcs.ClaudeSource
          and de.UsageSource is srcs.UsageSource
          and de.connect_ro is zsrc.connect_ro and de.today0_ms is zsrc.today0_ms
          and de.DB_PATH == zsrc.DB_PATH and de.ZCODE_DIR == zsrc.ZCODE_DIR)
    check("re-import:类身份即 sources 模块的 Source 类",
          srcs.ZCodeSource is zsrc.ZCodeSource and zsrc.ZCodeSource.order == 0
          and srcs.ClaudeSource.order == 10)

    # 枚举规则:合成假源目录(patch 包 __path__,finally 还原 + 清 sys.modules)
    root = tempfile.mkdtemp(prefix="zm_srcdisc_")
    orig_path = list(srcs.__path__)
    try:
        pkg = osmod.path.join(root, "fakesrc")
        osmod.makedirs(pkg)
        mods = {
            # order=50 但模块名字典序在前:证明 order 压过模块名
            "aaa": "from zcode_meter.sources.base import UsageSource\n"
                   "class Source(UsageSource):\n"
                   "    name = 'AAA'\n"
                   "    order = 50\n",
            # order=5 但模块名字典序在后:必须排在 AAA 前
            "zzz": "from zcode_meter.sources.base import UsageSource\n"
                   "class Source(UsageSource):\n"
                   "    name = 'ZZZ'\n"
                   "    order = 5\n",
            # `_` 前缀:即使有合法 Source 也必须被跳过
            "_priv": "from zcode_meter.sources.base import UsageSource\n"
                     "class Source(UsageSource):\n"
                     "    name = 'PRIV'\n",
            # 无 Source 属性:跳过不炸
            "nosrc": "x = 1\n",
            # Source=UsageSource 基类本体:不算源(无参构造即 NotImplementedError)
            "raw": "from zcode_meter.sources.base import UsageSource\n"
                   "Source = UsageSource\n",
        }
        for mod_name, text in mods.items():
            with open(osmod.path.join(pkg, mod_name + ".py"), "w",
                      encoding="utf-8") as f:
                f.write(text)
        srcs.__path__ = [pkg]
        got = [s.name for s in discover_sources()]
        check("枚举:order 排序压过模块名(ZZZ 在 AAA 前)",
              got == ["ZZZ", "AAA"], str(got))
        check("枚举:含 _ 前缀/无 Source/基类本体的模块全被跳过",
              "PRIV" not in got, str(got))
    finally:
        srcs.__path__ = orig_path
        # 假模块留在 sys.modules 会以 zcode_meter.sources.<name> 之名常驻,
        # 其文件已删,还原后本不可达 —— 清掉防后续 import 语义漂移
        for mod_name in mods:
            sys.modules.pop(f"zcode_meter.sources.{mod_name}", None)
        shutil.rmtree(root, ignore_errors=True)
    check("还原后内置顺序仍 [ZCode, Claude]",
          [s.name for s in discover_sources()] == names)


def test_cli_cost_report():
    """__main__.py CLI 伴侣(本组唯一非 data_engine 断言,放同文件的原因:
    CLI 与数据层共用回归入口,且断言核心是『fetch_daily_model_usage 的聚合
    展示不漂移口径』)。注入 FakeEngine + redirect_stdout:报表聚合/合计/
    partial 注脚/口径行逐项钉死,不依赖真实库当天形态;参数边界(非法 rc=2、
    越界 clamp)与『零 Qt』红线一并守卫 —— CLI 一旦间接拉起 PySide6,
    无显示环境(服务器/CI)上会直接崩,不再是"伴侣"而是负担。"""
    import contextlib
    import io
    from zcode_meter import __main__ as zm_cli

    class _FakeEngine:
        def __init__(self, out):
            self.out = out

        def fetch_daily_model_usage(self, days):
            assert days == 3, days          # clamp 后的窗口原样传到数据层
            # 手算:09-20 两模型合并 tokens=(1M+0.2M)+(0.5M+0.1M)=1.8M、
            # ¥24.50;09-21=0.5M、¥0.0+partial;合计 2.3M、¥24.50
            return [("2026-09-20", "GLM-5.3", 1_000_000, 800_000, 200_000,
                     24.0, False),
                    ("2026-09-20", "GLM-5.3-Flash", 500_000, 0, 100_000,
                     0.5, False),
                    ("2026-09-21", "unknown-model", 400_000, 0, 100_000,
                     0.0, True)]

        def stop(self):
            pass

    orig = zm_cli.DataEngine
    zm_cli.DataEngine = _FakeEngine       # main() 调用时查模块全局,patch 生效
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = zm_cli.main(["cost", "--days", "3"])
        out = buf.getvalue()
    finally:
        zm_cli.DataEngine = orig
    check("CLI:成功 rc=0", rc == 0, str(rc))
    check("CLI:窗口原样传数据层(--days=3)", rc == 0)  # days!=3 时 Fake 已 assert
    check("CLI:同日多模型按天合并",
          "2026-09-20" in out and "1,800,000" in out and "24.50" in out, out)
    check("CLI:合计行 tokens/¥", "合计" in out and "2,300,000" in out, out)
    check("CLI:窗口含 partial 时打下限注脚", "金额为下限" in out, out)
    check("CLI:口径行恒打印", "ZCode-DB-only" in out and "刊例价" in out, out)

    class _FakeClean(_FakeEngine):
        def fetch_daily_model_usage(self, days):
            return [("2026-09-20", "GLM-5.3", 1_000, 0, 200, 0.02, False)]

    zm_cli.DataEngine = _FakeClean
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = zm_cli.main(["cost", "--days", "1"])
        out = buf.getvalue()
    finally:
        zm_cli.DataEngine = orig
    # 全已知模型时金额是精确值,无条件打『下限』反而是误导(实现内已注裁决)
    check("CLI:无 partial 时注脚不出现", rc == 0 and "金额为下限" not in out, out)

    # 参数边界:非法(非整数)与缺子命令都 rc=2(argparse usage 走 SystemExit,
    # main 捕获转返回值恒 int);越界值 clamp 到 [1,366] 不拒跑
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc_bad = zm_cli.main(["cost", "--days", "abc"])
    check("CLI:--days 非整数 rc=2", rc_bad == 2, f"rc={rc_bad} err={err.getvalue()}")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rc_nocmd = zm_cli.main([])
    check("CLI:缺子命令 rc=2", rc_nocmd == 2, str(rc_nocmd))

    class _FakeWin(_FakeEngine):
        def __init__(self, out):
            self.out = out

        def fetch_daily_model_usage(self, days):
            assert days in (1, 366), days   # clamp 裁决的直接证据
            return []

        def stop(self):
            pass

    zm_cli.DataEngine = _FakeWin
    try:
        for arg, want in (("0", "近 1 天"), ("9999", "近 366 天")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc_c = zm_cli.main(["cost", "--days", arg])
            check(f"CLI:--days={arg} clamp({want})且空窗口不崩",
                  rc_c == 0 and want in buf.getvalue()
                  and "无 completed 用量记录" in buf.getvalue(),
                  buf.getvalue())
    finally:
        zm_cli.DataEngine = orig
    # 零 Qt 红线:import CLI 模块到现在,PySide6 不得在 sys.modules
    check("CLI:零 Qt(未拉起 PySide6)", "PySide6" not in sys.modules)

    # 真实引擎 smoke:main -> DataEngine -> fetch_daily_model_usage 全链路
    # (本文件其余测试同款依赖真实 ~/.zcode 库;仅断言自洽不绑当日数值)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_live = zm_cli.main(["cost", "--days", "7"])
    out_live = buf.getvalue()
    check("CLI:真库 smoke rc=0 且含合计/口径行",
          rc_live == 0 and "合计" in out_live and "口径" in out_live, out_live)



# ========== 重置通知(T4):长周期窗提取 / 重置事件判据 / 一次性通知去重 ==========

def test_parse_quota_cycle_key():
    """cycle 可选键:双 TOKENS_LIMIT 非零窗载荷取 number 最小条目(字符串
    number 形态也认);CREDIT_LIMIT 等其他 type 不入 cycle(钱/额度轨非 token
    计费窗,混入会把额度变动误报成 token 窗重置);无候选时整键省略
    (消费方一律 .get('cycle'));既有 4 键契约不受影响。"""
    from zcode_meter.data_engine import parse_quota_payload
    sample = {"data": {"limits": [
        {"type": "TOKENS_LIMIT", "number": 5, "percentage": "42.5",
         "nextResetTime": 1760000000000},
        {"type": "TOKENS_LIMIT", "number": 30, "percentage": 10.0,
         "nextResetTime": 1761000000000},
        {"type": "TOKENS_LIMIT", "number": "7", "percentage": "3.5",
         "nextResetTime": 1762000000000},
        {"type": "CREDIT_LIMIT", "number": 3, "percentage": 33.0},
    ]}}
    r = parse_quota_payload(sample)
    check("cycle:存在", r is not None and isinstance(r.get("cycle"), dict))
    c = (r or {}).get("cycle") or {}
    check("cycle:取 number 最小(7,非 30;字符串形态也认)",
          c.get("number") == 7, str(c))
    check("cycle:used/remaining=100-percentage",
          abs(c.get("used_pct", 0) - 3.5) < 1e-9
          and abs(c.get("remaining_pct", 0) - 96.5) < 1e-9, str(c))
    check("cycle:nextResetTime 透传", c.get("next_reset_ms") == 1762000000000,
          str(c.get("next_reset_ms")))
    check("cycle:CREDIT_LIMIT 不入(非 token 计费窗)", c.get("number") != 3)
    check("cycle:5h 的 4 键契约不受影响",
          r is not None and r.get("window_hours") == 5
          and abs(r.get("remaining_pct", 0) - 57.5) < 1e-9
          and r.get("next_reset_ms") == 1760000000000
          and abs(r.get("used_pct", 0) - 42.5) < 1e-9)
    # 无非零窗候选:cycle 整键省略(『可选键』语义:仅值合法时含键)
    r2 = parse_quota_payload({"data": {"limits": [
        {"type": "TOKENS_LIMIT", "number": 5, "percentage": 80}]}})
    check("cycle:无候选整键省略",
          r2 is not None and "cycle" not in r2 and r2.get("cycle") is None)
    # 候选缺 nextResetTime:条目仍有效,值为 None(重置事件主判据对该窗
    # 自然失效,回退判据刻意不给 cycle —— 宁缺勿错)
    r3 = parse_quota_payload({"data": {"limits": [
        {"type": "TOKENS_LIMIT", "number": 5, "percentage": 50.0},
        {"type": "TOKENS_LIMIT", "number": 30, "percentage": 10.0}]}})
    c3 = (r3 or {}).get("cycle") or {}
    check("cycle:缺 nextResetTime → 值为 None 但条目保留",
          r3 is not None and c3.get("number") == 30
          and c3.get("next_reset_ms") is None)


def test_quota_reset_event():
    """重置事件三形态 + 判据边界:主判据 nextResetTime 前跳>60s(5h/cycle
    两窗各自比,恰 +60s 不算);回退判据 remaining 跳升 Δ≥30pp 且新值>50
    (仅 5h 窗、仅缺 nextResetTime);双窗同拍归 cycle;prev=None(首查,
    换号/首配 key 防误报的判据根)恒 None。"""
    from zcode_meter.data_engine import quota_reset_event
    base = {"window_hours": 5.0, "used_pct": 80.0, "remaining_pct": 20.0,
            "next_reset_ms": 1_000_000}
    check("事件:prev=None 首查不触发",
          quota_reset_event(None, dict(base)) is None)
    check("事件:cur=None 不触发",
          quota_reset_event(dict(base), None) is None)
    check("事件:快照不变不触发",
          quota_reset_event(dict(base), dict(base)) is None)
    dec = dict(base, next_reset_ms=base["next_reset_ms"] - 5_000)
    check("事件:倒计时递减不触发", quota_reset_event(dict(base), dec) is None)
    jitter = dict(base, next_reset_ms=base["next_reset_ms"] + 60_000)
    check("事件:前跳恰 60s 不触发(容差边界)",
          quota_reset_event(dict(base), jitter) is None)
    jump = dict(base, next_reset_ms=base["next_reset_ms"] + 60_001,
                used_pct=1.0, remaining_pct=99.0)
    check("事件:5h 前跳>60s → '5h'",
          quota_reset_event(dict(base), jump) == "5h")
    # cycle 窗前跳:两侧都有 cycle 才可比(prev 无 cycle = 首见,不触发)
    c_prev = dict(base, cycle={"number": 30, "used_pct": 10.0,
                               "remaining_pct": 90.0, "next_reset_ms": 2_000_000})
    c_jump = dict(base, cycle={"number": 30, "used_pct": 0.5,
                               "remaining_pct": 99.5, "next_reset_ms": 2_060_001})
    check("事件:cycle 前跳>60s → 'cycle'",
          quota_reset_event(c_prev, c_jump) == "cycle")
    check("事件:cycle 首见(prev 无 cycle)不触发",
          quota_reset_event(dict(base), c_jump) is None)
    both_jump = dict(jump, cycle=c_jump["cycle"])
    check("事件:双窗同拍归 cycle(更罕见优先)",
          quota_reset_event(c_prev, both_jump) == "cycle")
    # 回退判据:缺 nextResetTime 时 remaining 跳升(仅 5h)
    p_no = dict(base, next_reset_ms=None)
    c_fb = dict(base, next_reset_ms=None, used_pct=10.0, remaining_pct=90.0)
    check("事件:缺 reset 时刻+remaining 跳升 70pp → '5h'",
          quota_reset_event(p_no, c_fb) == "5h")
    p_21 = dict(base, used_pct=79.0, remaining_pct=21.0, next_reset_ms=None)
    c_51 = dict(base, used_pct=49.0, remaining_pct=51.0, next_reset_ms=None)
    check("事件:Δ恰 30 且新值>50 触发回退",
          quota_reset_event(p_21, c_51) == "5h")
    c_edge = dict(base, next_reset_ms=None, used_pct=50.0, remaining_pct=50.0)
    check("事件:跳升恰 30pp 但新值=50(须>50)不触发",
          quota_reset_event(p_no, c_edge) is None)
    # 有 reset 时刻时主判据独裁:remaining 大跳也不触发
    check("事件:有 reset 时刻时 remaining 跳升不触发",
          quota_reset_event(dict(base), dict(base, used_pct=1.0,
                                            remaining_pct=99.0)) is None)
    # cycle 无回退判据:remaining 跳升但缺 reset 时刻 → None(宁缺勿错)。
    # 5h 侧从 base 出发保持静止(reset 时刻不变→主判据不命中且有值→回退
    # 被抑制),否则 5h 自己的 remaining 跳升会先触发,测不到 cycle 分支
    p_c = dict(base, cycle={"number": 30, "used_pct": 60.0,
                            "remaining_pct": 40.0, "next_reset_ms": None})
    c_c = dict(base, cycle={"number": 30, "used_pct": 5.0,
                            "remaining_pct": 95.0, "next_reset_ms": None})
    check("事件:cycle 不做 remaining 回退(5h 静止)",
          quota_reset_event(p_c, c_c) is None)


def test_alerts_fire_once():
    """BudgetAlerts.fire_once:当日首次 True/同日 False/跨日重置;键空间与
    evaluate 互不干扰;守卫语义同 _save(守卫命中不落盘但内存去重仍成立);
    真实写读路径下 zm_alerts.json 形状不变({"fired": {key: 日期}})且重启
    后同日仍去重。"""
    import json as jsonmod
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    tmp = _Path(tempfile.mkdtemp(prefix="zm_fire_"))
    try:
        a = de.BudgetAlerts([20, 10], state_path=str(tmp / "a.json"))
        check("fire_once:当日首次 True",
              a.fire_once("reset_5h", "2026-01-01") is True)
        check("fire_once:同日第二次 False",
              a.fire_once("reset_5h", "2026-01-01") is False)
        check("fire_once:跨日重置 True",
              a.fire_once("reset_5h", "2026-01-02") is True)
        check("fire_once:键空间与 evaluate 隔离",
              a.fire_once("reset_cycle", "2026-01-02") is True
              and a.evaluate("budget", 15.0, "2026-01-02") is not None)
        # 守卫语义同 _save:显式 patch _no_persist=True(与运行环境无关,
        # run_all 注入 ZM_NO_STATE=1 与裸跑都得同一结果)
        orig = de._no_persist
        de._no_persist = lambda: True
        try:
            g = de.BudgetAlerts([20, 10], state_path=str(tmp / "g.json"))
            check("fire_once:守卫下首次仍 True(内存去重)",
                  g.fire_once("reset_5h", "2026-01-05") is True)
            check("fire_once:守卫下同日仍 False",
                  g.fire_once("reset_5h", "2026-01-05") is False)
            check("fire_once:守卫命中不落盘", not (tmp / "g.json").exists())
        finally:
            de._no_persist = orig
        # 真实写读路径:形状不变 + 重启恢复(同既有 BudgetAlerts 测试模式)
        de._no_persist = lambda: False
        try:
            p = str(tmp / "c.json")
            c0 = de.BudgetAlerts([20, 10], state_path=p)
            check("fire_once:写盘路径首次 True",
                  c0.fire_once("reset_5h", "2026-01-03") is True)
            with open(p, encoding="utf-8") as f:
                obj = jsonmod.load(f)
            check("fire_once:zm_alerts.json 形状不变",
                  isinstance(obj, dict) and set(obj) == {"fired"}
                  and obj["fired"].get("reset_5h") == "2026-01-03", str(obj))
            c1 = de.BudgetAlerts([20, 10], state_path=p)
            check("fire_once:重启后同日仍去重(状态自盘恢复)",
                  c1.fire_once("reset_5h", "2026-01-03") is False)
        finally:
            de._no_persist = orig
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ========== MAX_SCAN_ROWS 历史聚合防御:合成库边界裁剪 + 活库守卫式检查 ==========

def test_max_scan_rows_synthetic():
    """MAX_SCAN_ROWS 防御上限(合成 temp 库,刻意不绑活库行数):patch 常量与
    DB_PATH 后,四个历史聚合查询只统计最近 N 行 —— 最老行被裁、结果与带同
    floor 的手算逐值相等;常量放大后全量相等(SQL 占位符参数绑定的证据,
    floor 若硬编码进 SQL 文本会让 patch 失效、合成库断言翻车)。DB_PATH 打
    模块级单点:T5 后 connect_ro 读 sources.zcode 模块全局,data_engine 侧
    re-export 同步替换(与 test_fetch_daily_model_usage_cost_scope 同款);
    finally 恢复,绝不碰真实 ~/.zcode 库。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc

    tmp = _Path(tempfile.mkdtemp(prefix="zm_scan_"))
    saved_rows = de.MAX_SCAN_ROWS
    orig_path = zsrc.DB_PATH
    e = None
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " model_id TEXT, status TEXT, started_at INTEGER,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER)")
        t0 = de.today0_ms()
        # 5 行 completed 按时间升序插入(rowid 1..5 与时间同序,隐式 rowid 即
        # 插入序):昨天 sess_old×2 + sess_new×1,今天 sess_new×1 + sess_old×1;
        # token 值 1k/2k/4k/8k/16k,任意 floor 的期望和都是手算小整数。
        # 时间全部锚定本地午夜相对偏移,任意时刻跑天界都不漂移(既有先例)。
        con.executemany(
            "INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?)",
            [("sess_old", "main_turn", "GLM-5.3", "completed",
              t0 - 3_600_000, 1_000, 0, 0),
             ("sess_old", "main_turn", "GLM-5.3", "completed",
              t0 - 3_540_000, 2_000, 1_000, 0),
             ("sess_new", "main_turn", "GLM-5.3", "completed",
              t0 - 3_480_000, 4_000, 0, 0),
             ("sess_new", "main_turn", "GLM-5.3", "completed",
              t0 + 3_600_000, 8_000, 0, 0),
             ("sess_old", "main_turn", "GLM-5.3", "completed",
              t0 + 3_660_000, 16_000, 0, 0)])
        con.executemany("INSERT INTO session VALUES (?,?)",
                        [("sess_old", "旧会话"), ("sess_new", "新会话")])
        con.commit()
        con.close()

        de.DB_PATH = tdb
        zsrc.DB_PATH = tdb
        e = de.DataEngine(queue.Queue(maxsize=1))     # 不 start,同步路径已完整
        # 钉死默认价格表:开发机 zm_prices.json 覆盖会让金额对账漂移(既有先例)
        e.prices = {m: dict(t) for m, t in de.DEFAULT_PRICES.items()}

        def manual_daily(floor):
            c = sqlite3.connect(f"file:{tdb}?mode=ro", uri=True)
            r = c.execute(
                "SELECT date(started_at/1000,'unixepoch','localtime') AS d,"
                " COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
                " FROM model_usage WHERE status='completed' AND started_at>=?"
                " AND rowid >= (SELECT COALESCE(MAX(rowid),0) - ? FROM model_usage)"
                " GROUP BY d ORDER BY d",
                (t0 - 86_400_000, floor)).fetchall()
            c.close()
            return r

        def manual_cost(floor):
            c = sqlite3.connect(f"file:{tdb}?mode=ro", uri=True)
            r = c.execute(
                "SELECT date(started_at/1000,'unixepoch','localtime') AS d, model_id,"
                " COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read_input_tokens),0),"
                " COALESCE(SUM(output_tokens),0)"
                " FROM model_usage WHERE status='completed' AND started_at>=?"
                " AND rowid >= (SELECT COALESCE(MAX(rowid),0) - ? FROM model_usage)"
                " GROUP BY d, model_id ORDER BY d",
                (t0 - 86_400_000, floor)).fetchall()
            c.close()
            agg = {}
            for d, model, i_, c_, o_ in r:
                tok, cny = agg.get(d, (0, 0.0))
                v, _p = de.cost_of(de.DEFAULT_PRICES, model, i_ or 0, o_ or 0, c_ or 0)
                agg[d] = (tok + (i_ or 0) + (o_ or 0), cny + v)
            return [(d, t, round(cn, 4)) for d, (t, cn) in sorted(agg.items())]

        # ---- floor=2:保留 rowid 3..5,最老两行(昨日 sess_old)被裁 ----
        de.MAX_SCAN_ROWS = 2
        got = e.fetch_daily_usage(2)
        check("floor=2 按天:最老行被裁(昨日桶 7000→4000)",
              [t for _, t in got] == [4_000, 24_000], str(got))
        check("floor=2 按天=带同 floor 手算逐值相等",
              [tuple(r) for r in got] == [tuple(r) for r in manual_daily(2)],
              f"{got} vs {manual_daily(2)}")
        gotc = e.fetch_daily_usage_cost(2)
        check("floor=2 金额版=带同 floor 手算逐值相等",
              [tuple(r) for r in gotc] == [tuple(r) for r in manual_cost(2)],
              f"{gotc} vs {manual_cost(2)}")
        gots = e.fetch_session_usage(20)
        check("floor=2 按会话:整会话被裁只剩新行",
              [tuple(r) for r in gots] == [("sess_old", "旧会话", 16_000, 1),
                                           ("sess_new", "新会话", 12_000, 2)],
              str(gots))
        c2 = sqlite3.connect(tdb)
        (a2,) = c2.execute(
            "SELECT MIN(started_at) FROM model_usage WHERE status='completed'"
            " AND rowid >= (SELECT COALESCE(MAX(rowid),0) - ? FROM model_usage)",
            (2,)).fetchone()
        c2.close()
        check("floor=2 锚点子查询=裁剪后最早行(t0-58min)",
              a2 == t0 - 3_480_000, str(a2))
        gotb = e.fetch_billing_blocks(29)
        check("floor=2 计费块桶总和=28000(裁 3000)",
              bool(gotb) and len(gotb) == 29
              and sum(b[1] for b in gotb) == 28_000,
              str(sum(b[1] for b in gotb) if gotb else gotb))

        # ---- 常量放大:全量恢复(占位符参数绑定证据,防 floor 硬编码) ----
        de.MAX_SCAN_ROWS = 100_000
        got2 = e.fetch_daily_usage(2)
        check("放大后按天全量=参数绑定非硬编码(昨日桶回 7000)",
              [t for _, t in got2] == [7_000, 24_000]
              and [tuple(r) for r in got2] == [tuple(r) for r in manual_daily(100_000)],
              str(got2))
        check("放大后金额版全量",
              [tuple(r) for r in e.fetch_daily_usage_cost(2)]
              == [tuple(r) for r in manual_cost(100_000)],
              "")
        gots2 = e.fetch_session_usage(20)
        check("放大后按会话全量(sess_old 恢复 3 行 19000)",
              [tuple(r) for r in gots2] == [("sess_old", "旧会话", 19_000, 3),
                                            ("sess_new", "新会话", 12_000, 2)],
              str(gots2))
        gotb2 = e.fetch_billing_blocks(29)
        check("放大后计费块桶总和=31000(全量)",
              bool(gotb2) and sum(b[1] for b in gotb2) == 31_000,
              str(sum(b[1] for b in gotb2) if gotb2 else gotb2))
        # 锚点子查询也吃常量:放大→锚点更早 2 分钟(t0-58min→t0-60min)→
        # base 同步更早,差值为负;两次调用的 now 漂移可忽略,跨块界时差值
        # 平移一个块宽,绝对值对块宽取模后仍应为 2 分钟
        check("锚点随常量放大回移 2 分钟(锚点参数绑定)",
              bool(gotb) and bool(gotb2)
              and abs(gotb2[0][0] - gotb[0][0]) % 18_000_000 == 120_000,
              f"{gotb2[0][0] - gotb[0][0]}")
    finally:
        de.MAX_SCAN_ROWS = saved_rows
        de.DB_PATH = orig_path
        zsrc.DB_PATH = orig_path
        if e is not None:
            e.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_max_scan_rows_live_guard():
    """活库守卫式检查(刻意不绑活库行数,约 45 天后穿越上限):completed 行数
    < MAX_SCAN_ROWS 时,四函数输出与无 floor 手算等价(防线当前零影响的证据);
    行数超上限时打印跳过不失败 —— 硬断言会随库自然增长变 flaky。查询间隙
    可能有新请求完成,函数输出等于前后两次手算之一即通过(既有测试同款
    竞态容忍,见 test_daily_usage_matches_today_scope 注释)。"""
    from zcode_meter import data_engine as de

    con = db()
    e = None
    try:
        (n,) = con.execute(
            "SELECT COUNT(*) FROM model_usage WHERE status='completed'").fetchone()
        if n >= de.MAX_SCAN_ROWS:
            print(f"  SKIP  行数已超上限({n} >= {de.MAX_SCAN_ROWS}),等价检查跳过")
            return
        e = de.DataEngine(queue.Queue(maxsize=1))     # 不 start
        e.prices = {m: dict(t) for m, t in de.DEFAULT_PRICES.items()}
        t0 = de.today0_ms()

        def daily_sql():
            return con.execute(
                "SELECT date(started_at/1000,'unixepoch','localtime') AS d,"
                " COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
                " FROM model_usage WHERE status='completed' AND started_at>=?"
                " GROUP BY d ORDER BY d",
                (t0 - 29 * 86_400_000,)).fetchall()

        m1 = daily_sql()
        got = e.fetch_daily_usage(30)
        m2 = daily_sql()
        check("活库守卫:按天=无 floor 手算", got == m1 or got == m2,
              f"{len(got)} 桶 vs 手算 {len(m1)}/{len(m2)}")

        def cost_sql():
            rows = con.execute(
                "SELECT date(started_at/1000,'unixepoch','localtime') AS d, model_id,"
                " COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read_input_tokens),0),"
                " COALESCE(SUM(output_tokens),0)"
                " FROM model_usage WHERE status='completed' AND started_at>=?"
                " GROUP BY d, model_id ORDER BY d",
                (t0 - 29 * 86_400_000,)).fetchall()
            agg = {}
            for d, model, i_, c_, o_ in rows:
                tok, cny = agg.get(d, (0, 0.0))
                v, _p = de.cost_of(de.DEFAULT_PRICES, model, i_ or 0, o_ or 0, c_ or 0)
                agg[d] = (tok + (i_ or 0) + (o_ or 0), cny + v)
            return [(d, t, round(cn, 4)) for d, (t, cn) in sorted(agg.items())]

        m1 = cost_sql()
        gotc = e.fetch_daily_usage_cost(30)
        m2 = cost_sql()
        check("活库守卫:按天金额=无 floor 手算", gotc == m1 or gotc == m2,
              f"{len(gotc)} 桶 vs 手算 {len(m1)}/{len(m2)}")

        def sess_sql():
            return con.execute(
                "SELECT mu.session_id, COALESCE(s.title,''),"
                " COALESCE(SUM(mu.input_tokens),0)+COALESCE(SUM(mu.output_tokens),0),"
                " COUNT(*) FROM model_usage mu"
                " LEFT JOIN session s ON s.id = mu.session_id"
                " WHERE mu.status='completed' AND mu.query_source='main_turn'"
                " AND mu.session_id NOT LIKE 'sess_subagent%'"
                " GROUP BY mu.session_id"
                " ORDER BY MAX(mu.started_at) DESC LIMIT 20").fetchall()

        m1 = sess_sql()
        gots = e.fetch_session_usage(20)
        m2 = sess_sql()
        check("活库守卫:按会话=无 floor 手算", gots == m1 or gots == m2,
              f"{len(gots)} 行 vs 手算 {len(m1)}/{len(m2)}")

        gotb = e.fetch_billing_blocks(29)
        check("活库守卫:计费块非空(前提)", bool(gotb), str(gotb[:1]))
        if gotb:
            base = gotb[0][0]

            def block_sql():
                (t,) = con.execute(
                    "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
                    " FROM model_usage WHERE status='completed' AND started_at>=?"
                    " AND started_at<?",
                    (base, base + 29 * 18_000_000)).fetchone()
                return t

            s1 = block_sql()
            total = sum(b[1] for b in gotb)
            s2 = block_sql()
            check("活库守卫:计费块桶总和=无 floor 手算",
                  total == s1 or total == s2, f"{total} vs {s1}/{s2}")
    finally:
        con.close()
        if e is not None:
            e.stop()


# ========== T8 刷新档位显式化:手动档决策边界 / quota_refresh 可选键语义 ==========

def test_quota_fetch_decision_manual_mode():
    """手动档(固定间隔轮询)决策:①边界 179.9/180(3min 档)与 899/900
    (15min 档)—— 恰好到点放行,与 auto 档⑤『< 才拒』同侧;②忽略活动/
    静默闸(静默期照查、无活动也照查 —— 用户显式选择压过 auto 语义);
    ③force 首查与 last_fetch=None 兜底保留;④auto 档默认参数行为逐字不变
    (既有 test_quota_fetch_decision 同口径抽测);⑤QuotaMonitor(refresh=)
    存档、缺省 auto。全部纯函数/纯构造,零网络零线程。"""
    from zcode_meter.data_engine import QuotaMonitor, quota_fetch_decision
    now = 1_000_000.0
    # ① 3 分钟档(gap=180):179.9 拒、180 整点放行
    check("手动档:179.9s 拒(180 档)",
          not quota_fetch_decision(now, now - 179.9, now - 30.0,
                                   mode=180, gap=180))
    check("手动档:180s 整点放行",
          quota_fetch_decision(now, now - 180.0, now - 30.0,
                               mode=180, gap=180))
    # ① 15 分钟档(gap=900):899 拒、900 整点放行
    check("手动档:899s 拒(900 档)",
          not quota_fetch_decision(now, now - 899.0, now - 30.0,
                                   mode=900, gap=900))
    check("手动档:900s 整点放行",
          quota_fetch_decision(now, now - 900.0, now - 30.0,
                               mode=900, gap=900))
    # ② 忽略静默/活动:同样输入在 auto 档被④拦,手动档照查(语义差异钉死)
    check("手动档:静默期(>1h 无活动)照查",
          quota_fetch_decision(now, now - 600.0, now - 7200.0,
                               mode=180, gap=180))
    check("对照:auto 档同输入被静默闸拒",
          not quota_fetch_decision(now, now - 600.0, now - 7200.0))
    check("手动档:无活动信号(None)照查",
          quota_fetch_decision(now, now - 600.0, None, mode=180, gap=180))
    # ③ force 首查 / last_fetch=None 兜底保留(与 auto 档同源)
    check("手动档:force 压过间隔",
          quota_fetch_decision(now, now - 10.0, None, force=True,
                               mode=180, gap=180))
    check("手动档:last_fetch=None 放行",
          quota_fetch_decision(now, None, None, mode=180, gap=180))
    # ④ auto 档旧行为(不传 mode/gap)不变:抽既有三断言同值复验
    check("auto 档:默认参数仍活跃 179.9 拒",
          not quota_fetch_decision(now, now - 179.9, now - 30.0))
    check("auto 档:默认参数仍 180 放行",
          quota_fetch_decision(now, now - 180.0, now - 30.0))
    check("auto 档:默认参数仍静默拒",
          not quota_fetch_decision(now, now - 600.0, now - 3600.1))
    # ⑤ monitor 存档(仅构造绝不 start,网络红线)
    m = QuotaMonitor("sk-test", 300)
    check("monitor:refresh=300 已存档", m.refresh == 300)
    check("monitor:缺省 refresh='auto'", QuotaMonitor("sk-test").refresh == "auto")


def test_quota_refresh_optional_key():
    """quota_refresh 可选键语义(load/save 仅合法时含键):
    ①三键 cfg 落盘文件仍三键 —— test_save_config_roundtrip 的 back2 三键
    全等断言零改动的根基;②合法值 "auto"/60/86400/180 回环逐值相等;
    ③非法值(越界 59/86401、字符串 "180"、float 180.5、bool True)save
    整键省略、load 读含非法值的文件也无键(其他三键不受污染)。全部显式
    path 临时文件,不碰真实 zm_config.json。"""
    import json as jsonmod
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de

    tmp = _Path(tempfile.mkdtemp(prefix="zm_qref_"))
    orig_path = de.CONFIG_PATH
    base = {"quota_api_key": "sk-x", "daily_budget_cny": 5, "alert_pct": [20.0, 10.0]}
    three = {"quota_api_key": "sk-x", "daily_budget_cny": 5.0,
             "alert_pct": [20.0, 10.0]}
    try:
        # ① 三键 cfg(无 quota_refresh)→ 原子文件形状仍三键,load 全等三键
        p1 = str(tmp / "c1.json")
        check("save:三键 cfg 返回 True", de.save_config(base, path=p1) is True)
        raw = jsonmod.loads(_Path(p1).read_text(encoding="utf-8"))
        check("save:文件无第 4 键", set(raw) == set(three), str(sorted(raw)))
        de.CONFIG_PATH = p1
        check("load:三键全等(back2 断言形状)",
              de.load_config() == three, str(de.load_config()))
        # ② 合法值回环:auto 字面值与间隔秒数逐值相等
        for val in ("auto", 60, 86400, 180):
            p = str(tmp / f"c_{val}.json")
            cfg = dict(base, quota_refresh=val)
            check(f"save:{val!r} 合法落盘 True", de.save_config(cfg, path=p) is True)
            de.CONFIG_PATH = p
            back = de.load_config()
            check(f"load:{val!r} 回环含键同值",
                  back.get("quota_refresh") == val, str(back))
        # ③ 非法值:save 省键(文件仍三键)、load 遇非法值当无键
        for bad in (59, 86401, "180", 180.5, True, None):
            p = str(tmp / f"bad_{bad!r}.json")
            check(f"save:非法 {bad!r} 仍可落盘",
                  de.save_config(dict(base, quota_refresh=bad), path=p) is True)
            raw = jsonmod.loads(_Path(p).read_text(encoding="utf-8"))
            check(f"save:非法 {bad!r} 整键省略", "quota_refresh" not in raw,
                  str(sorted(raw)))
        pb = str(tmp / "load_bad.json")
        _Path(pb).write_text(jsonmod.dumps(
            {"quota_api_key": "sk-x", "daily_budget_cny": 5,
             "alert_pct": [20, 10], "quota_refresh": 30}), encoding="utf-8")
        de.CONFIG_PATH = pb
        back = de.load_config()
        check("load:文件含越界 30 → 无键", "quota_refresh" not in back, str(back))
        check("load:其他三键不受污染", back == three, str(back))
    finally:
        de.CONFIG_PATH = orig_path
        shutil.rmtree(tmp, ignore_errors=True)


# ================= 趋势外推(T3:trend_forecast 纯函数,合成行手算对账) =================

def test_trend_forecast_hand_computed():
    """trend_forecast:7 日历日窗口补零、avg 恒除 7(不按有数据天数)、
    forecast=avg_cny×当月真实天数、零用量→None、partial 透传、窗口外旧行
    忽略 —— 全部手算对账;不触库,today 显式注入(跨月/闰年不 flaky)。"""
    import datetime as dt
    from zcode_meter.data_engine import trend_forecast
    T = dt.date(2026, 9, 27)          # 9 月 30 天;窗口 = 09-21..09-27
    rows = [
        # (date, model, in_tok, cache, out_tok, cny, partial) —— T2 行形状
        ("2026-09-27", "GLM-5.3",       7000,  5600,  1400, 0.1000, False),
        ("2026-09-27", "GLM-5.3-Flash",  3000,  2400,   600, 0.0100, False),
        ("2026-09-26", "GLM-5.3",      14000, 11200,  2800, 0.2000, False),
        ("2026-09-25", "deepseek-x",        500,     0,   500, 0.0000, True),
        ("2026-09-23", "GLM-5.3",       7000,  5600,  1400, 0.1000, False),
        ("2026-09-14", "GLM-5.3",   99999999,     0, 99999, 999.0, False),
    ]
    # 09-21/22/24 为零日(补零摊薄,不抬均值)。手算:
    # tokens = 8400+3600+16800+1000+8400 = 38200(09-14 窗口外不计)
    # avg_tokens = 38200/7 = 5457.1428… → 5457.1
    # cny = 0.10+0.01+0.20+0.00+0.10 = 0.41;avg_cny = 0.41/7 → 0.0586
    # forecast = 0.0586 × 30(9 月天数)= 1.758
    r = trend_forecast(rows, today=T)
    check("趋势:avg_tokens 恒除 7(零日补零摊薄)", r is not None
          and r["avg_tokens"] == round(38200 / 7, 1), str(r and r["avg_tokens"]))
    check("趋势:avg_cny 手算对账", r is not None
          and r["avg_cny"] == round(0.41 / 7, 4), str(r and r["avg_cny"]))
    check("趋势:forecast=avg_cny×当月天数(9月=30)",
          r is not None
          and r["forecast_cny"] == round(round(0.41 / 7, 4) * 30, 4),
          str(r and r["forecast_cny"]))
    check("趋势:窗口内含未知模型行 → partial", r is not None and r["partial"] is True)
    r2 = trend_forecast([row for row in rows if row[1] != "deepseek-x"], today=T)
    check("趋势:剔除未知模型行后 partial=False", r2 is not None
          and r2["partial"] is False, str(r2 and r2["partial"]))
    # 当月天数以 today 所在月为准:非闰年 2 月 = 28
    rfeb = trend_forecast(
        [("2027-02-15", "GLM-5.3", 700, 0, 700, 0.70, False)],
        today=dt.date(2027, 2, 15))
    check("趋势:forecast 按当月真实天数(2027-02=28)",
          rfeb is not None
          and rfeb["forecast_cny"] == round(round(0.70 / 7, 4) * 28, 4),
          str(rfeb and rfeb["forecast_cny"]))
    # 空窗口 / 全零窗口 / 仅窗口外旧数据 → None(宁缺勿错)
    check("趋势:空行列表 → None", trend_forecast([], today=T) is None)
    check("趋势:全零用量窗口 → None",
          trend_forecast([("2026-09-26", "GLM-5.3", 0, 0, 0, 0.0, False)],
                         today=T) is None)
    check("趋势:仅窗口外旧数据 → None",
          trend_forecast([("2026-09-14", "GLM-5.3", 1000, 0, 1000, 1.0, False)],
                         today=T) is None)
    check("趋势:today 缺省走本地时钟可用",
          trend_forecast(rows) is not None)


# ========== 速度趋势 sparkline 数据源(v0.8.0 T4:合成库手算对账) ==========

def test_fetch_recent_speeds_synthetic():
    """fetch_recent_speeds(速度趋势 sparkline 数据源)合成 temp 库对账:
    ①逐条速度手算 + 时间正序(值/序);②n 截断取最近 n 条且仍正序;
    ③排除项:同会话 subagent(v0.2.0 劫持事故红线)/cancelled/零输出/
    缺时长/他会话;④COALESCE(ttft,0) 与 max(,1) 防零除两边界;⑤异常路径
    空表(connect 失败 ⊂ sqlite3.Error,与 _poll_stats 同吞法不炸);
    ⑥Snapshot.recent_speeds 接线:_poll_stats 填充后非 None 且与直查同值
    (_switch_session 重建后同款补全,不闪空)。DB_PATH 双 patch 单点
    (test_fetch_total_usage 同款),finally 恢复,绝不碰真实 ~/.zcode 库。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc

    tmp = _Path(tempfile.mkdtemp(prefix="zm_spd_"))
    orig = zsrc.DB_PATH
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        # part 只需 session_id 列:_latest_session 的跟随查询就够用,
        # 让构造器自然锚到 sess_main(顺带覆盖缺省参数=当前会话路径)
        con.execute("CREATE TABLE part (session_id TEXT)")
        # model_usage 全列:_poll_stats 接线断言要完整跑一轮(缺列会让整段
        # 查询被 sqlite3.Error 吞掉,recent_speeds 停留 None —— 那正是要测的)
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER)")
        t0 = de.today0_ms()

        def mu(sid, src, status, out, dur, ttft):
            return (sid, src, status, t0, "GLM-5.3", "bigmodel",
                    1_000, 0, out, dur, ttft)

        con.executemany(
            "INSERT INTO model_usage (session_id, query_source, status,"
            " started_at, model_id, provider_id, input_tokens,"
            " cache_read_input_tokens, output_tokens, duration_ms,"
            " time_to_first_token_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                # ---- 计入行(rowid 1..5 = 时间序,速度全部手算)----
                # r1: 900 / ((10000-1000)/1000) = 900/9 = 100.0
                mu("sess_main", "main_turn", "completed", 900, 10_000, 1_000),
                # r2: ttft NULL → COALESCE 0:1800 / (10000/1000) = 180.0
                mu("sess_main", "main_turn", "completed", 1_800, 10_000, None),
                # r3: 1350 / 9 = 150.0
                mu("sess_main", "main_turn", "completed", 1_350, 10_000, 1_000),
                # r4: 450 / 9 = 50.0
                mu("sess_main", "main_turn", "completed", 450, 10_000, 1_000),
                # r5: dur==ttft → 净生成 0 → max(,1)=1ms 防零除:100/0.001=100000.0
                mu("sess_main", "main_turn", "completed", 100, 1_000, 1_000),
                # ---- 排除行(插在尾部:漏入任何一条都会撑爆列表长度/混入
                # 1000.0 档速度,逐条可归因)----
                # 同会话 subagent:不得进曲线(与主速度口径不符)
                mu("sess_main", "subagent", "completed", 9_000, 10_000, 1_000),
                # cancelled:status 不符
                mu("sess_main", "main_turn", "cancelled", 9_000, 10_000, 1_000),
                # 零输出:output_tokens>0 滤掉(0 速点无信息量)
                mu("sess_main", "main_turn", "completed", 0, 10_000, 1_000),
                # 缺时长:duration_ms IS NOT NULL 滤掉(速度无分母)
                mu("sess_main", "main_turn", "completed", 5_000, None, 1_000),
                # 其他会话:session 过滤(该会话单独查时应回它自己)
                mu("sess_other", "main_turn", "completed", 9_000, 10_000, 1_000),
            ])
        con.execute("INSERT INTO part (session_id) VALUES ('sess_main')")
        con.commit()
        con.close()

        zsrc.DB_PATH = tdb; de.DB_PATH = tdb
        e = de.DataEngine(queue.Queue(maxsize=1))     # 不 start,同步路径已完整

        check("sparkline:引擎跟随 sess_main", e.session_id == "sess_main",
              e.session_id)
        expect_full = [100.0, 180.0, 150.0, 50.0, 100_000.0]
        full = e.fetch_recent_speeds()                # 缺省 = 当前会话 n=12
        check("sparkline:多行值+时间正序(手算)",
              len(full) == 5 and all(abs(a - b) < 1e-9
                                     for a, b in zip(full, expect_full)),
              f"got {full}")
        check("sparkline:显式 session_id 同结果",
              e.fetch_recent_speeds(12, "sess_main") == full, "")
        got_other = e.fetch_recent_speeds(12, "sess_other")
        check("sparkline:他会话只回该会话行",
              len(got_other) == 1 and abs(got_other[0] - 1000.0) < 1e-9,
              f"got {got_other}")
        got3 = e.fetch_recent_speeds(3)               # 截断取最近 3 条(r3/r4/r5)
        check("sparkline:n=3 截断取最近仍正序",
              len(got3) == 3 and all(abs(a - b) < 1e-9 for a, b in
                                     zip(got3, [150.0, 50.0, 100_000.0])),
              f"got {got3}")
        check("sparkline:无记录会话空表",
              e.fetch_recent_speeds(12, "sess_none") == [])
        # 接线:_poll_stats 在 snap_lock 内填充(直调与 _switch_session 内部
        # 同一调用点;不 start 免线程竞态)
        e._poll_stats()
        rs = e.snap.recent_speeds
        check("sparkline:Snapshot.recent_speeds 已接线(_poll_stats)",
              isinstance(rs, list) and len(rs) == 5
              and all(abs(a - b) < 1e-9 for a, b in zip(rs, expect_full)),
              f"got {rs}")
        e.stop()

        # 异常路径:DB_PATH 指向不存在的库 → connect_ro 抛 OperationalError
        # ⊂ sqlite3.Error → 返回 [](UI 按 <2 点隐藏不闪空,引擎线程不炸)
        zsrc.DB_PATH = str(tmp / "nope.sqlite"); de.DB_PATH = zsrc.DB_PATH
        e2 = de.DataEngine(queue.Queue(maxsize=1))
        check("sparkline:查询异常 → 空表",
              e2.fetch_recent_speeds() == [] and e2.snap.recent_speeds is None)
        e2.stop()
    finally:
        zsrc.DB_PATH = orig; de.DB_PATH = orig
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print("== test_fetch_total_usage =="); test_fetch_total_usage()
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
    # ---- v0.5.1 新增(事件驱动节流/本地倒计时/活动信号,全部无网络) ----
    print("== test_quota_fetch_decision ==");              test_quota_fetch_decision()
    print("== test_quota_countdown_and_age ==");           test_quota_countdown_and_age()
    print("== test_engine_activity_signal ==");            test_engine_activity_signal()
    print("== test_quota_monitor_throttle_bookkeeping =="); test_quota_monitor_throttle_bookkeeping()
    # ---- 导出 CSV 数据源(合成 temp 库,不碰真实 ~/.zcode 库) ----
    print("== test_fetch_daily_model_usage_cost_scope =="); test_fetch_daily_model_usage_cost_scope()
    # ---- sources 包源发现机制(patch __path__ 的临时假源包,不写仓库) ----
    print("== test_discover_sources =="); test_discover_sources()
    # ---- CLI 伴侣(__main__.py:注入 Fake + redirect_stdout,零 UI 零 Qt) ----
    print("== test_cli_cost_report =="); test_cli_cost_report()
    # ---- 重置通知(T4:cycle 提取/事件判据/一次性去重,全部无网络) ----
    print("== test_parse_quota_cycle_key ==");              test_parse_quota_cycle_key()
    print("== test_quota_reset_event ==");                  test_quota_reset_event()
    print("== test_alerts_fire_once ==");                   test_alerts_fire_once()

    # ---- MAX_SCAN_ROWS 历史聚合防御(合成库边界裁剪 + 活库守卫,不绑活库行数) ----
    print("== test_max_scan_rows_synthetic ==");    test_max_scan_rows_synthetic()
    print("== test_max_scan_rows_live_guard ==");   test_max_scan_rows_live_guard()
    # ---- 刷新档位显式化(T8:手动档决策边界 + quota_refresh 可选键语义) ----
    print("== test_quota_fetch_decision_manual_mode =="); test_quota_fetch_decision_manual_mode()
    print("== test_quota_refresh_optional_key ==");       test_quota_refresh_optional_key()
    # ---- 趋势外推(T3:trend_forecast 纯函数,合成行手算对账,零网络零库) ----
    print("== test_trend_forecast_hand_computed ==");     test_trend_forecast_hand_computed()
    # ---- 速度趋势 sparkline 数据源(v0.8.0 T4:合成库手算对账,值/序/截断/排除/异常) ----
    print("== test_fetch_recent_speeds_synthetic ==");    test_fetch_recent_speeds_synthetic()
    if FAILED:
        print(f"\nFAILED: {FAILED}")
        sys.exit(1)
    print("\nDATA ENGINE TESTS ALL PASS")
