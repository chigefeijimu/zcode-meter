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


def _gptps(eng, now: float) -> float:
    """_global_part_tps 的总贡献分量。#12 起该函数返回 (tps, {sid: 贡献})
    二元组 —— 既有断言只消费总量,per-session dict 由 #12 专项测试覆盖。"""
    return eng._global_part_tps(now)[0]


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


def _claude_line_factory():
    """T5 ClaudeSource 增量解析单测共用:本地时间→Claude jsonl ISO-UTC
    时间戳与行构造器(与 test_claude_source_synthetic 同形态)。mid 前缀
    由各测试自带(inc_/rep_/conc_)—— 类级缓存跨实例共享,跨测试复用
    同名 mid 会跨临时目录串扰归属。"""
    import datetime as dtmod
    import json as jsonmod
    t0 = dtmod.datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0)
    t_today = t0 + dtmod.timedelta(minutes=30)       # 本地刚过午夜
    t_yday = t0 - dtmod.timedelta(milliseconds=1)    # 本地昨天最后一毫秒

    def iso(local_dt):
        return (local_dt.astimezone(dtmod.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")

    def line(ts, mid, i, cr, cc, o, bad_ts=False, side=False, typ="assistant"):
        obj = {"type": typ, "timestamp": "not-a-timestamp" if bad_ts else iso(ts),
               "message": {"id": mid, "usage": {
                   "input_tokens": i, "cache_read_input_tokens": cr,
                   "cache_creation_input_tokens": cc, "output_tokens": o}}}
        if side:
            obj["isSidechain"] = True
        return jsonmod.dumps(obj, ensure_ascii=False)

    return line, t_today, t_yday


def test_claude_source_incremental_append():
    """T5 ClaudeSource 增量偏移解析(关 registry#19):追加只解析新增字节
    且与『从零重解析』全等;同 mid 多行只计首行(含跨追加批次);坏
    JSON/坏时间戳追加行跳过;未终止残行不预计、补全换行后一次计入;
    SCAN_TTL 兜底(watcher 死亡 → 纯 _scan 尾读也能吃到追加);
    跨午夜按本地天界切换。"""
    import datetime as dtmod
    import os as osmod
    import shutil
    import tempfile
    from zcode_meter.data_engine import ClaudeSource

    line, t_today, t_yday = _claude_line_factory()
    today_iso = dtmod.date.today().isoformat()
    yday_iso = (dtmod.date.today() - dtmod.timedelta(days=1)).isoformat()

    def write_all(path, rows, mode="w"):
        with open(path, mode, encoding="utf-8") as f:
            f.write("".join(r + "\n" for r in rows))

    root = tempfile.mkdtemp(prefix="zm_claude_inc_")
    root2 = None
    try:
        proj = osmod.path.join(root, "p1")
        osmod.makedirs(proj)
        f1 = osmod.path.join(proj, "s1.jsonl")
        write_all(f1, [
            line(t_today, "inc_a", 100, 10, 5, 50),   # in=115 out=50 → 165
            line(t_today, "inc_a", 100, 10, 5, 50),   # 同 mid 复行 → 只计首行
            line(t_yday, "inc_b", 200, 0, 0, 30),     # 本地昨天 230
        ])
        src = ClaudeSource(projects_dir=root)
        src.SCAN_TTL = 0.05                           # 实例级覆盖,免 15s 等待
        check("冷解析今日含 cache 补齐", src.today_usage() == 165,
              str(src.today_usage()))
        daily = dict(src.daily_usage(2))
        check("冷解析昨日键", daily.get(yday_iso) == 230, str(daily))

        # ---- 追加(watcher 路径):note_changes 增量尾读 ----
        write_all(f1, [
            line(t_today, "inc_c", 7, 0, 0, 3),       # +10
            "{not json",                              # 坏 JSON 行跳过
            line(t_today, "inc_c", 7, 0, 0, 3),       # 同 mid 跨批次再追加 → 不双计
            line(t_today, "inc_badts", 9, 0, 0, 9, bad_ts=True),  # 坏时间戳跳过
            line(t_yday, "inc_eve", 1, 0, 0, 1),      # 本地昨天午夜前 1ms → +2 归昨天
            line(t_today, "inc_morn", 2, 0, 0, 2),    # 今天(午夜后)→ +4
        ], mode="a")
        src.note_changes([f1])
        got = src.today_usage()
        check("增量追加后今日值", got == 165 + 10 + 4, str(got))
        daily = dict(src.daily_usage(2))
        check("增量跨午夜天界",
              daily.get(yday_iso) == 230 + 2 and daily.get(today_iso) == 179,
              str(daily))

        # ---- 与从零重解析全等:新目录放最终内容,冷实例聚合必须相等 ----
        root2 = tempfile.mkdtemp(prefix="zm_claude_inc2_")
        proj2 = osmod.path.join(root2, "p1")
        osmod.makedirs(proj2)
        with open(f1, "rb") as a, open(osmod.path.join(proj2, "s1.jsonl"), "wb") as b:
            b.write(a.read())
        src2 = ClaudeSource(projects_dir=root2)
        check("增量==从零重解析(今日)",
              src2.today_usage() == src.today_usage(),
              f"{src2.today_usage()} vs {src.today_usage()}")
        check("增量==从零重解析(按天)",
              dict(src2.daily_usage(30)) == dict(src.daily_usage(30)))

        # ---- 残行不预计:追加未终止的行 → 不计;补全换行 → 一次计入 ----
        with open(f1, "a", encoding="utf-8") as f:
            f.write(line(t_today, "inc_partial", 5, 0, 0, 5))   # 无换行残行
        src.note_changes([f1])
        check("未终止残行不预计", src.today_usage() == 179, str(src.today_usage()))
        with open(f1, "a", encoding="utf-8") as f:
            f.write("\n")                                       # 补全
        src.note_changes([f1])
        check("残行补全后一次计入", src.today_usage() == 189,
              str(src.today_usage()))

        # ---- SCAN_TTL 兜底:不经 note_changes,等 TTL 过期由 _scan 尾读 ----
        before = src.today_usage()
        write_all(f1, [line(t_today, "inc_ttl", 1, 0, 0, 1)], mode="a")
        time.sleep(0.08)                                        # > SCAN_TTL=0.05
        check("SCAN_TTL 兜底尾读", src.today_usage() == before + 2,
              f"{src.today_usage()} expect {before + 2}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        if root2:
            shutil.rmtree(root2, ignore_errors=True)


def test_claude_source_reparse_rebuild():
    """T5 N1 裁决:全局 mid→owner 所有权 + 整文件重解析的确定性重建。
    ①共享 mid 首 claim 者胜(摄取序=sorted(path),a.jsonl 先于 b.jsonl);
    ②owner 文件截断重写后共享 mid 由幸存文件重新计入,结果==从零重建;
    ③重写非共享行后归属稳定、只计新值;④原地重写(size 不变 mtime 变)
    不双计;⑤文件删除(vanished)→ 幸存文件重新计入。每步都与
    『从零重解析(同 sorted 序)』对账。"""
    import os as osmod
    import shutil
    import tempfile
    import time as timemod
    from zcode_meter.data_engine import ClaudeSource

    line, t_today, _t_yday = _claude_line_factory()

    def write_all(path, rows):
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(r + "\n" for r in rows))

    def cold(files):
        """从零重解析对照:全新目录放同样内容,冷实例首扫(摄取序同为
        sorted(path),与重建序一致)。"""
        d = tempfile.mkdtemp(prefix="zm_claude_cold_")
        try:
            p = osmod.path.join(d, "p2")
            osmod.makedirs(p)
            for name, rows in files.items():
                write_all(osmod.path.join(p, name), rows)
            return ClaudeSource(projects_dir=d).today_usage()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    root = tempfile.mkdtemp(prefix="zm_claude_rep_")
    try:
        proj = osmod.path.join(root, "p2")
        osmod.makedirs(proj)
        fa = osmod.path.join(proj, "a.jsonl")
        fb = osmod.path.join(proj, "b.jsonl")
        rows_a = [line(t_today, "rep_m", 100, 0, 0, 10),    # 共享 mid(110)
                  line(t_today, "rep_oa", 1, 0, 0, 1)]      # a 独占(2)
        rows_b = [line(t_today, "rep_m", 500, 0, 0, 50),    # 同 mid → b 抑制
                  line(t_today, "rep_ob", 2, 0, 0, 2)]      # b 独占(4)
        write_all(fa, rows_a)
        write_all(fb, rows_b)
        src = ClaudeSource(projects_dir=root)
        src.SCAN_TTL = 0.05
        check("共享 mid 首 claim 归 a(sorted 序)",
              src.today_usage() == 110 + 2 + 4, str(src.today_usage()))

        # ②owner 文件 a 截断重写(更短,不再含 rep_m)→ b 的 rep_m 解禁
        rows_a = [line(t_today, "rep_oa2", 5, 0, 0, 5)]     # 新 a 只有 10
        write_all(fa, rows_a)
        src.note_changes([fa])
        check("owner 截断重写后 mid 归 b(N1)",
              src.today_usage() == 10 + 550 + 4, str(src.today_usage()))
        check("N1 重建==从零重建",
              cold({"a.jsonl": rows_a, "b.jsonl": rows_b}) == src.today_usage())

        # ③重写 b(rep_m 用量变):归属仍在 b、只计新值(双计则 564+330)
        rows_b = [line(t_today, "rep_m", 300, 0, 0, 30),
                  line(t_today, "rep_ob", 2, 0, 0, 2)]
        write_all(fb, rows_b)
        src.note_changes([fb])
        check("重写后归属稳定只计新值",
              src.today_usage() == 10 + 330 + 4, str(src.today_usage()))
        check("重写对账==从零重建",
              cold({"a.jsonl": rows_a, "b.jsonl": rows_b}) == src.today_usage())

        # ④原地重写:size 不变(300→600、30→60 位数相同)、mtime 变 →
        # 只计新内容;若按追加尾读则旧字节重解析 → 双计可判别
        timemod.sleep(0.02)                                 # 保证 mtime_ns 前进
        rows_b = [line(t_today, "rep_m", 600, 0, 0, 60),
                  line(t_today, "rep_ob", 2, 0, 0, 2)]
        write_all(fb, rows_b)
        src.note_changes([fb])
        check("原地重写不双计", src.today_usage() == 10 + 660 + 4,
              str(src.today_usage()))
        check("原地重写对账==从零重建",
              cold({"a.jsonl": rows_a, "b.jsonl": rows_b}) == src.today_usage())

        # ⑤删除 a(vanished 检测):a 的独占 mid 随之消失,b 不受影响
        osmod.remove(fa)
        timemod.sleep(0.08)                                 # > SCAN_TTL,纯 _scan 路径
        check("删除文件后幸存者重计", src.today_usage() == 660 + 4,
              str(src.today_usage()))
        check("删除对账==从零重建",
              cold({"b.jsonl": rows_b}) == src.today_usage())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_claude_source_note_changes_concurrency():
    """T5 并发与 TTL 交互:①note_changes 后 TTL 聚合立即失效(不等
    SCAN_TTL)—— watch→wake→push Live 链路的取数前提;②watcher 线程
    note_changes 与引擎线程 today_usage/daily_usage 并发压力,终值==
    串行期望且全程无异常(类级锁零竞态)。"""
    import os as osmod
    import shutil
    import tempfile
    import threading
    import time as timemod
    from zcode_meter.data_engine import ClaudeSource

    line, t_today, _t_yday = _claude_line_factory()

    def write_all(path, rows, mode="w"):
        with open(path, mode, encoding="utf-8") as f:
            f.write("".join(r + "\n" for r in rows))

    root = tempfile.mkdtemp(prefix="zm_claude_conc_")
    try:
        proj = osmod.path.join(root, "p3")
        osmod.makedirs(proj)
        f1 = osmod.path.join(proj, "s.jsonl")
        write_all(f1, [line(t_today, "conc_0", 1, 0, 0, 1)])
        src = ClaudeSource(projects_dir=root)
        check("并发前冷值", src.today_usage() == 2, str(src.today_usage()))

        # ①TTL 失效:默认 15s TTL 窗口内,note_changes 后必须立即可见
        write_all(f1, [line(t_today, "conc_1", 2, 0, 0, 2)], mode="a")
        src.note_changes([f1])
        check("note_changes 后立即可见(不等 TTL)",
              src.today_usage() == 2 + 4, str(src.today_usage()))

        # ②并发压力:后台线程模拟 watcher(追加+note_changes),
        # 主线程模拟引擎轮询(today_usage/daily_usage 各 TTL 读)
        N = 60
        errors = []

        def watcher():
            try:
                for i in range(N):
                    write_all(f1, [line(t_today, f"conc_w{i}", 1, 0, 0, 1)],
                              mode="a")
                    src.note_changes([f1])
                    timemod.sleep(0.002)
            except Exception as ex:      # noqa: BLE001 单测只需收集线程异常
                errors.append(repr(ex))

        th = threading.Thread(target=watcher, daemon=True)
        th.start()
        reads = 0
        while th.is_alive():
            try:
                src.today_usage()
                src.daily_usage(2)
                reads += 1
            except Exception as ex:      # noqa: BLE001
                errors.append(repr(ex))
            timemod.sleep(0.001)
        th.join()
        expect = 2 + 4 + N * 2
        check("并发终值==串行期望", src.today_usage() == expect,
              f"{src.today_usage()} expect {expect}")
        check("并发全程无异常", not errors, "; ".join(errors[:3]))
        check("并发期间确有读发生", reads > 10, str(reads))
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
        back2 = {k: back2.get(k) for k in ("quota_api_key", "daily_budget_cny",
                                           "alert_pct")}
        check("坏形规范化为默认(前三键)",
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
            # Source=UsageSource 基类本体:不算源(纯接口再导出豁免)。
            # #91 注:守卫的必要性不在『无参构造会抛』—— 普通类无参构造完全
            # 成功,NotImplementedError 延迟到 today_usage() 调用侧才抛(引擎
            # #17 守卫兜底);守卫真正防的是『接口再导出』被当源声明,让
            # 潜伏源行活到引擎每秒现调时才被跳过
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
        def __init__(self, out, boot_queries=True):   # #84:CLI 现传 boot_queries
            self.out = out

        def fetch_daily_model_usage(self, days, raise_on_error=False):
            assert days == 3, days          # clamp 后的窗口原样传到数据层
            # raise_on_error 形参镜像真实签名(#64b CLI 查询侧故障通道):
            # main() 现以 raise_on_error=True 调用,缺参会 TypeError
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
        def fetch_daily_model_usage(self, days, raise_on_error=False):
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
        def __init__(self, out, boot_queries=True):
            self.out = out

        def fetch_daily_model_usage(self, days, raise_on_error=False):
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
        _lc = de.load_config()
        check("load:三键全等(可选键 bar_segments 除外)",
              all(_lc.get(k) == v for k, v in three.items()), str(_lc))
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
        check("load:其他三键不受污染(可选键除外)",
              all(back.get(k) == v for k, v in three.items()), str(back))
    finally:
        de.CONFIG_PATH = orig_path
        shutil.rmtree(tmp, ignore_errors=True)


# ================= 皮肤配置键(v0.8.x 皮肤系统:白名单/归一化/可选键落盘) =================

def test_skin_optional_key():
    """skin 配置键语义(白名单归一化 + 可选键落盘,结构镜像
    test_quota_refresh_optional_key):
    ①三键 cfg 落盘文件仍三键 —— 既有 test_save_config_roundtrip back2 三键
    全等断言零改动的根基;load 恒含归一化 skin(缺省 "glass");
    ②白名单内非缺省值("crt")save 落盘读回环相等;glass 输入=整键省略
    (文件无 skin 键,与 quota_refresh="auto" 同省键纪律);
    ③非法值("dark"/123/[]/"")save 整键省略、load 读含非法值的文件也
    回 "glass"(其他三键不受污染,坏值不驻留文件);
    ④缺文件早退路径恒含 skin=="glass"(默认 dict 携带,『恒含』才是完整
    承诺 —— 只在函数末尾置键会让早退路径仍是无键形状)。
    全部显式 path 临时文件,不碰真实 zm_config.json。"""
    import json as jsonmod
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de

    tmp = _Path(tempfile.mkdtemp(prefix="zm_skin_"))
    orig_path = de.CONFIG_PATH
    base = {"quota_api_key": "sk-x", "daily_budget_cny": 5, "alert_pct": [20.0, 10.0]}
    three = {"quota_api_key": "sk-x", "daily_budget_cny": 5.0,
             "alert_pct": [20.0, 10.0]}
    try:
        # ⓪ 白名单契约:SKIN_IDS 9 款无重复、glass 在首位(缺省即第 0 款);
        # _norm_skin 纯函数对任意类型输入安全(list/bool/None 不抛错)
        check("白名单:9 款无重复且 glass 在首位", len(de.SKIN_IDS) == 9
              and len(set(de.SKIN_IDS)) == 9 and de.SKIN_IDS[0] == "glass",
              str(de.SKIN_IDS))
        check("norm:白名单内原样返回", de._norm_skin("crt") == "crt"
              and de._norm_skin("vaporwave") == "vaporwave")
        check("norm:白名单外/任意类型 → None", de._norm_skin("dark") is None
              and de._norm_skin(123) is None and de._norm_skin([]) is None
              and de._norm_skin("") is None and de._norm_skin(None) is None
              and de._norm_skin(True) is None)
        # ① 三键 cfg(无 skin)→ 原子文件形状仍三键;load 恒含 skin="glass"
        p1 = str(tmp / "c1.json")
        check("save:三键 cfg 返回 True", de.save_config(base, path=p1) is True)
        raw = jsonmod.loads(_Path(p1).read_text(encoding="utf-8"))
        check("save:文件无 skin 键(仍三键)", set(raw) == set(three),
              str(sorted(raw)))
        de.CONFIG_PATH = p1
        back = de.load_config()
        check("load:无键 → skin=glass", back.get("skin") == "glass",
              str(back.get("skin")))
        check("load:其他三键不受污染(可选键除外)",
              all(back.get(k) == v for k, v in three.items()), str(back))
        # ② 合法非缺省值回环:crt 落盘读回环相等;glass 输入=整键省略
        p2 = str(tmp / "c_crt.json")
        check("save:crt 落盘 True",
              de.save_config(dict(base, skin="crt"), path=p2) is True)
        raw = jsonmod.loads(_Path(p2).read_text(encoding="utf-8"))
        check("save:crt 文件含键同值", raw.get("skin") == "crt", str(sorted(raw)))
        de.CONFIG_PATH = p2
        check("load:crt 回环相等", de.load_config().get("skin") == "crt",
              str(de.load_config()))
        p3 = str(tmp / "c_glass.json")
        check("save:glass 输入落盘 True",
              de.save_config(dict(base, skin="glass"), path=p3) is True)
        raw = jsonmod.loads(_Path(p3).read_text(encoding="utf-8"))
        check("save:glass 输入整键省略", "skin" not in raw, str(sorted(raw)))
        # ③ 非法值:save 省键(文件仍三键)、load 遇非法值恒回 glass
        for bad in ("dark", 123, [], ""):
            p = str(tmp / f"bad_{bad!r}.json")
            check(f"save:非法 {bad!r} 仍可落盘",
                  de.save_config(dict(base, skin=bad), path=p) is True)
            raw = jsonmod.loads(_Path(p).read_text(encoding="utf-8"))
            check(f"save:非法 {bad!r} 整键省略", "skin" not in raw,
                  str(sorted(raw)))
        pb = str(tmp / "load_bad.json")
        _Path(pb).write_text(jsonmod.dumps(
            {"quota_api_key": "sk-x", "daily_budget_cny": 5,
             "alert_pct": [20, 10], "skin": "dark"}), encoding="utf-8")
        de.CONFIG_PATH = pb
        back = de.load_config()
        check("load:文件含非法 dark → glass", back.get("skin") == "glass",
              str(back.get("skin")))
        check("load:其他三键不受污染(可选键除外)",
              all(back.get(k) == v for k, v in three.items()), str(back))
        # ④ 缺文件早退路径恒含 skin(默认 dict 携带;键缺失会让『恒含』
        # 塌成 .get 兜底,T2 期 cfg["skin"] 直读就 KeyError)
        de.CONFIG_PATH = str(tmp / "no_such_file.json")
        back = de.load_config()
        check("load:缺文件 → 恒含 skin=glass",
              "skin" in back and back["skin"] == "glass", str(back))
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
    # today 缺省走本地时钟:行日期必须跟真实时钟取(本地今日行)—— 钉死
    # 历史日期(上面的 rows 是 2026-09 固定值)会随日历漂移滚出 7 日窗,
    # 2026-10-04 回归就在这条上炸过(today=10-04 → 窗 09-28..10-04,
    # 全部行窗外 → None)。顺带断言 avg_tokens 手算值,证明行真被缺省
    # 时钟的窗圈进去了(而非碰巧返回非 None);跨午夜竞态无虞:即使
    # 两次取 today 之间日期翻转,行变成『昨天』仍在 7 日窗内,和值不变。
    rows_local = [(dt.date.today().isoformat(), "GLM-5.3",
                   7000, 5600, 1400, 0.10, False)]
    rdef = trend_forecast(rows_local)
    check("趋势:today 缺省走本地时钟可用",
          rdef is not None and rdef["avg_tokens"] == round(8400 / 7, 1),
          str(rdef))


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
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
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
        # 同一调用点;不 start 免线程竞态)。v0.8.0 速度语义改全局吞吐后,
        # recent_speeds = _gtps_hist 逐秒采样(1 次 poll → 1 点;fixture 行
        # completed_at 为旧时刻,重叠=0,part 表无 text 增长 → 0.0)
        e._poll_stats()
        rs = e.snap.recent_speeds
        check("sparkline:全局吞吐采样已接线(_poll_stats,1 点 0.0)",
              isinstance(rs, list) and len(rs) == 1 and abs(rs[0]) < 1e-9
              and abs(e.snap.global_tps) < 1e-9, f"got {rs}")
        # 完成重叠:把一行 completed 挪进最近窗口 → 全局吞吐 = 重叠加权
        con = sqlite3.connect(tdb)
        now_ms = int(time.time() * 1000)
        # r3(1350tok 全表唯一):ft=now−4s c=now−1s → 区间 [−4,−1]s 完整落
        # 在 10s 窗内,gen=3s,r=450 → 贡献 450×3/10=135.0(其余行 completed_at
        # NULL 天然被滤)。T2 起 started_at 一并钉到 now−5s:该行原本继承 mu()
        # 的今日午夜,本地 02:00 后跑本测试它就成了『单请求>2h』行 —— 落进
        # started_at 前置取数的设计边界(B3',被有意排除),135 断言会随一天
        # 中的时刻忽绿忽红;钉成 5s 短请求后 24h 全绿,期望值不变(135 只
        # 依赖 ft/c/out,与 started_at 无关)。
        con.execute("UPDATE model_usage SET started_at=?, first_token_at=?,"
                    " completed_at=? WHERE output_tokens=1350"
                    " AND session_id='sess_main'",
                    (now_ms - 5_000, now_ms - 4_000, now_ms - 1_000))
        con.commit(); con.close()
        e._poll_stats()
        expect_overlap = (1350.0 / 3.0) * 3.0 / de.DataEngine.THROUGHPUT_WINDOW_S
        check("全局吞吐:完成行重叠加权(手算 135)",
              len(e.snap.recent_speeds) == 2
              and abs(e.snap.global_tps - expect_overlap) < 1e-6,
              f"got {e.snap.global_tps}")
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


# ========== T2:_global_completed_tps started_at 前置换行 + 行缓存衰减 ==========

def test_global_completed_tps_started_at_cache():
    """_global_completed_tps 取数/加权拆分(T2)合成 temp 库对账:
    ①手算对账 + 与旧 completed_at-only 形态全等(行集全在 2h 边界内时逐位
    相等,参照实现=旧函数逐字拷贝独立连接执行);
    ②边界:跨 cut 长请求部分重叠计入、completed_at<cut 排除、first_token_at
    NULL 跳过、output=0 跳过、cancelled 跳过(缓存行集逐行可归因);
    ③B3' 设计边界:started_at<cut-2h 且 completed_at>=cut(单请求>2h)的行
    被新形态有意排除 —— 构造该行断言新值零变化,且旧形态参考值恰好多出
    该行的手算贡献(证边界真实存在、排除是设计而非漏算);
    ④衰减等价:冻结 db + 冻结缓存(计数 _connect 断言 0 SQL)下连续 tick,
    缓存+当前 now 重算 == 旧形态每秒重查(T4 闸门关闭期衰减照推的等价性
    依据),衰减序列前 4 拍手算对账;
    ⑤直调兜底:新引擎缓存未灌时首次直调自动取数一次(脱离 _poll_stats
    仍自洽);
    ⑥坏库(缺列)吞错不穿透:refresh 自吞清缓存盖章、直调 0.0 不抛。
    固定合成时钟(与墙钟零耦合);DB_PATH 双 patch 单点,finally 恢复,
    绝不碰真实 ~/.zcode 库。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc

    tmp = _Path(tempfile.mkdtemp(prefix="zm_gct_"))
    orig = zsrc.DB_PATH
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        con.execute("CREATE TABLE part (session_id TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        NOW = 1_800_000_000.0                      # 固定合成时钟
        WIN = de.DataEngine.THROUGHPUT_WINDOW_S    # 10.0,与引擎同窗
        cut = int((NOW - WIN) * 1000)              # 1_799_999_990_000
        LB = de.DataEngine.COMPLETED_LOOKBACK_MS   # 2h,与常量同源不硬编码

        def ins(status, started, ft, c, out):
            con.execute(
                "INSERT INTO model_usage (session_id, query_source, status,"
                " started_at, first_token_at, completed_at, output_tokens)"
                " VALUES ('sess_main','main_turn',?,?,?,?,?)",
                (status, started, ft, c, out))

        # r1 完整落窗:gen=3s r=450 → 450×3/10 = 135.0
        ins("completed", cut + 500, cut + 1_000, cut + 4_000, 1_350)
        # r2 跨 cut 长请求:gen=7s r=1000/7,重叠 = c−cut = 2s → 200/7
        ins("completed", cut - 5_000, cut - 5_000, cut + 2_000, 1_000)
        # r3 completed_at<cut:已滑出窗(计入即错)
        ins("completed", cut - 9_000, cut - 8_000, cut - 1_000, 5_000)
        # r4 first_token_at NULL:跳过(无生成起点,负∞ 区间无意义)
        ins("completed", cut + 2_000, None, cut + 3_000, 800)
        # r5 output=0:跳过(0 token 无贡献)
        ins("completed", cut + 1_000, cut + 1_000, cut + 2_000, 0)
        # r6 cancelled:status 不符
        ins("cancelled", cut + 500, cut + 500, cut + 900, 700)
        con.execute("INSERT INTO part (session_id) VALUES ('sess_main')")
        con.commit(); con.close()

        def old_form(now, window_s):
            # 旧 _global_completed_tps 逐字参照(completed_at-only 谓词 + 同一
            # 加权循环):全等对照与 B3' 差值归因都靠它;独立连接,不掺引擎状态
            cut_ms = int((now - window_s) * 1000)
            rc = sqlite3.connect(tdb)
            rows = rc.execute(
                "SELECT output_tokens, first_token_at, completed_at"
                " FROM model_usage WHERE status='completed' AND completed_at>=?"
                " AND output_tokens>0 AND first_token_at IS NOT NULL",
                (cut_ms,)).fetchall()
            rc.close()
            win = max(window_s, 1e-3)
            now_ms = now * 1000.0
            tps = 0.0
            for out_tok, ft, c in rows:
                gen_s = max((c - ft) / 1000.0, 1e-3)
                ov_ms = min(c, now_ms) - max(ft, cut_ms)
                if ov_ms > 0:
                    tps += (out_tok / gen_s) * (ov_ms / 1000.0) / win
            return tps

        zsrc.DB_PATH = tdb; de.DB_PATH = tdb
        e = de.DataEngine(queue.Queue(maxsize=1))   # 不 start,同步路径已完整
        # #12 起行集带 session_id(4 元组,尾段完成拍去重的原料)
        expect_rows = {(1_350, cut + 1_000, cut + 4_000, "sess_main"),
                       (1_000, cut - 5_000, cut + 2_000, "sess_main")}
        expect = 135.0 + 200.0 / 7.0
        check("T2:新引擎缓存未灌(cut None、行集空)",
              e._completed_rows_cut_ms is None and e._completed_rows == [], "")
        got = e._global_completed_tps(NOW, WIN)     # 首调:兜底取数一次
        check("T2:直调兜底取数一次(值=手算 135+200/7、cut 已盖章)",
              abs(got - expect) < 1e-9 and e._completed_rows_cut_ms == cut,
              f"got {got} cut={e._completed_rows_cut_ms}")
        check("T2:与旧 completed_at-only 形态全等(2h 边界内)",
              abs(got - old_form(NOW, WIN)) < 1e-9,
              f"old {old_form(NOW, WIN)}")
        check("T2:缓存行集恰为 r1/r2(r3-r6 各因由排除)",
              set(e._completed_rows) == expect_rows,
              f"got {sorted(e._completed_rows)}")

        # 衰减等价:冻结 db + 冻结缓存(不再刷新)下连续 tick,缓存+当前
        # now 重算 == 旧形态每秒重查;计数 _connect 证 0 SQL(T4 闸门关闭期
        # 的形态:衰减照推、不发查询)
        e._refresh_completed_rows(cut)      # 以序列最早 cut 预灌(行集偏多为无害超集)
        sql_calls = []
        orig_connect = e._connect

        def _counting_connect():
            sql_calls.append(1)
            return orig_connect()
        e._connect = _counting_connect
        try:
            decay_new = [e._global_completed_tps(NOW + i, WIN) for i in range(13)]
        finally:
            e._connect = orig_connect
        decay_old = [old_form(NOW + i, WIN) for i in range(13)]
        # 手算:i=0 → 135+200/7;i=1 → r2 重叠缩到 1s → 135+100/7;
        # i=2 → r2 出窗 → 90;i=3 → 45;i≥4 → 全出窗 0
        hand_decay = [135.0 + 200.0 / 7.0, 135.0 + 100.0 / 7.0, 90.0, 45.0] \
            + [0.0] * 9
        check("T2:冻结缓存连续 tick 0 SQL 且逐拍==旧形态重查",
              not sql_calls and all(abs(a - b) < 1e-9
                                     for a, b in zip(decay_new, decay_old)),
              f"sql={len(sql_calls)} new={decay_new[:5]} old={decay_old[:5]}")
        check("T2:衰减序列手算对账(135+200/7→135+100/7→90→45→0…)",
              all(abs(a - b) < 1e-9 for a, b in zip(decay_new, hand_decay)),
              f"got {decay_new[:5]}")

        # B3' 设计边界:单请求>2h(started_at<cut-LB 且 completed_at>=cut)
        # 被新形态有意排除;旧形态会计入 —— 新值零变化、旧参考恰好多出该行
        # 的手算贡献(gen=(LB+1)/1000,重叠恰 1s)
        con = sqlite3.connect(tdb)
        con.execute(
            "INSERT INTO model_usage (session_id, query_source, status,"
            " started_at, first_token_at, completed_at, output_tokens)"
            " VALUES ('sess_main','main_turn','completed',?,?,?,?)",
            (cut - LB - 1, cut - LB + 999, cut + 1_000, 10_000))
        con.commit(); con.close()
        e._refresh_completed_rows(cut)      # 显式换行:别让冻结缓存吞掉差异
        check("T2:B3' >2h 行被排除(新值不变)",
              abs(e._global_completed_tps(NOW, WIN) - expect) < 1e-9,
              f"got {e._global_completed_tps(NOW, WIN)}")
        check("T2:B3' 行不在缓存行集",
              set(e._completed_rows) == expect_rows,
              f"got {sorted(e._completed_rows)}")
        b3_gen_s = (LB + 1) / 1000.0        # c−ft = (cut+1000)−(cut−LB+999)
        b3_contrib = (10_000 / b3_gen_s) * (1_000 / 1000.0) / WIN
        check("T2:B3' 旧形态恰计入该行(差值=其手算贡献)",
              abs(old_form(NOW, WIN) - expect - b3_contrib) < 1e-9,
              f"old {old_form(NOW, WIN)} contrib {b3_contrib}")

        # 坏库(缺列):refresh 自吞不穿透、清缓存并盖章;直调 0.0 不抛
        bad = str(tmp / "bad.sqlite")
        bcon = sqlite3.connect(bad)
        bcon.execute("CREATE TABLE model_usage (session_id TEXT)")  # 缺全部取数列
        bcon.execute("CREATE TABLE part (session_id TEXT)")
        bcon.commit(); bcon.close()
        zsrc.DB_PATH = bad; de.DB_PATH = bad
        e._refresh_completed_rows(cut)              # 不得抛
        check("T2:坏库 refresh 自吞清缓存且盖章 cut",
              e._completed_rows == [] and e._completed_rows_cut_ms == cut,
              f"rows={e._completed_rows} cut={e._completed_rows_cut_ms}")
        check("T2:坏库直调 → 0.0",
              e._global_completed_tps(NOW, WIN) == 0.0, "")
        e.stop()
    finally:
        zsrc.DB_PATH = orig; de.DB_PATH = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_global_part_tps_watermark_walk_probe():
    """_global_part_tps 水位走读+rowid IN 定点探针重构(T1)合成 temp 库对账:
    (a)手算:固定字符比下 text 行 in-place 增长两拍后 tps=Δlen/Δt/chars_per_token;
    (b)新 text 行 append 重置基线不计负(更长新行仍按旧语义计正差,新旧一致);
    (c)删顶行致 MAX(rowid) 回退 → _gpart 基线与 _session_last_rowid 双重建
        (B2:注入 ghost 键证两 dict 都是从零重建而非增量修补);
    (d)IN 探针 miss(text 行被删但表顶 rowid 未回落)→ 会话从探针集与基线
        双裁剪;非 text 行只喂 _session_last_rowid 不进探针集(任何类型行
        的 append 语义);
    (e)同一合成窗口场景,新旧实现逐拍数值全等 + 基线 dict/_sid_text_rowid
        全等(参照实现=旧 4000 行窗口 SQL+同款差分逐字拷贝,独立连接自持
        状态,含 subagent 会话不筛的口径);
    (f)B5':超 seen+64 阈值后 _sid_text_rowid 与 _gpart_last 同点同步收缩、
        _session_last_rowid 不收缩(100 会话 → 10 活动裁到 10/10/100);
    B3:坏库(part 无 data 列)水位探测可成功但重建 SQL 失败 → 水位保持
    None 下拍重试、贡献恒 0.0 不穿透;库打不开 → 水位维持原值、贡献 0.0,
    恢复后走读自愈。固定合成时钟(与墙钟零耦合);DB_PATH 双 patch 单点,
    finally 恢复,绝不碰真实 ~/.zcode 库。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc

    tmp = _Path(tempfile.mkdtemp(prefix="zm_gpt_"))
    orig = zsrc.DB_PATH
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        # part 带 data 列:水位/走读/探针/旧窗口 SQL 的全部依赖列
        con.execute("CREATE TABLE part (session_id TEXT, data TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        con.commit(); con.close()

        def mk(n):                       # 长度可预测的 text part 行
            return '{"type":"text","text":"' + "x" * n + '"}'

        def tool():                      # 非 text 行(走读喂 session 缓存,不进探针集)
            return '{"type":"tool_call","name":"bash"}'

        L = lambda n: len(mk(n))         # 期望长度不手数字符
        T0 = 1_800_000_000.0             # 固定合成时钟,与墙钟零耦合

        def part_rows(sid, data):        # 直插合成行(显式拿回 rowid)
            cur = sqlite3.connect(tdb)
            cur.execute("INSERT INTO part (session_id, data) VALUES (?,?)",
                        (sid, data))
            rid = cur.execute("SELECT last_insert_rowid()").fetchone()[0]
            cur.commit(); cur.close()
            return rid

        def part_exec(sql, params=()):
            cur = sqlite3.connect(tdb)
            cur.execute(sql, params)
            cur.commit(); cur.close()

        zsrc.DB_PATH = tdb; de.DB_PATH = tdb
        # ---- (a) 手算对账:in-place 增长两拍后 tps=Δlen/Δt/chars_per_token ----
        part_rows("sess_a", mk(100))                     # rowid 1
        e = de.DataEngine(queue.Queue(maxsize=1))        # 不 start,同步路径已完整
        e._chars_per_token = 3.2                         # 固定字符比(手算口径)
        check("T1:首拍建基线贡献 0.0",
              _gptps(e, T0) == 0.0, "")
        check("T1:首拍重建灌满三态",
              e._part_watermark == 1
              and e._gpart_last == {"sess_a": (L(100), T0)}
              and e._sid_text_rowid == {"sess_a": 1}
              and e._session_last_rowid == {"sess_a": 1},
              f"wm={e._part_watermark} g={e._gpart_last}"
              f" s={e._sid_text_rowid} sl={e._session_last_rowid}")
        # in-place 增长:同 rowid 内追加文本(真实库 213,694/434,238 行如此,
        # 只有定点重读可见 —— 本重构存在的理由)
        part_exec("UPDATE part SET data=? WHERE rowid=1", (mk(132),))
        got = _gptps(e, T0 + 1.0)
        expect_a = (L(132) - L(100)) / 1.0 / 3.2
        check("T1:(a) in-place 增长两拍手算对账",
              abs(got - expect_a) < 1e-9, f"got {got} expect {expect_a}")

        # ---- (b) 新 text 行 append:重置基线不计负;更长新行仍计正差 ----
        part_rows("sess_a", mk(50))                      # rowid 2,更短的新一轮
        check("T1:(b) 新行更短 → 重置基线不计负",
              _gptps(e, T0 + 2.0) == 0.0
              and e._gpart_last["sess_a"][0] == L(50)
              and e._sid_text_rowid["sess_a"] == 2
              and e._session_last_rowid["sess_a"] == 2,
              f"g={e._gpart_last} s={e._sid_text_rowid}")
        part_rows("sess_a", mk(200))                     # rowid 3,更长的新行
        got = _gptps(e, T0 + 3.0)
        expect_b = (L(200) - L(50)) / 1.0 / 3.2
        check("T1:(b) 新行更长 → 按旧语义计正差(手算)",
              abs(got - expect_b) < 1e-9, f"got {got} expect {expect_b}")

        # ---- (c) 删顶行 → MAX(rowid) 回退 → 双基线全量重建 ----
        part_rows("sess_b", mk(80))                      # rowid 4
        _gptps(e, T0 + 3.5)                     # 走读喂入 sess_b
        # 注入 ghost 键:重建必须从零覆盖两 dict,而非增量修补(半截重建会让
        # _session_last_rowid 永久缺会话,走读只覆盖新行再无补救窗口)
        e._gpart_last["ghost"] = (1.0, 0.0)
        e._session_last_rowid["ghost2"] = 999
        part_exec("DELETE FROM part WHERE rowid=4")      # 删顶行,MAX 4→3 回退
        check("T1:(c) 回退拍贡献 0.0(基线刚重置)",
              _gptps(e, T0 + 4.0) == 0.0, "")
        check("T1:(c) 双基线从零重建(ghost 清除、GROUP BY 对账)",
              e._part_watermark == 3
              and "ghost" not in e._gpart_last and "ghost2" not in e._session_last_rowid
              and e._session_last_rowid == {"sess_a": 3}
              and e._gpart_last == {"sess_a": (L(200), T0 + 4.0)}
              and e._sid_text_rowid == {"sess_a": 3},
              f"wm={e._part_watermark} g={e._gpart_last}"
              f" s={e._sid_text_rowid} sl={e._session_last_rowid}")

        # ---- (d) 探针 miss 裁剪 + 非 text 行只喂 session 缓存 ----
        r_tool1 = part_rows("sess_c", tool())            # rowid 4(顶行已删,复用)
        r_text = part_rows("sess_d", mk(60))             # rowid 5
        _gptps(e, T0 + 5.0)
        check("T1:(d) 非 text 行喂 _session_last_rowid 不进探针集",
              e._session_last_rowid["sess_c"] == r_tool1
              and "sess_c" not in e._sid_text_rowid
              and e._sid_text_rowid["sess_d"] == r_text,
              f"sl={e._session_last_rowid} s={e._sid_text_rowid}")
        r_tool2 = part_rows("sess_c", tool())            # rowid 6,顶行抬高
        _gptps(e, T0 + 6.0)
        check("T1:(d) 同会话再写非 text 行仍推进 session 缓存",
              e._session_last_rowid["sess_c"] == r_tool2, "")
        part_exec("DELETE FROM part WHERE rowid=?", (r_text,))   # 删 sess_d 的
        # text 行:表顶(rowid 6)未回落 → 不触发回退重建,只走探针 miss 裁剪
        check("T1:(d) 探针 miss → 会话从探针集与基线双裁剪",
              _gptps(e, T0 + 7.0) == 0.0
              and "sess_d" not in e._sid_text_rowid
              and "sess_d" not in e._gpart_last
              and e._part_watermark == r_tool2,
              f"s={e._sid_text_rowid} wm={e._part_watermark}")
        # 全量缓存的已知边界:行删而表顶未回落时保留旧 rowid(仅影响 T3 菜单
        # 排序;表顶回落的整批删除由 (c) 的回退重建自愈)
        check("T1:(d) _session_last_rowid 全量保留(设计边界)",
              e._session_last_rowid.get("sess_d") == r_text,
              str(e._session_last_rowid))
        e.stop()

        # ---- (e) 新旧全等:同一合成窗口场景逐拍对账 ----
        def legacy_ref(now, gstate, sstate):
            """旧 _global_part_tps(v0.9 T1 前)逐字参照:4000 行窗口 SQL +
            同款差分/裁剪,自持状态独立连接执行。sstate 镜像 _sid_text_rowid
            (窗口 SQL 的 MAX(rowid)),供探针集对照。"""
            rc = sqlite3.connect(tdb)
            rows = rc.execute(
                "SELECT session_id, MAX(rowid), length(data) FROM part"
                " WHERE data LIKE '{\"type\":\"text\"%'"
                " AND rowid > (SELECT MAX(rowid) FROM part) - 4000"
                " GROUP BY session_id").fetchall()
            rc.close()
            tps = 0.0
            seen = set()
            for sid, mx, ln in rows:
                if not sid or not ln:
                    continue
                seen.add(sid)
                prev = gstate.get(sid)
                if prev is not None and ln > prev[0]:
                    dt = max(now - prev[1], 1e-3)
                    tps += (ln - prev[0]) / dt / 3.2
                gstate[sid] = (ln, now)
                sstate[sid] = mx
            if len(gstate) > len(seen) + 64:
                gstate = {k: v for k, v in gstate.items() if k in seen}
                sstate = {k: v for k, v in sstate.items() if k in seen}
            return tps, gstate, sstate

        e2 = de.DataEngine(queue.Queue(maxsize=1))
        e2._chars_per_token = 3.2
        lg, ls = {}, {}                    # 参照实现自持状态
        # 拍 1:双基线(sess_sub1 刻意用 subagent 名 —— 全局吞吐不筛会话口径)
        got = _gptps(e2, 2000.0)
        ref, lg, ls = legacy_ref(2000.0, lg, ls)
        check("T1:(e) 拍1 建基线新旧全等",
              got == 0.0 and ref == 0.0 and e2._gpart_last == lg
              and e2._sid_text_rowid == ls,
              f"got {got} ref {ref}")
        # 拍 2:sess_a in-place 增长 + 新 subagent 会话入窗
        part_exec("UPDATE part SET data=? WHERE rowid=3", (mk(240),))
        r_sub = part_rows("sess_sub1", mk(70))
        got = _gptps(e2, 2000.5)
        ref, lg, ls = legacy_ref(2000.5, lg, ls)
        expect_2 = (L(240) - L(200)) / 0.5 / 3.2
        check("T1:(e) 拍2 in-place 增长新旧全等(含手算)",
              abs(got - ref) < 1e-9 and abs(got - expect_2) < 1e-9
              and e2._gpart_last == lg and e2._sid_text_rowid == ls,
              f"got {got} ref {ref} expect {expect_2}")
        # 拍 3:subagent 会话 in-place 增长(不筛会话口径)+ 新会话入窗
        part_exec("UPDATE part SET data=? WHERE rowid=?", (mk(130), r_sub))
        part_rows("sess_e", mk(90))
        got = _gptps(e2, 2001.0)
        ref, lg, ls = legacy_ref(2001.0, lg, ls)
        expect_3 = (L(130) - L(70)) / 0.5 / 3.2
        check("T1:(e) 拍3 subagent 增长计贡献(新旧全等+手算)",
              abs(got - ref) < 1e-9 and abs(got - expect_3) < 1e-9,
              f"got {got} ref {ref} expect {expect_3}")
        # 拍 4:sess_a 新行更短(行切换重置,双方都 0)
        part_rows("sess_a", mk(10))
        got = _gptps(e2, 2001.5)
        ref, lg, ls = legacy_ref(2001.5, lg, ls)
        check("T1:(e) 拍4 行切换重置新旧全等(双方 0)",
              got == 0.0 and ref == 0.0
              and e2._gpart_last == lg and e2._sid_text_rowid == ls,
              f"got {got} ref {ref}")
        # 拍 5:sess_e 新行更长(旧行基线差分,双方都计正差)
        part_rows("sess_e", mk(300))
        got = _gptps(e2, 2002.0)
        ref, lg, ls = legacy_ref(2002.0, lg, ls)
        expect_5 = (L(300) - L(90)) / 0.5 / 3.2
        check("T1:(e) 拍5 更长新行计正差新旧全等(含手算)",
              abs(got - ref) < 1e-9 and abs(got - expect_5) < 1e-9,
              f"got {got} ref {ref} expect {expect_5}")
        # walk 的 append 语义对账:引擎侧 _session_last_rowid == 全量 GROUP BY
        gcon = sqlite3.connect(tdb)
        expect_slr = dict(gcon.execute(
            "SELECT session_id, MAX(rowid) FROM part GROUP BY session_id"
        ).fetchall())
        gcon.close()
        check("T1:(e) _session_last_rowid == 全量 GROUP BY(append 语义)",
              e2._session_last_rowid == expect_slr,
              f"got {e2._session_last_rowid} expect {expect_slr}")

        # ---- B3:库打不开 → 水位维持、贡献 0.0;恢复后走读自愈 ----
        wm_before = e2._part_watermark
        zsrc.DB_PATH = str(tmp / "nope.sqlite"); de.DB_PATH = zsrc.DB_PATH
        check("T1:B3 库打不开 → 0.0 且水位维持",
              _gptps(e2, 2010.0) == 0.0
              and e2._part_watermark == wm_before,
              f"wm={e2._part_watermark}")
        zsrc.DB_PATH = tdb; de.DB_PATH = tdb
        r_z = part_rows("sess_z", mk(40))
        check("T1:B3 恢复后走读自愈(新会话重新入表)",
              _gptps(e2, 2011.0) == 0.0
              and e2._sid_text_rowid.get("sess_z") == r_z
              and e2._part_watermark == r_z,
              f"s={e2._sid_text_rowid} wm={e2._part_watermark}")
        e2.stop()

        # ---- (f) B5':超 seen+64 → 两 dict 同点同步收缩,session 缓存不缩 ----
        fdb = str(tmp / "f.sqlite")
        fc = sqlite3.connect(fdb)
        fc.execute("CREATE TABLE part (session_id TEXT, data TEXT)")
        fc.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        for i in range(1, 101):          # 100 会话各 1 text 行,全在窗口内
            fc.execute("INSERT INTO part (session_id, data) VALUES (?,?)",
                       (f"sess_f{i:03d}", mk(100)))
        fc.commit(); fc.close()
        zsrc.DB_PATH = fdb; de.DB_PATH = fdb
        ef = de.DataEngine(queue.Queue(maxsize=1))
        ef._chars_per_token = 3.2
        _gptps(ef, 3000.0)      # 建基线:首拍不裁剪(100 全跟踪)
        check("T1:(f) 建基线拍不裁剪(100 会话全跟踪)",
              len(ef._gpart_last) == 100 and len(ef._sid_text_rowid) == 100
              and len(ef._session_last_rowid) == 100,
              f"{len(ef._gpart_last)}/{len(ef._sid_text_rowid)}"
              f"/{len(ef._session_last_rowid)}")
        fcur = sqlite3.connect(fdb)
        for i in range(1, 11):           # 10 会话 in-place 增长 = 唯一活动集
            fcur.execute("UPDATE part SET data=? WHERE session_id=?",
                         (mk(150), f"sess_f{i:03d}"))
        fcur.commit(); fcur.close()
        got = _gptps(ef, 3001.0)
        grown = {f"sess_f{i:03d}" for i in range(1, 11)}
        expect_f = 10 * (L(150) - L(100)) / 1.0 / 3.2
        check("T1:(f) 裁剪拍活动会话贡献照计(手算)",
              abs(got - expect_f) < 1e-9, f"got {got} expect {expect_f}")
        check("T1:(f) _sid_text_rowid 与 _gpart_last 同点同步收缩到 seen",
              set(ef._sid_text_rowid) == grown and set(ef._gpart_last) == grown,
              f"s={sorted(ef._sid_text_rowid)[:3]}…{len(ef._sid_text_rowid)}"
              f" g={len(ef._gpart_last)}")
        check("T1:(f) _session_last_rowid 全量不收缩",
              len(ef._session_last_rowid) == 100, str(len(ef._session_last_rowid)))
        ef.stop()

        # ---- B3:part 无 data 列(回归 fixture 形态)→ 水位探测可成功但重建
        # SQL 失败 → 水位保持 None 下拍重试、贡献恒 0.0,绝不穿透 ----
        bad = str(tmp / "bad.sqlite")
        bc = sqlite3.connect(bad)
        bc.execute("CREATE TABLE part (session_id TEXT)")
        bc.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        bc.execute("INSERT INTO part (session_id) VALUES ('sess_main')")
        bc.commit(); bc.close()
        zsrc.DB_PATH = bad; de.DB_PATH = bad
        e3 = de.DataEngine(queue.Queue(maxsize=1))
        check("T1:B3 无 data 列 → 贡献 0.0 且水位不推进(重试语义)",
              _gptps(e3, 4000.0) == 0.0
              and _gptps(e3, 4001.0) == 0.0
              and e3._part_watermark is None,
              f"wm={e3._part_watermark}")
        e3.stop()
    finally:
        zsrc.DB_PATH = orig; de.DB_PATH = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_claude_watcher_injected():
    """Claude watcher 线程(T6)注入式单测 —— 不依赖 watchdog 安装、不发真实
    FS 事件:合成 ZCode 库 + 合成 Claude projects 目录 + sys.modules 注入假
    watchdog,绝不碰真实 ~/.zcode 与 ~/.claude:
    ①handler 只入队:.jsonl 之外(目录/别的扩展/缺 src_path)全丢;
    ②pump 分发:防抖窗合并 + 同路径去重 → note_changes + _wake 置位
      (_wake 是 T4 的 seam:测试自带事件,getattr 消费路径一并覆盖 ——
      T4 落地前后本测试都绿);
    ③watchdog import 失败(sys.modules 注入 None)→ dbg 优雅降级不抛;
    ④循环体抛异常 → watcher 线程退出 + dbg 落盘;_db_loop 存活(采样仍
      推进)且 TTL 路径仍出数(15s SCAN_TTL 兜底 =『死亡自愈=回退 TTL』);
    ⑤projects 目录不存在 → 直接 return(不装监听);
    ⑥守卫环境:run() 不启动 watcher 线程;直接调循环也被内部守卫拦下。
    """
    import datetime as dtmod
    import json as jsonmod
    import os as osmod
    import shutil
    import tempfile
    import threading as thmod
    import types as typesmod
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.data_engine import ClaudeSource
    from zcode_meter.sources import zcode as zsrc

    tmp = _Path(tempfile.mkdtemp(prefix="zm_watch_"))
    orig_db = zsrc.DB_PATH
    orig_persist, orig_dbgpath, orig_debug = de._no_persist, de.DBG_PATH, de.DEBUG
    wd_mods = ("watchdog", "watchdog.observers", "watchdog.events")
    saved_mods = {k: sys.modules.get(k) for k in wd_mods}
    engines = []
    try:
        # ---- 合成 ZCode 库:引擎构造/轮询全程不碰真实 ~/.zcode ----
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        con.execute("CREATE TABLE part (session_id TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        con.execute("INSERT INTO part (session_id) VALUES ('sess_main')")
        con.commit(); con.close()
        zsrc.DB_PATH = tdb; de.DB_PATH = tdb

        # ---- 合成 Claude projects 目录:1 条今日 assistant 行(in=100
        # out=40 → today=140);jpath2 只当队列里的路径字符串用,不落盘 ----
        proj_root = osmod.path.join(str(tmp), "claude")
        proj = osmod.path.join(proj_root, "p1")
        osmod.makedirs(proj)
        t0 = dtmod.datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0)
        iso = ((t0 + dtmod.timedelta(minutes=30)).astimezone(dtmod.timezone.utc)
               .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
        jpath = osmod.path.join(proj, "s.jsonl")
        jpath2 = osmod.path.join(proj, "later.jsonl")
        with open(jpath, "w", encoding="utf-8") as f:
            f.write(jsonmod.dumps({"type": "assistant", "timestamp": iso,
                                   "message": {"id": "msg_w1", "usage": {
                                       "input_tokens": 100,
                                       "cache_read_input_tokens": 0,
                                       "cache_creation_input_tokens": 0,
                                       "output_tokens": 40}}}) + "\n")
        src = ClaudeSource(projects_dir=proj_root)
        expect_today = 140

        # dbg 断言要真实落盘:DEBUG 开 + DBG_PATH 指向临时文件
        de.DEBUG = True
        de.DBG_PATH = str(tmp / "dbg.log")
        de._no_persist = lambda: False     # 进 watcher(守卫语义在 ⑥ 单独断言)

        e = de.DataEngine(queue.Queue(maxsize=1))   # 先不 start,直接驱动内部路径
        engines.append(e)
        e.sources = [src]                 # 引擎只见合成源,不碰真实 ~/.claude
        e._wake = thmod.Event()           # T4 的唤醒事件 seam(落地后 __init__ 自带)

        # ① handler 只入队:.jsonl 之外全丢(目录事件/别的扩展/缺 src_path)
        e._claude_watch_offer(jpath)
        e._claude_watch_offer(jpath2)
        e._claude_watch_offer(osmod.path.join(proj, "note.txt"))
        e._claude_watch_offer(proj)                    # 目录事件
        e._claude_watch_offer(None)                    # 缺 src_path 的怪事件
        check("watcher:offer 只入队 .jsonl(2 条)",
              e._claude_watch_q.qsize() == 2,
              f"qsize={e._claude_watch_q.qsize()}")

        # ② pump:防抖窗内合并成一批 → note_changes + _wake 置位
        seen = []
        src.note_changes = lambda paths: seen.append(list(paths))
        ok = e._claude_watch_pump()
        check("watcher:pump 分发防抖合并批",
              ok is True and seen == [[jpath, jpath2]], f"ok={ok} seen={seen}")
        check("watcher:分发后 _wake 置位", e._wake.is_set(), "")
        e._claude_watch_offer(jpath)       # watchdog 一次 append 常连发多条
        e._claude_watch_offer(jpath)
        e._wake.clear()
        e._claude_watch_pump()
        check("watcher:同路径去重(一次 append 多事件)",
              seen[-1] == [jpath], f"seen={seen[-1]}")
        check("watcher:再次分发仍置位 _wake", e._wake.is_set(), "")
        e._wake.clear()
        ok2 = e._claude_watch_pump()       # 队列空:空转
        check("watcher:空队列空转 False 且不置位",
              ok2 is False and not e._wake.is_set(), f"ok={ok2}")

        # ③ watchdog import 失败(None 注入 → ImportError)→ dbg 降级不抛
        for k in wd_mods:
            sys.modules[k] = None
        raised = None
        try:
            e._claude_watch_loop()          # 直接调:守卫已放行,必须安静返回
        except Exception as exc:
            raised = exc
        check("watcher:import 失败优雅降级不抛", raised is None, repr(raised))
        with open(de.DBG_PATH, encoding="utf-8") as f:
            dbg_txt = f.read()
        check("watcher:import 失败 dbg 声明回退 15s TTL",
              "claude watcher unavailable, fallback to 15s TTL" in dbg_txt, "")

        # ④ 循环体抛异常 → 线程退出 + dbg;_db_loop 存活、TTL 路径仍出数。
        #    假 watchdog:Observer=哑对象(不发真实 FS 事件),handler 基类=object
        scheduled = []
        obs_mod = typesmod.ModuleType("watchdog.observers")

        class _StubObserver:
            def schedule(self, handler, path, recursive=False):
                scheduled.append((path, recursive))
                return "watch"
            def start(self): pass
            def stop(self): pass
            def join(self, timeout=None): pass
        obs_mod.Observer = _StubObserver
        ev_mod = typesmod.ModuleType("watchdog.events")
        ev_mod.FileSystemEventHandler = object
        wd_mod = typesmod.ModuleType("watchdog")
        for k, v in (("watchdog", wd_mod), ("watchdog.observers", obs_mod),
                     ("watchdog.events", ev_mod)):
            sys.modules[k] = v

        def boom(paths):
            raise ValueError("note_changes boom")
        src.note_changes = boom
        e.start()                          # 守卫已放行 → run() 启动 watcher 线程
        deadline = time.time() + 2.0
        while not scheduled and time.time() < deadline:
            time.sleep(0.05)
        check("watcher:observer 递归挂载 projects 目录",
              scheduled == [(proj_root, True)], str(scheduled))
        e._claude_watch_offer(jpath)       # 合成事件 → pump → note_changes 抛
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not any(t.name == "zm-claude-watch" and t.is_alive()
                       for t in thmod.enumerate()):
                break
            time.sleep(0.05)
        check("watcher:循环体异常 → 线程退出(不拖死引擎)",
              not any(t.name == "zm-claude-watch" and t.is_alive()
                      for t in thmod.enumerate()), "")
        with open(de.DBG_PATH, encoding="utf-8") as f:
            dbg_txt = f.read()
        check("watcher:异常 dbg 落盘(回退 15s TTL)",
              "claude watcher died" in dbg_txt, "")
        # 死后 _db_loop 照跑:1s tick 的全局吞吐采样持续推进 + TTL 出数。
        # _scanned_at=0 模拟 TTL 到期:watcher 已死,只有 walk+stat 兜底路径
        n_at_death = len(e.snap.recent_speeds or [])
        src._scanned_at = 0.0
        time.sleep(2.2)
        n_after = len(e.snap.recent_speeds or [])
        check("watcher:死亡后 TTL 路径仍出数(TTL 到期重扫)",
              src.today_usage() == expect_today, f"got {src.today_usage()}")
        srcs = dict(e.snap.today_by_source or [])
        check("watcher:死亡后 _db_loop 存活(采样推进)且出 Claude 分量",
              n_after > n_at_death and srcs.get("Claude") == expect_today,
              f"hist {n_at_death}->{n_after} srcs={srcs}")
        e.stop()

        # ⑤ projects 目录不存在 → 直接 return(不装监听:哑 observer 不多挂)
        n_sched = len(scheduled)
        src_dead = ClaudeSource(projects_dir=osmod.path.join(str(tmp), "nope"))
        src_dead.note_changes = lambda paths: None   # 排除 note_changes 缺位干扰
        e2 = de.DataEngine(queue.Queue(maxsize=1))
        engines.append(e2)
        e2.sources = [src_dead]
        t2 = thmod.Thread(target=e2._claude_watch_loop, daemon=True)
        t2.start(); t2.join(2.0)
        check("watcher:无 projects 目录 → 直接 return(线程退出)",
              not t2.is_alive() and len(scheduled) == n_sched,
              f"alive={t2.is_alive()} sched={len(scheduled)}")

        # ⑥ 守卫环境:run() 不启动 watcher;直接调循环被内部守卫拦下
        de._no_persist = lambda: True
        e3 = de.DataEngine(queue.Queue(maxsize=1))
        engines.append(e3)
        e3.sources = [src]
        e3._wake = thmod.Event()
        e3.start(); time.sleep(0.8); e3.stop()
        check("watcher:守卫环境 run() 不启动 watcher 线程",
              not any(t.name == "zm-claude-watch" for t in thmod.enumerate()), "")
        t3 = thmod.Thread(target=e3._claude_watch_loop, daemon=True)
        t3.start(); t3.join(2.0)
        check("watcher:守卫环境直接调循环 → 内部守卫拦下",
              not t3.is_alive(), "")
    finally:
        for eng in engines:
            try:
                eng.stop()
            except Exception:
                pass
        de._no_persist = orig_persist
        de.DBG_PATH = orig_dbgpath
        de.DEBUG = orig_debug
        zsrc.DB_PATH = orig_db; de.DB_PATH = orig_db
        for k, v in saved_mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


# ========== T3:recent_sessions 缓存读(引擎维护 sid→rowid,菜单暖读 ≤1ms) ==========

def test_recent_sessions_cache_read():
    """recent_sessions 缓存读(T3)合成 temp 库对账:
    ①新旧实现输出全等(顺序+[:16]):混 subagent/dwf(含最新行,验顶部
    过滤)、NULL 标题、缺 session 行(验 LEFT JOIN 空标题语义)、>16 字符
    标题截断;参照实现=改前 recent_sessions 逐字拷贝独立连接执行(不调
    _recent_sessions_sql 防自证);limit 8/2/100/0/-1 逐个对账 + 手算期望
    表(截断字面硬编码);
    ②冷缓存回退:未 start 引擎首调走旧 SQL 恰一次(计数补丁)且回填后
    第二拍起零回退;dict 全量含 subagent/dwf(过滤在读侧,与 T1 走读
    『任何新行都喂』同不变式);
    ③T1 供数:真实 _global_part_tps 走读新行喂点 → 菜单暖读即得新会话
    居首;run() 序幕(stop_flag 预置后同步走完,三子线程起步即退)经
    _poll_stats 首拍重建 → 菜单未 open 过也暖;
    ④删行裁剪:DELETE FROM part 后经 T1 回退重建(MAX(rowid) 回落)菜单
    不再列出;部分幸存重建后恰列幸存会话;
    ⑤LIMIT 语义镜像:None/不可绑定 → [](旧路径 execute 即抛 sqlite3.Error
    被吞的同判)、数值字符串 '3' 与 SQLite 同强转、负 int=不设上限;
    ⑥N3 锁序:菜单读/冷回填/T1 走读喂点三路径 snap_lock acquire==0、
    _session_lock≥3(探针显式实现 __enter__ —— 特殊方法经 type 查找,
    实例 __getattr__ 兜不住 with 语句);
    ⑦库缺失:回退失败 → [](与改前同判),缓存仍冷不炸、重复调用同判。
    DB_PATH 双 patch 单点(test_fetch_total_usage 同款),finally 恢复,
    绝不碰真实 ~/.zcode 库。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc

    tmp = _Path(tempfile.mkdtemp(prefix="zm_rsc_"))
    orig = zsrc.DB_PATH
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        # part 带 data 列:T1 走读/重建 SQL 需要(值全 NULL 即可,length/
        # substr 对 NULL 安全);model_usage 全列:run() 序幕子测试要完整
        # 跑一轮 _poll_stats(缺列会让整段 try 吞错早退,走不到
        # _global_part_tps 首拍重建);session 最小两列(菜单只碰 id/title)
        con.execute("CREATE TABLE part (session_id TEXT, data TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT)")
        # rowid 1..8 按插入序:两行 sess_old(验 MAX(rowid) 分组)、被排除
        # 的 subagent/dwf(含最新行 8,验顶部过滤)、NULL 标题(sess_mid)、
        # 缺 session 行(sess_ghost,验批查缺行→空标题)、17 字符标题(验
        # [:16] 截断)
        con.executemany(
            "INSERT INTO part (session_id, data) VALUES (?,?)",
            [("sess_old", None), ("sess_old", None), ("sess_subagent-1", None),
             ("sess_mid", None), ("sess_dwf-dwfrun-9", None),
             ("sess_ghost", None), ("sess_main", None),
             ("sess_subagent-2", None)])
        con.executemany(
            "INSERT INTO session (id, title) VALUES (?,?)",
            [("sess_old", "这是一个超过十六个字符的老会话标题"),
             ("sess_mid", None), ("sess_main", "当前会话")])
        con.commit(); con.close()

        def oracle(k):
            # 改前 recent_sessions 逐字参照(独立连接):新旧全等的 oracle,
            # 刻意不调引擎的 _recent_sessions_sql(那是被测代码的一部分)
            c = sqlite3.connect(tdb)
            rows = c.execute(
                "SELECT p.session_id, s.title FROM"
                " (SELECT session_id, MAX(rowid) AS mr FROM part"
                "  WHERE session_id NOT LIKE 'sess_subagent%'"
                "    AND session_id NOT LIKE 'sess_dwf-%' GROUP BY session_id) p"
                " LEFT JOIN session s ON s.id = p.session_id"
                " ORDER BY p.mr DESC LIMIT ?", (k,)).fetchall()
            c.close()
            return [(sid, (title or "").strip()[:16]) for sid, title in rows]

        # 手算期望表:mr 倒序 = main(7)/ghost(6)/mid(4)/old(2);ghost 缺
        # session 行与 mid NULL 标题都列空标题;old 标题 17 字符截 16
        expect_hand = [("sess_main", "当前会话"), ("sess_ghost", ""),
                       ("sess_mid", ""),
                       ("sess_old", "这是一个超过十六个字符的老会话标")]

        zsrc.DB_PATH = tdb; de.DB_PATH = tdb
        e = de.DataEngine(queue.Queue(maxsize=1))     # 不 start:冷缓存路径

        # ---- ① 冷回退恰一次 + ② 回填后零回退;① 全等 + 手算期望 ----
        calls = []
        orig_sql = e._recent_sessions_sql

        def _spy(k=8):
            calls.append(k)
            return orig_sql(k)

        e._recent_sessions_sql = _spy
        cold = e.recent_sessions(8)                   # 冷:回退恰一次
        check("T3:冷缓存首调走旧 SQL 回退恰一次", calls == [8], str(calls))
        warm = e.recent_sessions(8)                   # 暖:零回退
        check("T3:回填后第二拍起零回退(纯缓存读)", calls == [8], str(calls))
        check("T3:冷/暖两拍结果全等", cold == warm, f"{cold} vs {warm}")
        for k in (2, 100, 0, -1):
            got = e.recent_sessions(k)
            check(f"T3:暖读 limit={k} 与旧 SQL 全等(0=空/负=不设上限,同 SQLite)",
                  got == oracle(k), f"got {got}")
        check("T3:全程回退仅冷首拍一次", calls == [8], str(calls))
        e._recent_sessions_sql = orig_sql
        check("T3:冷回退=旧 SQL 结果(全等)", cold == oracle(8), f"got {cold}")
        check("T3:顺序+过滤+空标题+[:16] 截断=手算期望表",
              cold == expect_hand, f"got {cold}")
        check("T3:回填后 dict 全量含 subagent/dwf(过滤在读侧)",
              set(e._session_last_rowid) ==
              {"sess_old", "sess_subagent-1", "sess_mid", "sess_dwf-dwfrun-9",
               "sess_ghost", "sess_main", "sess_subagent-2"},
              str(sorted(e._session_last_rowid)))

        # ---- ③ T1 走读喂点:首拍建水位,插新行后走读批量喂入 ----
        _gptps(e, 1_800_000.0)     # 首拍:水位 None → 全量重建,水位=8
        con = sqlite3.connect(tdb)
        con.execute("INSERT INTO part (session_id, data)"
                    " VALUES ('sess_new', NULL)")
        con.execute("INSERT INTO session (id, title)"
                    " VALUES ('sess_new', '新会话')")
        con.commit(); con.close()
        _gptps(e, 1_800_001.0)     # 走读 rowid 9 → 锁内批量 update
        got_new = e.recent_sessions(8)
        check("T3:T1 走读喂点 → 菜单暖读即得新会话居首",
              got_new == oracle(8) and got_new[0] == ("sess_new", "新会话"),
              f"got {got_new[:2]}")

        # ---- ③' run() 序幕基线接线:stop_flag 预置后同步走完序幕,首拍
        # 重建让菜单从未 open 过也暖(与『run() 起步 GROUP BY』条款的落地
        # 形态:重建由 T1 首拍承担,早于三子线程启动)----
        e2 = de.DataEngine(queue.Queue(maxsize=1))
        e2.stop_flag.set()
        e2.run()   # 序幕同步:_session_title/_poll_stats(内含首拍重建)/
        # _init_tps/_push;三子线程与 watcher 见 stop_flag 即退
        calls2 = []
        orig_sql2 = e2._recent_sessions_sql

        def _spy2(k=8):
            calls2.append(k)
            return orig_sql2(k)

        e2._recent_sessions_sql = _spy2
        got2 = e2.recent_sessions(8)
        e2._recent_sessions_sql = orig_sql2
        check("T3:run() 序幕基线 → 菜单首读即暖(零回退)且全等",
              got2 == oracle(8) and calls2 == []
              and bool(e2._session_last_rowid),
              f"got {got2[:2]} calls={calls2}")
        e2.stop()

        # ---- ④ 删行裁剪(依赖 T1 回退重建):全删 → MAX(rowid) 回落 →
        # 全量重建清 dict → 菜单冷回退亦空(旧 SQL 同判)----
        con = sqlite3.connect(tdb)
        con.execute("DELETE FROM part")
        con.commit(); con.close()
        _gptps(e, 1_800_002.0)     # mx=0 < 水位 9 → 回退重建 → dict={}
        check("T3:会话行全删+T1 回退重建 → 菜单不再列出",
              e._session_last_rowid == {} and e.recent_sessions(8) == []
              and oracle(8) == [],
              f"sl={e._session_last_rowid}")
        con = sqlite3.connect(tdb)          # 部分幸存:只回 sess_old 一行
        con.execute("INSERT INTO part (session_id, data)"
                    " VALUES ('sess_old', NULL)")
        con.commit(); con.close()
        _gptps(e, 1_800_003.0)     # 走读新行 → dict={'sess_old':…}
        got_part = e.recent_sessions(8)
        check("T3:部分幸存会话重建后恰列一个(截断不变)",
              got_part == oracle(8)
              and got_part == [("sess_old", "这是一个超过十六个字符的老会话标")],
              f"got {got_part}")

        # ---- ⑤ LIMIT 语义镜像(暖路径,dict={'sess_old':…})----
        check("T3:limit=None → [](旧路径 LIMIT NULL 即 sqlite3.Error 同判)",
              e.recent_sessions(None) == [], "")
        check("T3:limit='3' 数值字符串与 SQLite 同强转",
              e.recent_sessions("3") == oracle(3),
              f"got {e.recent_sessions('3')}")
        check("T3:limit 不可绑定 → [](旧路径 ProgrammingError 同判)",
              e.recent_sessions("abc") == [] and e.recent_sessions({}) == [],
              "")

        # ---- ⑥ N3 锁序:菜单读/冷回填/T1 走读喂点三路径零 snap_lock、
        # _session_lock 每路径至少一次(此处再插一行让走读真发生)----
        con = sqlite3.connect(tdb)
        con.execute("INSERT INTO part (session_id, data)"
                    " VALUES ('sess_late', NULL)")
        con.commit(); con.close()

        class _LockProbe:
            """锁计数探针:with 语句与显式 acquire 都计入 —— 特殊方法经
            type 查找,实例 __getattr__ 兜不住 with,必须显式实现 __enter__。"""

            def __init__(self, inner):
                self.inner = inner
                self.acquires = 0

            def acquire(self, *a, **k):
                self.acquires += 1
                return self.inner.acquire(*a, **k)

            def release(self):
                return self.inner.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *exc):
                self.release()
                return False

        snap_probe = _LockProbe(e.snap_lock)
        sess_probe = _LockProbe(e._session_lock)
        e.snap_lock = snap_probe
        e._session_lock = sess_probe
        try:
            e.recent_sessions(8)                    # 读侧快照:1 次 _session_lock
            e._rebuild_session_last_rowid()         # 冷回填:1 次
            _gptps(e, 1_800_004.0)         # 走读 sess_late:锁内喂点 1 次
            check("T3:N3 菜单/回填/走读三路径 snap_lock acquire==0",
                  snap_probe.acquires == 0, str(snap_probe.acquires))
            check("T3:N3 三路径各取 _session_lock(≥3)",
                  sess_probe.acquires >= 3, str(sess_probe.acquires))
        finally:
            e.snap_lock = snap_probe.inner
            e._session_lock = sess_probe.inner
        e.stop()

        # ---- ⑦ 库缺失:回退失败 → [](改前同判),缓存仍冷不炸 ----
        zsrc.DB_PATH = str(tmp / "nope.sqlite"); de.DB_PATH = zsrc.DB_PATH
        e4 = de.DataEngine(queue.Queue(maxsize=1))
        check("T3:库缺失 → [](改前同判)且缓存仍冷",
              e4.recent_sessions(8) == [] and e4.recent_sessions(8) == []
              and e4._session_last_rowid == {}, "")
        e4.stop()
    finally:
        zsrc.DB_PATH = orig; de.DB_PATH = orig
        shutil.rmtree(tmp, ignore_errors=True)


# ========== T4:ZCode db 文件闸门 + today0 时间闸 + _wake 事件唤醒 + 变化即 push ==========

def test_t4_db_gate_time_gate_and_wake():
    """T4 闸门三件套合成 temp 库验收(①-⑥⑧直接驱动 _db_tick = _db_loop
    循环体,5 拍 = 5s 节拍的确定性等价;真实线程路径由 ⑦ 单独覆盖):
    ①空闲 0-SQL:基线拍后冻结 db + 时钟钉死(de.today0_ms 固定值,时间闸
    静默)连续 5 拍闸门管辖 SQL 计数==0 —— monkeypatch _connect 计数,B4'
    清单口径含 _check_activity(on_activity 已接线,未跳过必发水位 SQL)与
    _poll_new_completed;且 N6 计数代理包 eng.snap_lock 断言 acquire==0
    (尾段无锁推进,空闲 0 持锁);
    ②衰减采样仍推进:_gtps_hist 随 tick 增长(基线+5=6 点)、值按 T2 缓存
    逐拍严格衰减(吞吐窗内完成行,关门拍不重查、ov 随 cut 前移收缩);
    ③Claude 分量每 tick 现调(假源计数),ZCode 分量关门拍取缓存不现调,
    today_by_source 两源均在列、ZCode 派生字段(today_tokens)保持开闸值;
    ④-wal 不存在不炸:合成库 DELETE journal 全程无 -wal(stat OSError →
    视为维持原状 N2),闸门照常关/开;
    ⑤文件 append(内容变)与 touch(仅 mtime 变)后下一拍 SQL 均恢复;
    append 拍 quota 活动信号照发(_check_activity 未被闸门饿死);
    ⑥值未变不 push:吞吐窗空(完成分量恒 0.0)的引擎上,touch 开闸拍全
    字段与上拍相同 → 不 push;关门拍永不 push;
    ⑦_wake 置位 → 真实 _db_loop 线程 ≤POLL_DB 内执行一轮且 push(started
    引擎;_no_persist 钉 True:T6 watcher 不启动,免真实 ~/.claude FS 事件
    串场;启动 push 落定后的静默窗口本身就是空闲无 push 的验证);
    ⑧跨午夜时间闸(B1'):假时钟 today0 前跳一天 → 强制开闸恰一次,
    today_tokens 随新天界清零、ZCode 源分量缓存随之刷新、易变字段有变
    → push,之后恢复关门(再一拍 SQL==0)。
    DB_PATH 双 patch 单点(既有同款),finally 恢复,绝不碰真实 ~/.zcode 库。"""
    import os
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc
    from zcode_meter.sources.base import UsageSource

    tmp = _Path(tempfile.mkdtemp(prefix="zm_t4gate_"))
    orig = zsrc.DB_PATH
    orig_today0 = de.today0_ms
    orig_nopersist = de._no_persist
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        # part 无 data 列:与既有 sparkline fixture 同款 —— _global_part_tps
        # 的重建/走读/探针按 B3 各自吞 sqlite3.Error,part 分量恒 0.0,本测试
        # 专注闸门与完成分量衰减,不被 part 侧行为牵扯
        con.execute("CREATE TABLE part (session_id TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        t0 = de.today0_ms()
        now_ms = int(time.time() * 1000)
        # 唯一 completed 行:今日且横跨 10s 吞吐窗沿(ft<cut<c)—— 关门拍
        # 衰减可见:ov=c−cut 随 cut 前移严格收缩(gen=19s r=1350/19)。
        # started_at 必须钉在 now 附近而非 t0(午夜):T2 取数是 started_at
        # 前置形态(B3' 设计边界 = 单请求>2h 的行被有意排除),上午跑测试时
        # t0 起点的行会整个被排除、完成分量恒 0;max(t0,…) 兜住午夜后
        # ~21s 内跑测试的极端时刻(仍保『今日』口径)
        s1 = max(t0 + 1000, now_ms - 21_000)
        con.execute(
            "INSERT INTO model_usage (session_id, query_source, status,"
            " started_at, model_id, provider_id, input_tokens,"
            " cache_read_input_tokens, output_tokens, duration_ms,"
            " time_to_first_token_ms, first_token_at, completed_at)"
            " VALUES ('sess_main','main_turn','completed',?,"
            " 'GLM-5.3','bigmodel',1000,0,1350,19000,1000,?,?)",
            (s1, now_ms - 20_000, now_ms - 1_000))
        con.execute("INSERT INTO part (session_id) VALUES ('sess_main')")
        con.commit(); con.close()
        check("T4④:合成库无 -wal 文件(DELETE journal,stat OSError 路径)",
              not os.path.exists(tdb + "-wal"))
        zsrc.DB_PATH = tdb; de.DB_PATH = tdb
        # 时钟钉死:today0_ms 恒定 → 时间闸全程静默(⑧再改值触发);真实
        # 午夜瞬间跑本测试也不会误开闸(固定值永不变化)
        de.today0_ms = lambda: t0

        class _GateZCode(zsrc.ZCodeSource):
            """计数假 ZCode 源:isinstance 保留(尾段按 ZCodeSource 判缓存/
            现调),today_usage 不发 SQL、返回可变值 —— 只验证调用面与缓存
            语义;真源的 SQL 骑同一闸门(开拍刷新/关拍取缓存)。"""
            def __init__(self):
                self.calls = 0
                self.val = 111

            def is_available(self):
                return True

            def today_usage(self):
                self.calls += 1
                return self.val

        class _GateClaude(UsageSource):
            """计数假非 DB 源:闸门不管非 DB 分量,每 tick 都该被现调。"""
            name = "FakeClaude"

            def __init__(self):
                self.calls = 0

            def is_available(self):
                return True

            def today_usage(self):
                self.calls += 1
                return 7

        class _LockProbe:
            """snap_lock 计数代理(N6):with 与显式 acquire 都计入;特殊
            方法经 type 查找,必须显式实现 __enter__(T3 同款)。"""

            def __init__(self, inner):
                self.inner = inner
                self.acquires = 0

            def acquire(self, *a, **k):
                self.acquires += 1
                return self.inner.acquire(*a, **k)

            def release(self):
                return self.inner.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *exc):
                self.release()
                return False

        fz, fc = _GateZCode(), _GateClaude()
        e = de.DataEngine(queue.Queue(maxsize=50))
        e.sources = [fz, fc]
        sql = []
        orig_connect = e._connect

        def _counting_connect():
            sql.append(1)
            return orig_connect()
        e._connect = _counting_connect
        act = []
        e.on_activity = lambda: act.append(1)   # 接线:_check_activity 不再短路

        # —— 基线拍:无基线必开闸(SQL 已跑、源缓存已灌、首拍 push)——
        e._db_tick(False)
        # #20:ZCode 源分量不再现调源(改前同 tick 同 WHERE 双跑),直接取
        # 本拍 today SQL 的值(2350=1000+1350)双用 —— fz.calls 恒 0
        check("T4①:首拍无基线必开闸(SQL 已跑+基线已立+源缓存已灌)",
              len(sql) > 0 and e._gate_ready and fz.calls == 0
              and e._src_today_cache.get("ZCode") == 2350,
              f"sql={len(sql)} fz={fz.calls} cache={e._src_today_cache}")
        check("T4①:首拍 push(fp 无基线必不同)", e.out.qsize() == 1,
              str(e.out.qsize()))
        v0 = e.snap.global_tps
        expect0 = (1350.0 / 19.0) * 9.0 / de.DataEngine.THROUGHPUT_WINDOW_S
        # 上界精确(ov=c−cut≤9s,构造延迟只会缩小 ov)、下界放宽(50.0 兜
        # 住构造耗时抖动):>50 证明行确在窗内被取到 —— started_at 钉 now 的
        # 注释就是防它落进 B3' 2h 边界被排除(初版曾因此恒 0.0)
        check("T4②:完成分量手算量级(ft=−20s c=−1s,ov≈9s)",
              50.0 < v0 <= expect0 + 0.1, f"got {v0} expect≈{expect0}")
        today_base = e.snap.today_tokens

        # —— ①②③④ 空闲 5 拍:冻结 db + 时钟钉死 → 0 SQL / 0 持锁 / 衰减照推 ——
        n_base = len(sql)
        probe = _LockProbe(e.snap_lock)
        e.snap_lock = probe
        try:
            for _ in range(5):
                # 拍间隔先行(sleep 在 tick 前):基线拍→首拍也隔 ≥20ms ——
                # 衰减侧 cut_ms 是 int 毫秒,同毫秒两拍会得到完全相等的值,
                # 严格递减断言就被同毫秒抖动吞掉(实测踩过);20ms 保证每对
                # 相邻拍必跨毫秒,5 拍合计 ~0.1s 测试开销可忽略
                time.sleep(0.02)
                e._db_tick(False)
        finally:
            e.snap_lock = probe.inner
        hist = list(e._gtps_hist)
        check("T4①:空闲 5 拍闸门管辖 SQL==0(B4' 清单口径)",
              len(sql) == n_base, f"sql={len(sql)}")
        check("T4①:空闲 5 拍 snap_lock acquire==0(N6 尾段无锁)",
              probe.acquires == 0, f"acquires={probe.acquires}")
        check("T4②:_gtps_hist 随 tick 增长(基线+5=6 点)",
              len(hist) == 6, f"len={len(hist)}")
        check("T4②:值按 T2 缓存衰减(逐拍严格递减)",
              all(a > b for a, b in zip(hist, hist[1:])),
              f"hist={[round(v, 3) for v in hist]}")
        check("T4③:Claude 分量每 tick 现调(6 拍 6 调)",
              fc.calls == 6, f"calls={fc.calls}")
        check("T4③:ZCode 分量全程零现调(#20 单查询双用)",
              fz.calls == 0, f"calls={fz.calls}")
        check("T4③:today_by_source 两源在列(ZCode=today SQL 缓存值)",
              dict(e.snap.today_by_source) == {"ZCode": 2350, "FakeClaude": 7},
              str(e.snap.today_by_source))
        check("T4③:ZCode 派生字段保持开闸值(today_tokens 不动)",
              e.snap.today_tokens == today_base,
              f"{e.snap.today_tokens} vs {today_base}")
        # #76 修订:关门拍不再『永不 push』—— 本 fixture 完成行在 10s 窗内,
        # 尾段每拍衰减重算 global_tps(②已断言逐拍严格递减),fp 逐拍有变
        # → 逐拍送达(基线+5=6);『值未变不 push』由下方 tdb2 fixture 验证
        check("T4⑥(#76):关门拍衰减照推(基线+5=6,逐拍送达)",
              e.out.qsize() == 6, f"qsize={e.out.qsize()}")

        # —— ⑤ 文件 append(内容变)→ 下一拍 SQL 恢复 + 活动信号 + push ——
        n2 = len(sql)
        con = sqlite3.connect(tdb)
        con.execute(
            "INSERT INTO model_usage (session_id, query_source, status,"
            " started_at, model_id, provider_id, input_tokens,"
            " cache_read_input_tokens, output_tokens, duration_ms,"
            " time_to_first_token_ms, first_token_at, completed_at)"
            " VALUES ('sess_main','main_turn','completed',?,"
            " 'GLM-5.3','bigmodel',100,0,500,9000,1000,?,?)",
            (max(t0 + 2000, now_ms - 19_000), now_ms - 18_000, now_ms - 2_000))
        con.commit(); con.close()
        e._db_tick(False)
        check("T4⑤:append 后下一拍 SQL 恢复(闸门开)",
              len(sql) > n2, f"sql={len(sql)}")
        check("T4⑤:ZCode 源分量随开闸刷新(today SQL 值同步,#20)",
              fz.calls == 0 and e._src_today_cache.get("ZCode") == today_base + 600,
              f"calls={fz.calls} cache={e._src_today_cache}")
        check("T4⑤:quota 活动信号照发(_check_activity 未被闸门饿死)",
              len(act) == 1, f"act={len(act)}")
        check("T4⑤:易变字段有变(today_tokens +600)→ push",
              e.out.qsize() == 7 and e.snap.today_tokens == today_base + 600,
              f"qsize={e.out.qsize()} today={e.snap.today_tokens}")

        # —— ⑥ touch(仅 mtime 变,行集不变)→ 开闸但值未必变:本引擎完成
        # 分量在窗内会衰减(值有变必 push),『值未变不 push』用独立库 ⑥ 补 ——
        n3 = len(sql)
        os.utime(tdb, None)
        e._db_tick(False)
        check("T4⑤:touch(仅 mtime 变)后下一拍 SQL 恢复",
              len(sql) > n3, f"sql={len(sql)}")
        e.stop()

        # —— ⑥⑧ 独立库:完成行滑出吞吐窗(global_tps 恒 0.0,fp 只会因
        # 真实字段变化而变)—— 值未变不 push + 跨午夜时间闸 ——
        tdb2 = str(tmp / "t2.sqlite")
        con = sqlite3.connect(tdb2)
        con.execute("CREATE TABLE part (session_id TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        # 今日行但 completed_at=−60s(滑出 10s 窗):today_tokens 有值、
        # 完成分量恒 0.0 —— touch 开闸拍全字段不变 → 不 push 的干净形态
        # (started_at 同样钉 now 附近,见上:B3' 2h 前置边界)
        con.execute(
            "INSERT INTO model_usage (session_id, query_source, status,"
            " started_at, model_id, provider_id, input_tokens,"
            " cache_read_input_tokens, output_tokens, duration_ms,"
            " time_to_first_token_ms, first_token_at, completed_at)"
            " VALUES ('sess_main','main_turn','completed',?,"
            " 'GLM-5.3','bigmodel',1000,0,1350,19000,1000,?,?)",
            (max(t0 + 1000, now_ms - 80_000), now_ms - 79_000, now_ms - 60_000))
        con.execute("INSERT INTO part (session_id) VALUES ('sess_main')")
        con.commit(); con.close()
        zsrc.DB_PATH = tdb2; de.DB_PATH = tdb2
        fz2, fc2 = _GateZCode(), _GateClaude()
        e2 = de.DataEngine(queue.Queue(maxsize=50))
        e2.sources = [fz2, fc2]
        sql2 = []
        orig_connect2 = e2._connect

        def _counting_connect2():
            sql2.append(1)
            return orig_connect2()
        e2._connect = _counting_connect2
        e2._db_tick(False)                       # 基线拍:开闸 + 首拍 push
        check("T4⑥:基线拍 push(fp 无基线)", e2.out.qsize() == 1,
              str(e2.out.qsize()))
        today2 = e2.snap.today_tokens
        check("T4⑥:基线拍今日有值(2350)", today2 == 2350, str(today2))
        n4 = len(sql2)
        os.utime(tdb2, None)                     # 仅 mtime 变,内容不变
        e2._db_tick(False)
        check("T4⑥:touch 后 SQL 恢复(指纹含 mtime)",
              len(sql2) > n4, f"sql={len(sql2)}")
        check("T4⑥:全字段与上拍相同 → 不 push(值未变不 push)",
              e2.out.qsize() == 1 and e2.snap.today_tokens == today2,
              f"qsize={e2.out.qsize()}")
        n5 = len(sql2)
        e2._db_tick(False)
        # #76:fp 恒定(global_tps 衰减已归稳为 0)→ 关门拍 0 SQL 亦 0 push
        # ——『值未变不 push』在新语义下的归宿:衰减期逐拍推、归稳后自然停推
        check("T4⑥:衰减归稳后 fp 恒定 → 关门拍 0 SQL 亦 0 push",
              len(sql2) == n5 and e2.out.qsize() == 1,
              f"sql={len(sql2)} qsize={e2.out.qsize()}")
        # ⑧ 跨午夜:假时钟 today0 前跳一天 → 时间闸强制开闸一次
        # (#20 后 ZCode 分量随 today SQL 值走:新天界下 SQL 归 0 → 缓存 0)
        de.today0_ms = lambda: t0 + 86_400_000
        n6 = len(sql2)
        e2._db_tick(False)
        check("T4⑧:时间闸强制开闸一次(SQL 恢复)",
              len(sql2) > n6, f"sql={len(sql2)}")
        check("T4⑧:today_tokens 随新天界清零",
              e2.snap.today_tokens == 0, str(e2.snap.today_tokens))
        check("T4⑧:ZCode 源分量缓存随之刷新(today SQL 清零双用)",
              e2._src_today_cache.get("ZCode") == 0
              and dict(e2.snap.today_by_source)["ZCode"] == 0,
              f"cache={e2._src_today_cache} tbs={e2.snap.today_by_source}")
        check("T4⑧:易变字段有变(今日清零)→ push",
              e2.out.qsize() == 2, f"qsize={e2.out.qsize()}")
        n7 = len(sql2)
        e2._db_tick(False)
        check("T4⑧:时间闸消费后恢复关门(强制开闸恰一次)",
              len(sql2) == n7, f"sql={len(sql2)}")
        e2.stop()

        # —— ⑦ _wake:真实 _db_loop 线程,置位 → ≤POLL_DB 内执行一轮且 push ——
        de._no_persist = lambda: True            # T6 watcher 不启动,免真实
        # ~/.claude FS 事件串场(注入式;run_all 的 ZM_NO_STATE=1 下本就 True)
        try:
            fz3, fc3 = _GateZCode(), _GateClaude()
            e3 = de.DataEngine(queue.Queue(maxsize=50))
            e3.sources = [fz3, fc3]
            e3.start()
            # 静置 1.4s:启动 push(run 初值/state 首变/首拍开闸)全部落定,
            # 随后是关门拍(冻结 db)—— 空闲期无 push 本身就是要验证的语义
            time.sleep(1.4)
            while not e3.out.empty():
                e3.out.get_nowait()
            e3._wake.set()                       # T6 watcher 的提前出数信号
            t_set = time.time()
            got = None
            while time.time() - t_set < 2.0:
                if e3.out.qsize() > 0:
                    got = time.time() - t_set
                    break
                time.sleep(0.02)
            check("T4⑦:_wake 置位 → ≤POLL_DB 内执行一轮且 push",
                  got is not None
                  and got <= de.DataEngine.POLL_DB + 0.5,
                  f"elapsed={got}")
            e3.stop()
            e3.join(timeout=2.0)
        finally:
            de._no_persist = orig_nopersist
    finally:
        zsrc.DB_PATH = orig; de.DB_PATH = orig
        de.today0_ms = orig_today0
        de._no_persist = orig_nopersist
        shutil.rmtree(tmp, ignore_errors=True)




# ===================== v0.9.2 T1 治理批:逐项新单测 =====================

def test_load_config_early_exit_bar_segments():
    """#9:load_config 三条早退路径(缺文件/坏 JSON/非 dict)返回的 dict 恒含
    bar_segments(规范化全开形态)与 skin —— 『恒含』的完整承诺,此前早退
    dict 只有 skin 修过同款缺口,bar_segments 漏了。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    tmp = _Path(tempfile.mkdtemp(prefix="zm_bs_"))
    orig_path = de.CONFIG_PATH
    full = {"h": list(de.BAR_SEGMENTS_H), "v": list(de.BAR_SEGMENTS_V)}
    try:
        # ① 缺文件
        de.CONFIG_PATH = str(tmp / "no_such_file.json")
        back = de.load_config()
        check("load:缺文件 → 恒含 bar_segments 全开",
              back.get("bar_segments") == full, str(back.get("bar_segments")))
        check("load:缺文件 → 恒含 skin=glass(既有承诺不回归)",
              back.get("skin") == "glass", str(back.get("skin")))
        # ② 坏 JSON
        bad = tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        de.CONFIG_PATH = str(bad)
        back = de.load_config()
        check("load:坏 JSON → 恒含 bar_segments 全开",
              back.get("bar_segments") == full, str(back.get("bar_segments")))
        # ③ 非 dict
        nd = tmp / "arr.json"
        nd.write_text("[1,2,3]", encoding="utf-8")
        de.CONFIG_PATH = str(nd)
        back = de.load_config()
        check("load:非 dict → 恒含 bar_segments 全开",
              back.get("bar_segments") == full, str(back.get("bar_segments")))
    finally:
        de.CONFIG_PATH = orig_path
        shutil.rmtree(tmp, ignore_errors=True)


def test_handle_log_line_guards():
    """#10:_handle_log_line 对 json.loads 结果的 isinstance(dict) 守卫 ——
    一行合法 JSON 但非对象(数组/数字/字符串/裸值)曾 AttributeError 打死
    _tail_loop(外层只捕 OSError);对象行驱动状态机不受影响。"""
    e = DataEngine(queue.Queue(maxsize=1))     # 不 start:直接驱动解析路径
    for bad in ('[1,2,3]', '42', '"txt"', 'true', 'null'):
        e._handle_log_line(bad + "\n")        # 改前首行即 AttributeError
    check("非对象 JSON 行静默丢弃不炸(#10)", True)
    e._handle_log_line('{"event":"model.request.started",'
                       '"sessionId":"sess_g1"}\n')
    check("对象行仍驱动状态机", e._running is True)
    e._handle_log_line('{"event":"model.request.completed",'
                       '"sessionId":"sess_g1"}\n')
    check("completed 事件复位", e._running is False)
    e.stop()


def test_on_request_done_main_turn_scope():
    """#11:_on_request_done 会话查询补 query_source='main_turn'(8 处会话
    口径查询的最后一处漏网):同会话内更晚落库的 workflow_child 行不再覆盖
    tps_exact/last_ttft/last_duration(合成库手算)。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc
    tmp = _Path(tempfile.mkdtemp(prefix="zm_ord_"))
    orig = zsrc.DB_PATH
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        con.execute("CREATE TABLE part (session_id TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        t0 = de.today0_ms()
        # r1:main_turn 完成行 —— 期望命中:900/((10000-1000)/1000)=100.0
        con.execute(
            "INSERT INTO model_usage (session_id, query_source, status,"
            " started_at, model_id, provider_id, input_tokens,"
            " cache_read_input_tokens, output_tokens, duration_ms,"
            " time_to_first_token_ms) VALUES"
            " ('sess_main','main_turn','completed',?,'GLM-5.3','bigmodel',"
            "  1000,0,900,10000,1000)", (t0,))
        # r2:同会话更晚 rowid 的 workflow_child 行 —— 改前 ORDER BY rowid
        # DESC LIMIT 1 命中它(9000/9=1000.0),污染三字段与 chars/token 校准
        con.execute(
            "INSERT INTO model_usage (session_id, query_source, status,"
            " started_at, model_id, provider_id, input_tokens,"
            " cache_read_input_tokens, output_tokens, duration_ms,"
            " time_to_first_token_ms) VALUES"
            " ('sess_main','workflow_child','completed',?,'GLM-5.3','bigmodel',"
            "  9000,0,9000,9000,0)", (t0 + 1000,))
        con.execute("INSERT INTO part (session_id) VALUES ('sess_main')")
        con.commit(); con.close()
        zsrc.DB_PATH = tdb; de.DB_PATH = tdb
        e = de.DataEngine(queue.Queue(maxsize=1))
        check("引擎锚定 sess_main", e.session_id == "sess_main", e.session_id)
        e._prev_max_rowid = 0
        e._last_len = None
        e._on_request_done()
        check("校准命中 main_turn 行(tps=100.0 手算,#11)",
              e.snap.tps_exact is not None
              and abs(e.snap.tps_exact - 100.0) < 1e-9,
              f"got {e.snap.tps_exact}")
        check("last_ttft=main_turn 行 1.0s",
              abs(e.snap.last_ttft - 1.0) < 1e-9, str(e.snap.last_ttft))
        check("last_duration=main_turn 行 10.0s",
              abs(e.snap.last_duration - 10.0) < 1e-9, str(e.snap.last_duration))
        e.stop()
    finally:
        zsrc.DB_PATH = orig; de.DB_PATH = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_global_tps_completion_tick_dedup():
    """#12 完成拍同拍去重(默认裁决):完成的瞬间同一批输出曾同时以 part
    尾部增长与 completed 重叠加权两路计入(≈2× 尖峰)。扣减语义:对
    completed_at≥now−2×POLL_DB 的新鲜完成行,从 part 总贡献扣去该会话本拍
    贡献(钳 0);会话不匹配不扣、非新鲜不扣、扣不减负;完成分量权重公式
    r×ov/win 一字不动(手算 90.0)。另验 per-session 贡献 dict 的生产侧。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc

    # ---- 消费侧:直接装填行集驱动尾段(值全部手算,ov=c−ft 与时钟零耦合)----
    e = de.DataEngine(queue.Queue(maxsize=1))   # 不 start,不触库(仅装填缓存)
    now = time.time()
    # 行:gen=2s r=450,ov=c−ft=2s → 完成分量 = 450×2/10 = 90.0(恒定,
    # min(c,now)>c 且 max(ft,cut)<ft,now 的微秒级漂移不影响)
    e._completed_rows = [(900, (now - 3.0) * 1000, (now - 1.0) * 1000,
                          "sess_main")]
    e._completed_rows_cut_ms = int((now - 10) * 1000)
    # ① 会话匹配 + 新鲜(c=now−1 ≥ now−2):31.25 全额扣去 → 90.0 不再 2×
    e._poll_stats_tail(31.25, {"sess_main": 31.25})
    check("#12 匹配新鲜行:part 贡献被扣(90.0,不再 121.25)",
          abs(e.snap.global_tps - 90.0) < 1e-6, f"got {e.snap.global_tps}")
    # ② 会话不匹配:不扣 → 90+31.25
    e._poll_stats_tail(31.25, {"sess_other": 31.25})
    check("#12 他会话 part 贡献不扣(90+31.25)",
          abs(e.snap.global_tps - 121.25) < 1e-6, f"got {e.snap.global_tps}")
    # ③ 非新鲜行(c=now−6 < now−2):不扣。手算:gen=c−ft=1s → r=900,
    # ov=1s → 完成分量 900×1/10=90;若误扣则得 90,不扣=121.25
    e._completed_rows = [(900, (now - 7.0) * 1000, (now - 6.0) * 1000,
                          "sess_main")]
    e._poll_stats_tail(31.25, {"sess_main": 31.25})
    check("#12 非新鲜行不扣(90+31.25=121.25,误扣则 90)",
          abs(e.snap.global_tps - 121.25) < 1e-6, f"got {e.snap.global_tps}")
    # ④ 钳 0:part 总量 10 < 扣减 31.25 → 0,不得为负
    e._completed_rows = [(900, (now - 3.0) * 1000, (now - 1.0) * 1000,
                          "sess_main")]
    e._poll_stats_tail(10.0, {"sess_main": 31.25})
    check("#12 扣减钳 0(不为负,90.0)",
          abs(e.snap.global_tps - 90.0) < 1e-6, f"got {e.snap.global_tps}")
    e.stop()

    # ---- 生产侧:_global_part_tps 的 per-session dict 与总量同源同值 ----
    tmp = _Path(tempfile.mkdtemp(prefix="zm_ded_"))
    orig = zsrc.DB_PATH
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        con.execute("CREATE TABLE part (session_id TEXT, data TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        con.execute("INSERT INTO part (session_id, data) VALUES (?,?)",
                    ("sess_a", '{"type":"text","text":"' + "x" * 100 + '"}'))
        con.commit(); con.close()
        zsrc.DB_PATH = tdb; de.DB_PATH = tdb
        e2 = de.DataEngine(queue.Queue(maxsize=1))
        e2._chars_per_token = 3.2
        T0 = 1_800_000.0
        _tps, by_sid = e2._global_part_tps(T0)      # 建基线
        check("#12 生产侧:基线拍 dict 为空", by_sid == {}, str(by_sid))
        cur = sqlite3.connect(tdb)
        cur.execute("UPDATE part SET data=?",
                    ('{"type":"text","text":"' + "x" * 150 + '"}',))
        cur.commit(); cur.close()
        tps2, by_sid2 = e2._global_part_tps(T0 + 1.0)
        L = lambda n: len('{"type":"text","text":"' + "x" * n + '"}')
        expect = (L(150) - L(100)) / 1.0 / 3.2
        check("#12 生产侧:dict 与总量同源同值(手算)",
              abs(tps2 - expect) < 1e-9 and list(by_sid2) == ["sess_a"]
              and abs(by_sid2["sess_a"] - expect) < 1e-9,
              f"tps={tps2} by_sid={by_sid2}")
        e2.stop()
    finally:
        zsrc.DB_PATH = orig; de.DB_PATH = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_switch_session_resets_exact_chars():
    """#14:_switch_session 重置 _last_len 时同步 _exact_out_chars=0 —— 只重
    _last_len 会让旧会话字符数残留,切换后首个快速请求用旧字符数÷新会话
    token 数污染 chars/token 校准。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc
    tmp = _Path(tempfile.mkdtemp(prefix="zm_exc_"))
    orig = zsrc.DB_PATH
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        con.execute("CREATE TABLE part (session_id TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        con.executemany("INSERT INTO part (session_id) VALUES (?)",
                        [("sess_main",), ("sess_other",)])
        con.commit(); con.close()
        zsrc.DB_PATH = tdb; de.DB_PATH = tdb
        e = de.DataEngine(queue.Queue(maxsize=1))
        e._exact_out_chars = 4321          # 模拟旧会话遗留的流式字符数
        e._switch_session("sess_other")
        check("#14 切换同步重置 _exact_out_chars", e._exact_out_chars == 0,
              str(e._exact_out_chars))
        check("#14 既有 _last_len 重置不回归", e._last_len is None, "")
        e.stop()
    finally:
        zsrc.DB_PATH = orig; de.DB_PATH = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_source_guard_exception():
    """#17 源守卫:注入抛 NotImplementedError 的源(非 DB 源 today_usage 抛、
    ZCode 族 is_available 抛)—— 引擎 tick 不死、该源当拍跳过(行消失/缓存
    维持旧值)、dbg 每源仅首记(镜像 QuotaMonitor 节流)。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc
    from zcode_meter.sources.base import UsageSource

    tmp = _Path(tempfile.mkdtemp(prefix="zm_guard_"))
    orig_db, orig_dbg, orig_debug = zsrc.DB_PATH, de.DBG_PATH, de.DEBUG
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        con.execute("CREATE TABLE part (session_id TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        con.execute("INSERT INTO part (session_id) VALUES ('sess_main')")
        con.commit(); con.close()
        zsrc.DB_PATH = tdb; de.DB_PATH = tdb
        de.DEBUG = True
        de.DBG_PATH = str(tmp / "dbg.log")

        class _BoomZ(zsrc.ZCodeSource):
            name = "BoomZ"
            def is_available(self):
                raise NotImplementedError("boom-avail")
            def today_usage(self):
                raise NotImplementedError("boom-today")

        class _BoomO(UsageSource):
            name = "BoomO"
            def is_available(self):
                return True
            def today_usage(self):
                raise NotImplementedError("boom-today")

        e = de.DataEngine(queue.Queue(maxsize=1))
        e.sources = [_BoomZ(), _BoomO()]
        e._src_today_cache = {"BoomZ": 42}     # 预置缓存:失败侧维持旧值
        e._db_tick(False)                      # 开闸拍:gated+tail 双守卫
        e._db_tick(False)                      # 关门拍:tail 守卫再次触发
        check("#17 引擎 tick 存活(采样推进 2 拍)", len(e._gtps_hist) == 2,
              str(len(e._gtps_hist)))
        check("#17 坏源当拍跳过(today_by_source 无行)",
              dict(e.snap.today_by_source) == {}, str(e.snap.today_by_source))
        check("#17 失败 ZCode 源维持缓存旧值",
              e._src_today_cache.get("BoomZ") == 42, str(e._src_today_cache))
        with open(de.DBG_PATH, encoding="utf-8") as f:
            dbg_txt = f.read()
        check("#17 dbg 每源仅首记(BoomZ ×1)",
              dbg_txt.count("source guard: 'BoomZ'") == 1, dbg_txt[-300:])
        check("#17 dbg 每源仅首记(BoomO ×1)",
              dbg_txt.count("source guard: 'BoomO'") == 1, dbg_txt[-300:])
        e.stop()
    finally:
        zsrc.DB_PATH = orig_db; de.DB_PATH = orig_db
        de.DBG_PATH = orig_dbg
        de.DEBUG = orig_debug
        shutil.rmtree(tmp, ignore_errors=True)


def test_db_path_patch_contract():
    """#41/#62 单点 patch 契约:唯一定义在 sources/zcode —— monkeypatch
    zsrc.DB_PATH 后 ZCodeSource.is_available/today_usage/connect_ro 全部跟随;
    de.DB_PATH 是 import 时刻的值拷贝,不跟随(引擎侧查询走 connect_ro 的
    调用时属性查找,与 de.DB_PATH 名字无关)。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc
    tmp = _Path(tempfile.mkdtemp(prefix="zm_patch_"))
    orig = zsrc.DB_PATH
    try:
        zsrc.DB_PATH = str(tmp / "no_dir" / "nope.sqlite")
        z = zsrc.ZCodeSource()
        check("契约:zsrc patch → is_available 跟随(False)",
              z.is_available() is False, "")
        check("契约:zsrc patch → today_usage 跟随(0)", z.today_usage() == 0, "")
        raised = False
        try:
            zsrc.connect_ro()
        except sqlite3.OperationalError:
            raised = True
        check("契约:zsrc patch → connect_ro 跟随(OperationalError)",
              raised, "connect_ro 未跟随 patch")
        check("契约:de.DB_PATH 为 import 时刻快照(不跟随)",
              de.DB_PATH == orig, f"{de.DB_PATH} vs {orig}")
        # 换合成库:源跟随读到值(往返自证 patch 确实驱动源方法)
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        con.execute(
            "CREATE TABLE model_usage (started_at INTEGER, model_id TEXT,"
            " status TEXT, query_source TEXT, input_tokens INTEGER,"
            " cache_read_input_tokens INTEGER, output_tokens INTEGER)")
        con.execute(
            "INSERT INTO model_usage VALUES (?,?,?,?,?,?,?)",
            (de.today0_ms(), "GLM-5.3", "completed", "main_turn", 70, 0, 30))
        con.commit(); con.close()
        zsrc.DB_PATH = tdb
        check("契约:zsrc patch → today_usage 读到合成值 100",
              zsrc.ZCodeSource().today_usage() == 100, "")
    finally:
        zsrc.DB_PATH = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_prices_hot_reload():
    """#48:zm_prices.json (mtime_ns,size) 签名变化 → 重载 prices 且强制开闸
    一拍(db 未变也重算):今日金额随新价落地,空闲期改价不再等重启/等
    db 变化。合成库+合成价格文件,不碰真实 zm_prices.json。"""
    import json as jsonmod
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc
    tmp = _Path(tempfile.mkdtemp(prefix="zm_pri_"))
    orig_db, orig_prices = zsrc.DB_PATH, de.PRICES_PATH
    sql = []
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        con.execute("CREATE TABLE part (session_id TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        con.execute(
            "INSERT INTO model_usage (session_id, query_source, status,"
            " started_at, model_id, provider_id, input_tokens,"
            " cache_read_input_tokens, output_tokens)"
            " VALUES ('sess_main','main_turn','completed',?,"
            " 'GLM-5.3','bigmodel',1000000,0,1000000)",
            (de.today0_ms(),))
        con.execute("INSERT INTO part (session_id) VALUES ('sess_main')")
        con.commit(); con.close()
        zsrc.DB_PATH = tdb; de.DB_PATH = tdb

        pf = tmp / "p.json"
        pf.write_text(jsonmod.dumps(
            {"GLM-5.3": {"in": 1.0, "in_cache": 1.0, "out": 1.0}}),
            encoding="utf-8")
        de.PRICES_PATH = str(pf)
        e = de.DataEngine(queue.Queue(maxsize=1))
        orig_connect = e._connect

        def _counting():
            sql.append(1)
            return orig_connect()
        e._connect = _counting
        e._db_tick(False)                       # 基线拍:¥(1M+1M)/1M=2.0
        check("#48 基线拍金额=文件价手算 2.0",
              abs(e.snap.today_cost_cny - 2.0) < 1e-6,
              str(e.snap.today_cost_cny))
        n_base = len(sql)
        # 改价(in=4 档)且显式推 mtime,保证签名变化
        pf.write_text(jsonmod.dumps(
            {"GLM-5.3": {"in": 4.0, "in_cache": 4.0, "out": 4.0}}),
            encoding="utf-8")
        st = pf.stat()
        import os as osmod
        osmod.utime(pf, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        e._db_tick(False)                       # db 冻结,但价格闸强制开闸
        check("#48 价格签名变 → prices 重载", e.prices["GLM-5.3"]["in"] == 4.0,
              str(e.prices["GLM-5.3"]))
        check("#48 db 冻结仍强制开闸(SQL 恢复)", len(sql) > n_base,
              f"sql={len(sql)} base={n_base}")
        check("#48 金额随新价重算 8.0",
              abs(e.snap.today_cost_cny - 8.0) < 1e-6,
              str(e.snap.today_cost_cny))
        n2 = len(sql)
        e._db_tick(False)                       # 签名消费后恢复关门
        check("#48 价格闸消费后恢复关门", len(sql) == n2, f"sql={len(sql)}")
        e.stop()
    finally:
        zsrc.DB_PATH = orig_db; de.DB_PATH = orig_db
        de.PRICES_PATH = orig_prices
        shutil.rmtree(tmp, ignore_errors=True)


def test_fetch_daily_model_usage_raise_on_error():
    """#64:fetch_daily_model_usage 的 raise_on_error 参数 —— 缺省 False 吞错
    返回 [](引擎侧既有语义);True 时 sqlite3.Error 原样上抛(『导出 CSV』
    消费方区分 DB 故障与真空窗)。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc
    tmp = _Path(tempfile.mkdtemp(prefix="zm_reo_"))
    orig = zsrc.DB_PATH
    try:
        bad = str(tmp / "bad.sqlite")
        con = sqlite3.connect(bad)
        con.execute("CREATE TABLE part (session_id TEXT)")  # 无 model_usage 表
        con.commit(); con.close()
        zsrc.DB_PATH = bad; de.DB_PATH = bad
        e = de.DataEngine(queue.Queue(maxsize=1))
        check("#64 缺省吞错返回 []", e.fetch_daily_model_usage(30) == [], "")
        raised = False
        try:
            e.fetch_daily_model_usage(30, raise_on_error=True)
        except sqlite3.Error:
            raised = True
        check("#64 raise_on_error=True 上抛 sqlite3.Error", raised, "")
        e.stop()
    finally:
        zsrc.DB_PATH = orig; de.DB_PATH = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_plan_remaining_pct_deprecated_compat():
    """#75:plan_remaining_pct 按 S1 裁决保留(仅构造兼容),引擎不写不读 ——
    字段可构造传参(既有测试构造零改动),_poll_stats 全轮后仍为缺省 None。"""
    from zcode_meter import data_engine as de
    s = de.Snapshot(plan_remaining_pct=87.0)
    check("#75 字段保留:构造传参可用", s.plan_remaining_pct == 87.0, "")
    e = DataEngine(queue.Queue(maxsize=1))
    e._poll_stats()
    check("#75 引擎不写(全轮后恒 None)", e.snap.plan_remaining_pct is None, "")
    e.stop()


def test_push_snapshot_copy():
    """#28:_push 推浅拷贝(dataclasses.replace)而非活引用 —— UI 侧对收到的
    快照改回写不再影响引擎快照(改前跨线程共享黑板,单帧可读到撕裂组合);
    列表字段整体重赋值纪律下浅拷即安全。"""
    e = DataEngine(queue.Queue(maxsize=5))
    e.snap.today_tokens = 123
    e._push()
    s = e.out.get_nowait()
    check("#28 推入的是拷贝(非活引用)", s is not e.snap, "")
    s.today_tokens = 99999                     # UI 改回写
    check("#28 UI 改回写不影响引擎快照", e.snap.today_tokens == 123,
          str(e.snap.today_tokens))
    e.snap.today_by_source = [("ZCode", 1)]
    e._push()
    s2 = e.out.get_nowait()
    e.snap.today_by_source = [("ZCode", 2)]    # 引擎整体重赋值(既有纪律)
    check("#28 列表字段拷贝后引擎重赋值互不干扰",
          s2.today_by_source == [("ZCode", 1)], str(s2.today_by_source))
    e.stop()


def test_boot_queries_and_ensure():
    """#84:boot_queries=False 跳过构造期三查询(零连接);run() 序幕的
    _ensure_boot_state 补齐且幂等;缺省 True(GUI 路径)行为不变。"""
    import shutil
    import tempfile
    import threading as thmod
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc
    tmp = _Path(tempfile.mkdtemp(prefix="zm_boot_"))
    orig = zsrc.DB_PATH
    calls = []
    orig_connect = de.connect_ro

    def _counting():
        calls.append(thmod.current_thread().name)
        return orig_connect()
    de.connect_ro = _counting
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        con.execute("CREATE TABLE part (session_id TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        con.execute("INSERT INTO part (session_id) VALUES ('sess_main')")
        con.commit(); con.close()
        zsrc.DB_PATH = tdb; de.DB_PATH = tdb
        e0 = de.DataEngine(queue.Queue(maxsize=1), boot_queries=False)
        check("#84 boot_queries=False 构造零连接", calls == [], str(calls))
        check("#84 三值缺省占位",
              e0.session_id == "" and e0._prev_max_rowid == 0
              and e0._act_rowid == 0, "")
        e0._ensure_boot_state()
        e0._ensure_boot_state()                 # 幂等:第二次零查询
        check("#84 ensure 补齐恰三连接且幂等", len(calls) == 3, str(calls))
        check("#84 补齐后三值就绪", e0.session_id == "sess_main"
              and e0._boot_state_ready, e0.session_id)
        e0.stop()
        calls.clear()
        e1 = de.DataEngine(queue.Queue(maxsize=1))
        check("#84 缺省构造 3 连接(GUI 行为不变)", len(calls) == 3, str(calls))
        n = len(calls)
        e1._ensure_boot_state()
        check("#84 已就绪后 ensure 零查询", len(calls) == n, "")
        e1.stop()
    finally:
        de.connect_ro = orig_connect
        zsrc.DB_PATH = orig; de.DB_PATH = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_tail_partial_line_defense():
    """#86:_tail_loop 残行防线 —— 写者按块缓冲冲刷出的半行不消费(seek 回
    原位),补全换行后一次计入(镜像 claude._read_new_rows 纪律);完整行
    JSONDecodeError 落一条 dbg(不再零诊断)。合成日志文件,绝不碰真实
    ~/.zcode 日志。"""
    import shutil
    import tempfile
    import threading as thmod
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    tmp = _Path(tempfile.mkdtemp(prefix="zm_tail_"))
    orig_dbg, orig_debug = de.DBG_PATH, de.DEBUG
    log = tmp / "zcode-test.jsonl"
    th = None
    e = None
    try:
        de.DEBUG = True
        de.DBG_PATH = str(tmp / "dbg.log")
        # tail 从 EOF 起步:残行必须写在 tail 线程启动【之后】(写者按块
        # 冲刷出的半行),启动前已有的内容本就不消费
        log.write_text("", encoding="utf-8")
        e = de.DataEngine(queue.Queue(maxsize=1))
        e._log_path = lambda: str(log)          # 实例属性遮蔽方法
        th = thmod.Thread(target=e._tail_loop, daemon=True)
        th.start()
        time.sleep(0.3)                         # 线程就绪(空文件空转)
        with open(log, "a", encoding="utf-8") as f:
            f.write('{"event":"model.request.sta')   # 半行 flush
        time.sleep(0.5)                         # 残行防线:反复重读不消费
        check("#86 残行不被消费(状态机不动作)", e._running is False, "")
        with open(log, "a", encoding="utf-8") as f:
            f.write('rted","sessionId":"sess_t1"}\n')
        time.sleep(0.5)
        check("#86 补全后完整行一次计入", e._running is True, "")
        with open(log, "a", encoding="utf-8") as f:
            f.write('{not json\n')             # 完整坏行:dbg 一条
        time.sleep(0.6)
        with open(de.DBG_PATH, encoding="utf-8") as f:
            dbg_txt = f.read()
        check("#86 完整行 JSONDecodeError 落 dbg",
              "JSONDecodeError dropped" in dbg_txt, dbg_txt[-200:])
        check("#86 坏行不炸状态机", e._running is True, "")
    finally:
        if e is not None:
            e.stop()
        if th is not None:
            th.join(1.0)
        de.DBG_PATH = orig_dbg
        de.DEBUG = orig_debug
        shutil.rmtree(tmp, ignore_errors=True)


def test_manual_switch_deferred():
    """#87 专项:引擎 alive 时 set/clear_manual_session 只投递意图 —— UI
    (调用方)线程零 SQL(计数主线程连接);pending 切换 ≤1 POLL_DB 内生效
    (manual_session/snap.manual/session_id 落地);切换后排除前缀
    (sess_subagent*/sess_dwf-*)在 _latest_session/recent_sessions 原样存活;
    未 start 路径保持同步执行。合成库,run() 由真实线程承载。"""
    import shutil
    import tempfile
    import threading as thmod
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc
    tmp = _Path(tempfile.mkdtemp(prefix="zm_def_"))
    orig_db, orig_np = zsrc.DB_PATH, de._no_persist
    calls = []
    orig_connect = de.connect_ro

    def _counting():
        calls.append(thmod.get_ident())
        return orig_connect()
    de.connect_ro = _counting
    e = None
    try:
        tdb = str(tmp / "t.sqlite")
        con = sqlite3.connect(tdb)
        con.execute("CREATE TABLE part (session_id TEXT)")
        con.execute(
            "CREATE TABLE model_usage (session_id TEXT, query_source TEXT,"
            " status TEXT, started_at INTEGER, model_id TEXT, provider_id TEXT,"
            " input_tokens INTEGER, cache_read_input_tokens INTEGER,"
            " output_tokens INTEGER, duration_ms INTEGER,"
            " time_to_first_token_ms INTEGER,"
            " first_token_at INTEGER, completed_at INTEGER)")
        # session 表:recent_sessions 批查依赖(缺表 → 整体 [],旧 LEFT
        # JOIN 同判);标题留空即『空标题不丢会话』语义
        con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT)")
        # rowid 序:mainA(1) < subagent(2) < mainB(3) < dwf(4,最新)——
        # 最新行是排除会话:跟随/菜单的排除前缀必须原样存活
        con.executemany("INSERT INTO part (session_id) VALUES (?)",
                        [("sess_mainA",), ("sess_subagent-9",),
                         ("sess_mainB",), ("sess_dwf-dwfrun-1",)])
        con.commit(); con.close()
        zsrc.DB_PATH = tdb; de.DB_PATH = tdb
        de._no_persist = lambda: True           # watcher 不启动(免真实 FS 事件)
        main_tid = thmod.get_ident()
        e = de.DataEngine(queue.Queue(maxsize=50))
        e.start()
        time.sleep(1.2)                         # 启动 push/首拍落定
        n_main_before = sum(1 for t in calls if t == main_tid)
        t0 = time.time()
        e.set_manual_session("sess_mainB")
        dur = time.time() - t0
        check("#87 set_manual_session 快速返回(SQL 批不在调用线程)",
              dur < 0.3, f"{dur:.3f}s")
        deadline = time.time() + de.DataEngine.POLL_DB + 1.0
        while time.time() < deadline and not (
                e.session_id == "sess_mainB" and e.snap.manual):
            time.sleep(0.02)
        check("#87 pending ≤1 POLL_DB 内生效(切换落地)",
              e.session_id == "sess_mainB" and e.manual_session == "sess_mainB"
              and e.snap.manual is True,
              f"sess={e.session_id} manual={e.manual_session} snap={e.snap.manual}")
        n_main_after = sum(1 for t in calls if t == main_tid)
        check("#87 UI 线程同步路径零连接(SQL 批全在引擎线程)",
              n_main_after == n_main_before,
              f"{n_main_before} -> {n_main_after}")
        # 排除前缀原样存活(以下两个调用本身在主线程连接,先记账再断言)
        latest = e._latest_session()
        check("#87 切换后 _latest_session 排除前缀存活(=mainB)",
              latest == "sess_mainB", latest)
        rows = e.recent_sessions(8)
        check("#87 recent_sessions 无 subagent/dwf",
              all(not sid.startswith(("sess_subagent", "sess_dwf-"))
                  for sid, _ in rows)
              and {sid for sid, _ in rows} == {"sess_mainA", "sess_mainB"},
              str(rows))
        # 清除:switch_auto 延迟,引擎线程解析最新会话(=mainB,dwf 被排除)
        e.clear_manual_session()
        deadline = time.time() + de.DataEngine.POLL_DB + 1.0
        while time.time() < deadline and e.snap.manual:
            time.sleep(0.02)
        check("#87 clear 延迟生效(恢复自动,仍锚 mainB 非 dwf)",
              e.manual_session is None and e.snap.manual is False
              and e.session_id == "sess_mainB",
              f"{e.manual_session}/{e.snap.manual}/{e.session_id}")
        # 未 start 路径:同步行为不变(既有语义)
        e2 = de.DataEngine(queue.Queue(maxsize=1))
        e2.set_manual_session("sess_mainA")
        check("#87 未 start:同步切换立即生效",
              e2.session_id == "sess_mainA" and e2.snap.manual is True, "")
        e2.clear_manual_session()
        check("#87 未 start:同步清除立即生效",
              e2.manual_session is None and e2.snap.manual is False, "")
        e2.stop()
    finally:
        if e is not None:
            e.stop()
            e.join(timeout=2.0)                 # 先停引擎再还原计数补丁
        de.connect_ro = orig_connect
        de._no_persist = orig_np
        zsrc.DB_PATH = orig_db; de.DB_PATH = orig_db
        shutil.rmtree(tmp, ignore_errors=True)


def test_discover_sources_contract_guards():
    """sources 发现机制治理批断言:
    ① #15 字面导入行 ↔ 包内非_模块 对账(PyInstaller 静态分析只认字面
       import,漏行=该源在 exe 被静默丢弃);
    ② #16 同一类的再导出按类身份去重(不二次实例化,两行同名同值的
       『用量翻倍假象』防线);
    ③ #38 Source 形态不合法(实例/函数/非 UsageSource 子类)loudly raise;
    ④ #73 order 只认 cls.__dict__ 自身声明,子类化内置源不继承 0/10;
    ⑤ #81 两个不同类同名 → raise(引擎 per-source 缓存键兼行名)。"""
    import ast as astmod
    import inspect as insmod
    import os as osmod
    import pkgutil
    import shutil
    import tempfile
    import zcode_meter.sources as srcs
    from zcode_meter.sources import discover_sources

    # ① #15 对账:字面导入行集合 == 包内非_模块集合
    init_src = insmod.getsource(srcs)
    tree = astmod.parse(init_src)
    literal = set()
    for node in astmod.walk(tree):
        if (isinstance(node, astmod.ImportFrom) and node.level == 1
                and node.module is None):
            literal |= {a.name for a in node.names}
    pkg_mods = {m.name for m in pkgutil.iter_modules(srcs.__path__)
                if not m.name.startswith("_")}
    check("#15 字面导入行==包内非_模块(对账)",
          literal == pkg_mods and len(literal) >= 3,
          f"literal={sorted(literal)} pkg={sorted(pkg_mods)}")

    root = tempfile.mkdtemp(prefix="zm_disc2_")
    orig_path = list(srcs.__path__)
    made_sysmods = []
    try:
        pkg = osmod.path.join(root, "fake1")
        osmod.makedirs(pkg)
        mods = {
            # ④ #73:子类化 ZCodeSource(order=0)但不声明自己的 order → 100
            "subz": "from zcode_meter.sources.zcode import ZCodeSource\n"
                    "class Source(ZCodeSource):\n    name = 'SUBZ'\n",
            # ② #16:同一类的再导出 ×2 —— 必须去重为一次
            "reexp": "from zcode_meter.sources.zcode import Source\n",
            "reexp2": "from zcode_meter.sources.zcode import Source\n",
            "aaa": "from zcode_meter.sources.base import UsageSource\n"
                   "class Source(UsageSource):\n    name = 'AAA'\n"
                   "    order = 50\n",
            "zzz": "from zcode_meter.sources.base import UsageSource\n"
                   "class Source(UsageSource):\n    name = 'ZZZ'\n"
                   "    order = 5\n",
        }
        for mod_name, text in mods.items():
            with open(osmod.path.join(pkg, mod_name + ".py"), "w",
                      encoding="utf-8") as f:
                f.write(text)
            made_sysmods.append(mod_name)
        srcs.__path__ = [pkg]
        got = [s.name for s in discover_sources()]
        check("#16 再导出去重(ZCode 恰一次)",
              got.count("ZCode") == 1, str(got))
        check("#73 子类不声明 order 排 100(在 5/50 之后,不继承 0)",
              got == ["ZCode", "ZZZ", "AAA", "SUBZ"], str(got))

        # ③ #38:形态不合法 loudly raise(每个独立假包,互不掺干扰)
        for bad_src, label in (
            ("Source = 12345\n", "实例"),
            ("def Source():\n    pass\n", "函数"),
            ("class Source:\n    name = 'X'\n", "非 UsageSource 子类"),
            ("class Source(UsageSource):\n    name = 38\n", "name 非 str"),
        ):
            pkg2 = osmod.path.join(root, "fake_" + label[:3])
            osmod.makedirs(pkg2, exist_ok=True)
            body = bad_src if "UsageSource" not in bad_src else (
                "from zcode_meter.sources.base import UsageSource\n" + bad_src)
            with open(osmod.path.join(pkg2, "bad.py"), "w",
                      encoding="utf-8") as f:
                f.write(body)
            srcs.__path__ = [pkg2]
            raised = False
            try:
                discover_sources()
            except ValueError:
                raised = True
            check(f"#38 Source 形态不合法({label})→ ValueError", raised,
                  "未抛错(静默跳过)")

        # ⑤ #81:两个不同类同名 → raise
        pkg3 = osmod.path.join(root, "fake_dup")
        osmod.makedirs(pkg3)
        for mod_name in ("d1", "d2"):
            with open(osmod.path.join(pkg3, mod_name + ".py"), "w",
                      encoding="utf-8") as f:
                f.write("from zcode_meter.sources.base import UsageSource\n"
                        "class Source(UsageSource):\n"
                        f"    name = 'DUP'\n    order = {60 + len(mod_name)}\n")
            made_sysmods.append(mod_name)
        srcs.__path__ = [pkg3]
        raised = False
        try:
            discover_sources()
        except ValueError as exc:
            raised = "duplicate source name" in str(exc)
        check("#81 两个不同类同名 → ValueError", raised, "未按同名抛错")
    finally:
        srcs.__path__ = orig_path
        for mod_name in set(made_sysmods) | {"reexp", "reexp2", "subz",
                                             "aaa", "zzz", "bad"}:
            sys.modules.pop(f"zcode_meter.sources.{mod_name}", None)
        shutil.rmtree(root, ignore_errors=True)
    check("还原后内置顺序仍 [ZCode, Claude]",
          [s.name for s in discover_sources()] == ["ZCode", "Claude"], "")


def test_zcode_daily_usage_floor():
    """#22:ZCodeSource.daily_usage 补 MAX_SCAN_ROWS rowid floor(镜像
    fetch_daily_usage 同闸)—— 超限旧行不计;patch 常量即调窗(参数绑定
    证据);唯一定义 re-export 后 de/zsrc 两名同值。"""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc
    tmp = _Path(tempfile.mkdtemp(prefix="zm_zfloor_"))
    orig_db, orig_rows = zsrc.DB_PATH, zsrc.MAX_SCAN_ROWS
    check("#22 唯一定义 re-export:de/zsrc 两名同值(默认)",
          de.MAX_SCAN_ROWS == zsrc.MAX_SCAN_ROWS == 100_000, "")
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
            [(t0 - 3_600_000, "GLM-5.3", "completed", "main_turn",
              1_000, 0, 0),                       # 昨天(rowid 1,将被 floor 裁)
             (t0 + 60_000, "GLM-5.3", "completed", "main_turn",
              2_000, 0, 0),                       # 今天(rowid 2)
             (t0 + 120_000, "GLM-5.3", "completed", "main_turn",
              4_000, 0, 0)])                      # 今天(rowid 3)
        con.commit(); con.close()
        zsrc.DB_PATH = tdb
        # floor 语义 = rowid >= MAX(rowid)-floor:floor=1 → rowid 1(昨天)被裁
        zsrc.MAX_SCAN_ROWS = 1
        z = zsrc.ZCodeSource()
        rows = z.daily_usage(30)
        # 手算:floor=1 只留今天两行(rowid 2/3)→ 6000;无 floor 含昨天 1000
        check("#22 daily_usage 受 floor(=今天 6000,昨天 1000 被裁)",
              [t for _d, t in rows] == [6_000], str(rows))
        rc = sqlite3.connect(tdb)
        manual = rc.execute(
            "SELECT date(started_at/1000, 'unixepoch', 'localtime') AS d,"
            " COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)"
            " FROM model_usage WHERE status='completed' AND started_at>=?"
            " AND rowid >= (SELECT COALESCE(MAX(rowid),0) - ? FROM model_usage)"
            " GROUP BY d ORDER BY d",
            (t0 - 86_400_000, 1)).fetchall()
        rc.close()
        check("#22 =带同 floor 手算逐值相等", rows == manual,
              f"{rows} vs {manual}")
    finally:
        zsrc.DB_PATH = orig_db
        zsrc.MAX_SCAN_ROWS = orig_rows
        shutil.rmtree(tmp, ignore_errors=True)


def test_claude_note_changes_scope_files():
    """#59:note_changes 摄取的新文件补记入 _scope_files —— 两次 walk 之间
    出生即死的文件不再逃过 vanished 检测:其独占 claim 的共享 mid 由幸存
    文件重新计入(改前永久压制,今日用量静默少计且无自愈)。"""
    import os as osmod
    import shutil
    import tempfile
    from zcode_meter.data_engine import ClaudeSource

    line, t_today, _ = _claude_line_factory()
    root = tempfile.mkdtemp(prefix="zm_c59_")
    try:
        proj = osmod.path.join(root, "p1")
        osmod.makedirs(proj)
        fa = osmod.path.join(proj, "a.jsonl")
        fab = osmod.path.join(proj, "ab.jsonl")
        fb = osmod.path.join(proj, "b.jsonl")
        # 场景:两次 walk 之间,watcher 先报 ab 出生(共享 mid n59_s 此刻
        # 空闲 → ab 即时 claim,m=5),再报 b 出生(n59_s 已被 ab 占 →
        # b 的同名行按『首 claim 者胜』抑制,b 只计自有行 20);随后 ab
        # 在下一次 walk 前死亡 —— #59 修复前 ab 不在 _scope_files,
        # vanished 检测永远不触发,n59_s 永久归属死文件,b 的 m=8 行被
        # 永久压制(35 不自愈);修复后 walk 不再列出 ab → 重建 → b 计 38
        with open(fa, "w", encoding="utf-8") as f:
            f.write(line(t_today, "n59_a", 5, 0, 0, 5) + "\n")     # 10
        src = ClaudeSource(projects_dir=root)
        check("#59 基线:首扫 10", src.today_usage() == 10,
              str(src.today_usage()))
        with open(fab, "w", encoding="utf-8") as f:
            f.write(line(t_today, "n59_s", 2, 0, 0, 3) + "\n")     # 5
        src.note_changes([fab])                 # 出生:claim 共享 mid
        check("#59 出生文件被摄取(15)", src.today_usage() == 15,
              str(src.today_usage()))
        with open(fb, "w", encoding="utf-8") as f:
            f.write(line(t_today, "n59_b", 10, 0, 0, 10) + "\n"    # 20
                    + line(t_today, "n59_s", 4, 0, 0, 4) + "\n")   # 8(被抑制)
        src.note_changes([fb])                  # 同名行抑制(b 只计 20)
        check("#59 后到者的共享 mid 行被抑制(35)",
              src.today_usage() == 35, str(src.today_usage()))
        # 即死:下一次 walk 前删除 → vanished 检测必须触发,n59_s 归还 b
        osmod.remove(fab)
        src.SCAN_TTL = 0.05
        time.sleep(0.08)
        got = src.today_usage()
        check("#59 出生即死不逃逸(共享 mid 归还幸存者,10+20+8=38)",
              got == 38, f"got {got}")
        check("#59 重建后稳定(无双计)", src.today_usage() == 38, "")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_claude_reparse_stale_entries():
    """#60:note_changes 的 reparse 重建集过滤已不存在路径 —— 已删文件的
    陈旧条目(条目只增不删是 #19 维持裁决)在任何后续 reparse 事件中不再
    复活重新 claim(vanished 刚解禁的幸存文件不再被压制)。"""
    import os as osmod
    import shutil
    import tempfile
    import time as timemod
    from zcode_meter.data_engine import ClaudeSource

    line, t_today, _ = _claude_line_factory()
    root = tempfile.mkdtemp(prefix="zm_c60_")
    try:
        proj = osmod.path.join(root, "p1")
        osmod.makedirs(proj)
        fa = osmod.path.join(proj, "a.jsonl")
        fb = osmod.path.join(proj, "b.jsonl")
        with open(fa, "w", encoding="utf-8") as f:
            f.write(line(t_today, "m60_a", 5, 0, 0, 5) + "\n")     # 10
        with open(fb, "w", encoding="utf-8") as f:
            f.write(line(t_today, "m60_b", 9, 0, 0, 1) + "\n")     # 10
        src = ClaudeSource(projects_dir=root)
        check("#60 基线 20", src.today_usage() == 20, str(src.today_usage()))
        osmod.remove(fa)
        src.SCAN_TTL = 0.05
        timemod.sleep(0.08)
        check("#60 a 删除后 vanished 重建(余 10)",
              src.today_usage() == 10, str(src.today_usage()))
        # b 原地重写(同长度不同值 9/1→8/1,mtime 前进)→ reparse 事件:
        # 重建集必须排除已删除的 a,否则 a 的陈旧条目复活把 10 抢回来
        timemod.sleep(0.02)
        with open(fb, "w", encoding="utf-8") as f:
            f.write(line(t_today, "m60_b", 8, 0, 0, 1) + "\n")     # 9
        src.note_changes([fb])
        got = src.today_usage()
        check("#60 陈旧条目不复活(重写 b 后 9,非 19)",
              got == 9, f"got {got}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_claude_walk_blind_lastgood():
    """#82:目录级瞬盲(os.walk scandir 错误)按 last-good 保守 —— 当轮聚合
    不清零(盲区文件沿用旧 date_agg)、vanished/returning 判定跳过;恢复
    后数值稳定;真删除仍照常触发 vanished(健康轮裁决力不减)。"""
    import os as osmod
    import shutil
    import tempfile
    from zcode_meter.data_engine import ClaudeSource

    line, t_today, _ = _claude_line_factory()
    root = tempfile.mkdtemp(prefix="zm_c82_")
    real_walk = osmod.walk
    try:
        proj = osmod.path.join(root, "p1")
        osmod.makedirs(proj)
        for name, mid, i, o in (("a.jsonl", "w82_a", 10, 10),
                                ("b.jsonl", "w82_b", 5, 5)):
            with open(osmod.path.join(proj, name), "w", encoding="utf-8") as f:
                f.write(line(t_today, mid, i, 0, 0, o) + "\n")
        src = ClaudeSource(projects_dir=root)
        check("#82 基线 30", src.today_usage() == 30, str(src.today_usage()))
        src.SCAN_TTL = 0.05

        def blind_walk(top, topdown=True, onerror=None, followlinks=False):
            if onerror is None:                 # 引擎外调用不受影响
                yield from real_walk(top, topdown=True)
                return
            onerror(OSError(13, "simulated transient scandir failure"))
            for r, ds, fs in real_walk(top, topdown=True):
                yield r, ds, [f for f in fs if f != "b.jsonl"]

        osmod.walk = blind_walk                 # 注入:子目录瞬盲 + b 落盲区
        try:
            time.sleep(0.08)
            got = src.today_usage()
            check("#82 瞬盲轮聚合按 last-good 不清零(仍 30)",
                  got == 30, f"got {got}")
            time.sleep(0.08)
            check("#82 瞬盲多轮稳定(不触发 vanished/returning)",
                  src.today_usage() == 30, str(src.today_usage()))
        finally:
            osmod.walk = real_walk
        time.sleep(0.08)
        check("#82 恢复健康 walk 后数值稳定", src.today_usage() == 30, "")
        osmod.remove(osmod.path.join(proj, "b.jsonl"))
        time.sleep(0.08)
        check("#82 健康轮真删 b 照常裁决(20)",
              src.today_usage() == 20, str(src.today_usage()))
    finally:
        osmod.walk = real_walk
        shutil.rmtree(root, ignore_errors=True)


def test_claude_nested_scope_isolation():
    """#92:_file_cache 按 scope 嵌套 —— 嵌套 projects_dir 双实例不再跨
    scope 串扰:共享文件级 date_agg 随 scope 独立裁决,两序(inner 先/outer
    先)终值都正确(inner=100/outer=50),TTL 过期重扫不回退。"""
    import os as osmod
    import shutil
    import tempfile
    from zcode_meter.data_engine import ClaudeSource

    line, t_today, _ = _claude_line_factory()
    root = tempfile.mkdtemp(prefix="zm_c92_")

    def build():
        outer = osmod.path.join(root, "outer")
        sub = osmod.path.join(outer, "sub")
        osmod.makedirs(sub, exist_ok=True)
        # sorted 序:OUTER/a.jsonl < OUTER/sub/x.jsonl → outer 域共享 mid 归 a(50)
        with open(osmod.path.join(outer, "a.jsonl"), "w", encoding="utf-8") as f:
            f.write(line(t_today, "n92_m", 25, 0, 0, 25) + "\n")   # 50
        with open(osmod.path.join(sub, "x.jsonl"), "w", encoding="utf-8") as f:
            f.write(line(t_today, "n92_m", 60, 0, 0, 40) + "\n")   # 100
        return outer, sub

    try:
        # 序一:inner 先扫,outer 后扫
        outer, sub = build()
        inner = ClaudeSource(projects_dir=sub)
        outer_src = ClaudeSource(projects_dir=outer)
        check("#92 inner 首值 100(独占共享 mid)", inner.today_usage() == 100,
              str(inner.today_usage()))
        check("#92 outer 随后 50(本 scope 内 a 先 claim)",
              outer_src.today_usage() == 50, str(outer_src.today_usage()))
        check("#92 inner 不被 outer 扫描回退(仍 100)",
              inner.today_usage() == 100, str(inner.today_usage()))
        # TTL 过期重扫:两实例都稳定(类级缓存按 scope 分桶)
        inner.SCAN_TTL = outer_src.SCAN_TTL = 0.05
        time.sleep(0.08)
        check("#92 TTL 重扫 inner 仍 100", inner.today_usage() == 100,
              str(inner.today_usage()))
        check("#92 TTL 重扫 outer 仍 50", outer_src.today_usage() == 50,
              str(outer_src.today_usage()))
        # 序二:outer 先扫,inner 后扫(全新目录,免上一序缓存干扰)
        outer2, sub2 = build()
        outer_first = ClaudeSource(projects_dir=outer2)
        inner_first = ClaudeSource(projects_dir=sub2)
        check("#92(outer 先)outer 50", outer_first.today_usage() == 50,
              str(outer_first.today_usage()))
        check("#92(outer 先)inner 100(此前会得 0)",
              inner_first.today_usage() == 100, str(inner_first.today_usage()))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_claude_deep_nesting_poison():
    """#93:_parse_line 的 json.loads 捕 RecursionError —— 深嵌套毒行
    (≥17000 层)按坏行跳过,today_usage 不抛、不杀引擎 _db_loop。"""
    import datetime as dtmod
    import os as osmod
    import shutil
    import tempfile
    from zcode_meter.data_engine import ClaudeSource

    line, t_today, _ = _claude_line_factory()
    root = tempfile.mkdtemp(prefix="zm_c93_")
    try:
        proj = osmod.path.join(root, "p1")
        osmod.makedirs(proj)
        poison = "[" * 20_000 + "]" * 20_000    # > 最小触发深度 16916
        with open(osmod.path.join(proj, "s.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write(line(t_today, "n93_ok", 4, 0, 0, 6) + "\n"     # 10
                    + poison + "\n")
        src = ClaudeSource(projects_dir=root)
        got = src.today_usage()                  # 改前 RecursionError 上抛
        check("#93 深嵌套毒行按坏行跳过(today=10 不抛)",
              got == 10, f"got {got}")
        check("#93 按天同判",
              dict(src.daily_usage(1)).get(dtmod.date.today().isoformat()) == 10,
              "")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_push_full_queue_drop_oldest():
    """P1(2026-10-08):UI 队列满时 _push 丢最旧腾位而非静默丢自己,且
    _push_fp 仅在真实入队后推进。旧缺陷链:拖动期 startSystemMove 模态
    循环停摆 timer(app.py moveEvent 自述)→ 队列积压满 → _db_tick 的
    push 被吞但基线照推进 → 满窗期最后变化且此后不再变的字段(state→idle
    的 run() 单次 push 同被吞)对 UI 永久丢失。消费侧 _poll_queue 每
    200ms 排干整队列末值胜出,丢最旧零损失。"""
    import os as osmod
    import shutil
    import tempfile
    from zcode_meter import data_engine as de
    from zcode_meter.sources import zcode as zsrc
    orig = zsrc.DB_PATH
    tmp = tempfile.mkdtemp(prefix="zm_pushfull_")
    try:
        zsrc.DB_PATH = de.DB_PATH = osmod.path.join(tmp, "probe.db")
        q = queue.Queue(maxsize=2)
        e = de.DataEngine(q)
        q.put("old1"); q.put("old2")            # 拖动期积压占位
        e.snap.today_tokens = 222               # 满窗期最后一次易变字段变化
        e._db_tick(woken=False)
        items = list(q.queue)
        check("满队列 tick:最旧被逐出、快照真实入队(222 在队尾)",
              q.qsize() == 2 and items[0] == "old2"
              and getattr(items[1], "today_tokens", None) == 222,
              f"qsize={q.qsize()} items={items!r}")
        check("满队列 tick:_push_fp 仅在真实 push 后推进",
              e._push_fp is not None and 222 in e._push_fp, "")
        while True:                             # 排水(拖动结束)
            try:
                q.get_nowait()
            except queue.Empty:
                break
        e._db_tick(woken=False)                 # 稳态拍:fp 未再变
        check("排水后稳态拍 0 push(值未变不 push 不回归)",
              q.qsize() == 0, f"qsize={q.qsize()}")
        q3 = queue.Queue(maxsize=1)             # run() 循环 state 单次 push 同机制
        e3 = de.DataEngine(q3)
        q3.put("stale")
        e3.snap.state = "idle"
        with e3.snap_lock:
            e3._push()
        check("满队列 state 单次 push 不丢(idle 快照送达)",
              q3.get_nowait().state == "idle", "")
        e.stop(); e3.stop()
    finally:
        zsrc.DB_PATH = de.DB_PATH = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_claude_blind_no_dead_resurrect():
    """P1(2026-10-08):_scan 盲轮合并只并 last —— 已被健康轮裁决消失的
    死文件陈旧 date_agg 不得复活(与 #60『已删文件陈旧条目不得复活』同
    纪律);活着但未被盲 walk 列出的文件仍按 last-good 保守并入(#82
    语义不回归)。两形态:唯一 mid(死文件全额复活)、共享 mid(双计)。"""
    import datetime as dtmod
    import json as jsonmod
    import os as osmod
    import shutil
    import tempfile
    from zcode_meter.data_engine import ClaudeSource

    ts = dtmod.datetime.now(dtmod.timezone.utc).isoformat().replace("+00:00", "Z")

    def wj(path, mid, i, o):
        with open(path, "w", encoding="utf-8") as f:
            f.write(jsonmod.dumps({"type": "assistant", "message": {"id": mid,
                    "usage": {"input_tokens": i, "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0,
                              "output_tokens": o}}, "timestamp": ts}) + "\n")

    def rescan(src):
        src._entries, src._scanned_at = None, 0.0
        return src.today_usage()

    real_walk = osmod.walk

    def blind_walk(top, topdown=True, onerror=None, followlinks=False):
        if onerror:
            onerror(OSError(13, "simulated scandir blindness"))
        return iter([])

    def blind(src):
        osmod.walk = blind_walk
        try:
            return rescan(src)
        finally:
            osmod.walk = real_walk

    # S1 唯一 mid:死文件用量在盲轮不得复活
    d1 = tempfile.mkdtemp(prefix="zm_blindfix1_")
    try:
        wj(osmod.path.join(d1, "z.jsonl"), "a1", 100, 10)
        wj(osmod.path.join(d1, "b.jsonl"), "b1", 40, 10)
        s1 = ClaudeSource(projects_dir=d1)
        alive = rescan(s1)
        osmod.remove(osmod.path.join(d1, "b.jsonl"))
        vanish = rescan(s1)                    # 健康轮裁决消失:110
        b = blind(s1)
        heal = rescan(s1)
        check("盲轮:死文件(唯一 mid)不复活",
              (alive, vanish, b, heal) == (160, 110, 110, 110),
              f"alive={alive} vanish={vanish} blind={b} heal={heal}")
    finally:
        shutil.rmtree(d1, ignore_errors=True)
    # S2 共享 mid:盲轮不得双计
    d2 = tempfile.mkdtemp(prefix="zm_blindfix2_")
    try:
        wj(osmod.path.join(d2, "a.jsonl"), "X", 80, 20)
        wj(osmod.path.join(d2, "z.jsonl"), "X", 80, 20)
        s2 = ClaudeSource(projects_dir=d2)
        alive = rescan(s2)
        osmod.remove(osmod.path.join(d2, "a.jsonl"))
        vanish = rescan(s2)
        b = blind(s2)
        check("盲轮:共享 mid 不双计",
              (alive, vanish, b) == (100, 100, 100),
              f"alive={alive} vanish={vanish} blind={b}")
    finally:
        shutil.rmtree(d2, ignore_errors=True)
    # S3 对照:无删除时盲轮仍 last-good 保守(#82 不回归)
    d3 = tempfile.mkdtemp(prefix="zm_blindfix3_")
    try:
        wj(osmod.path.join(d3, "z.jsonl"), "a1", 100, 10)
        s3 = ClaudeSource(projects_dir=d3)
        alive = rescan(s3)
        b = blind(s3)
        check("盲轮:活文件 last-good 并入(#82 语义保留)",
              (alive, b) == (110, 110), f"alive={alive} blind={b}")
    finally:
        shutil.rmtree(d3, ignore_errors=True)


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
    # ---- v1 T5 新增:ClaudeSource 增量偏移解析(关 registry#19,合成临时目录) ----
    print("== test_claude_source_incremental_append =="); test_claude_source_incremental_append()
    print("== test_claude_source_reparse_rebuild ==");    test_claude_source_reparse_rebuild()
    print("== test_claude_source_note_changes_concurrency =="); test_claude_source_note_changes_concurrency()
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
    # ---- 皮肤配置键(v0.8.x 皮肤系统:白名单/归一化/可选键落盘) ----
    print("== test_skin_optional_key ==");                test_skin_optional_key()
    # ---- 趋势外推(T3:trend_forecast 纯函数,合成行手算对账,零网络零库) ----
    print("== test_trend_forecast_hand_computed ==");     test_trend_forecast_hand_computed()
    # ---- 速度趋势 sparkline 数据源(v0.8.0 T4:合成库手算对账,值/序/截断/排除/异常) ----
    print("== test_fetch_recent_speeds_synthetic ==");    test_fetch_recent_speeds_synthetic()
    # ---- T2:全局吞吐完成分量 started_at 前置换行 + 行缓存衰减(取数/加权拆分) ----
    print("== test_global_completed_tps_started_at_cache ==")
    test_global_completed_tps_started_at_cache()
    # ---- T1:全局吞吐流式分量 水位走读+rowid IN 定点探针(80.7/45.95ms→~0.1ms) ----
    print("== test_global_part_tps_watermark_walk_probe ==")
    test_global_part_tps_watermark_walk_probe()
    # ---- T6:Claude watcher 线程(watchdog 可选,注入式 —— 无 watchdog 也绿) ----
    print("== test_claude_watcher_injected ==")
    test_claude_watcher_injected()
    # ---- T3:recent_sessions 缓存读(引擎维护 sid→rowid,菜单暖读 ≤1ms;新旧全等/冷回退/删行裁剪/锁序) ----
    print("== test_recent_sessions_cache_read ==")
    test_recent_sessions_cache_read()
    # ---- T4:ZCode db 文件闸门+today0 时间闸+_wake+变化即 push(空闲 0 SQL/0 持锁) ----
    print("== test_t4_db_gate_time_gate_and_wake ==")
    test_t4_db_gate_time_gate_and_wake()
    # ---- v0.9.2 T1 治理批逐项新单测(#9/10/11/12/14/17/28/41+62/48/64/75/84/86/87 等) ----
    print("== test_load_config_early_exit_bar_segments =="); test_load_config_early_exit_bar_segments()
    print("== test_handle_log_line_guards ==");             test_handle_log_line_guards()
    print("== test_on_request_done_main_turn_scope ==");    test_on_request_done_main_turn_scope()
    print("== test_global_tps_completion_tick_dedup ==");   test_global_tps_completion_tick_dedup()
    print("== test_switch_session_resets_exact_chars ==");  test_switch_session_resets_exact_chars()
    print("== test_source_guard_exception ==");             test_source_guard_exception()
    print("== test_db_path_patch_contract ==");             test_db_path_patch_contract()
    print("== test_prices_hot_reload ==");                  test_prices_hot_reload()
    print("== test_fetch_daily_model_usage_raise_on_error =="); test_fetch_daily_model_usage_raise_on_error()
    print("== test_plan_remaining_pct_deprecated_compat =="); test_plan_remaining_pct_deprecated_compat()
    print("== test_push_snapshot_copy ==");                 test_push_snapshot_copy()
    print("== test_boot_queries_and_ensure ==");            test_boot_queries_and_ensure()
    print("== test_tail_partial_line_defense ==");          test_tail_partial_line_defense()
    print("== test_manual_switch_deferred ==");             test_manual_switch_deferred()
    print("== test_discover_sources_contract_guards ==");   test_discover_sources_contract_guards()
    print("== test_zcode_daily_usage_floor ==");            test_zcode_daily_usage_floor()
    print("== test_claude_note_changes_scope_files ==");    test_claude_note_changes_scope_files()
    print("== test_claude_reparse_stale_entries ==");       test_claude_reparse_stale_entries()
    print("== test_claude_walk_blind_lastgood ==");         test_claude_walk_blind_lastgood()
    print("== test_claude_nested_scope_isolation ==");      test_claude_nested_scope_isolation()
    print("== test_claude_deep_nesting_poison ==");         test_claude_deep_nesting_poison()
    # ---- 2026-10-08 P1 修复回归:满队列丢最旧+_push_fp 真实推进/盲轮死文件不复活 ----
    print("== test_push_full_queue_drop_oldest ==");         test_push_full_queue_drop_oldest()
    print("== test_claude_blind_no_dead_resurrect ==");      test_claude_blind_no_dead_resurrect()
    if FAILED:
        print(f"\nFAILED: {FAILED}")
        sys.exit(1)
    print("\nDATA ENGINE TESTS ALL PASS")
