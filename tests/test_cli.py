#!/usr/bin/env python3
"""CLI 伴侣(__main__.py)专项单测(v0.9.2 T5b;standalone 可跑,run_all
默认组 cli 命令以 cwd=仓库根 子进程执行本文件)。

断言全部来自 registry open 条目(#4/5/6/45/84)钉下的行为契约,与
test_data_engine.py 的 FakeEngine 注入组互补:该组钉『聚合展示不漂移口径』
(Fake 驱动),本文件一律走 tmp 合成库 + zsrc.DB_PATH 单点 patch(#41/#62
单点 patch 契约:connect_ro 函数体读 sources.zcode 模块全局)驱动真实
DataEngine + 真实 SQL 路径,绝不触真实 ~/.zcode。五组:

  ① 故障通道(#4):库缺失/坏库(非 SQLite 文件)→ rc=1 + stderr 含
     "error" 行(含路径与异常类),且不再伪装成空窗口报表;正常 tmp 库
     (有数据)→ rc=0;
  ② 显示宽度(#6):_disp_width CJK/全角=2、ASCII=1;表头/合计行的
     tokens/￥ 列头与 ASCII 数据列按显示宽度对齐 —— 期望字符串用测试内
     的独立字符算术拼出(不用被测函数,防自证);
  ③ in_cache 上限注脚(#45):tmp zm_prices.json 只给 in/out 的新模型且确有
     cache_read 行 → 上限注脚出现、下限注脚不出现;零 cache_read 的缺档
     模型不触发;分类判定与 cost_of 实算交叉对账(镜像不漂,计算不动);
  ④ MAX_SCAN_ROWS 披露(#5):口径行恒含『历史聚合仅统计最近 100,000 行,
     更早记录不计』(有数据/空窗口两态恒真;闸本体不动);
  ⑤ boot_queries(#84b):CLI 构造+报表全程 connect_ro 连接计数==报表
     查询本身 1 次 —— 启动预热三查询与缺库 ROLL_DIR 全目录扫描不再发生
     (对照面:boot_queries=True 时恰为 3 次预热连接,证明参数确在生效)。
     计数域隔离:#4 故障通道的探针是 CLI 主动发起的连接,另有 ① 专项全链
     路钉死,⑤ 内 stub 为『已通过』—— 与验收文案『CLI 构造+报表全程』
     严格同界,数到的恰好是构造+报表的连接数。
"""
import contextlib
import io
import json
import os
import queue
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# src 布局下 src/ 不在 sys.path(本文件经 run_all.py 以 cwd=仓库根 子进程
# 运行,或直接 python tests/test_cli.py),与 test_data_engine.py 同款显式补入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 与 test_package.py 同款隔离开关:绝不触碰用户真实状态文件
os.environ.setdefault("ZM_NO_STATE", "1")

import zcode_meter.__main__ as zm_cli        # noqa: E402
from zcode_meter import data_engine as de    # noqa: E402
from zcode_meter.sources import zcode as zsrc  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# model_usage 列序与既有合成库测试逐字同形(started_at/model_id/status/
# query_source/input_tokens/cache_read_input_tokens/output_tokens);
# part 表给 _latest_session 一行可用结果(#84 对照面构造 True 时不再落
# ROLL_DIR 兜底扫描 —— 兜底会 os.listdir 真实 ~/.zcode/cli/rollout,与
# 本文件『不触真实 ~/.zcode』的承诺冲突)
SCHEMA = (
    "CREATE TABLE model_usage (started_at INTEGER, model_id TEXT,"
    " status TEXT, query_source TEXT, input_tokens INTEGER,"
    " cache_read_input_tokens INTEGER, output_tokens INTEGER)",
    "CREATE TABLE part (session_id TEXT, payload TEXT)",
)


def make_db(path: Path, usage_rows, part_rows=(("sess_cli_t1", "x"),)) -> Path:
    con = sqlite3.connect(str(path))
    for stmt in SCHEMA:
        con.execute(stmt)
    con.executemany("INSERT INTO model_usage VALUES (?,?,?,?,?,?,?)", usage_rows)
    con.executemany("INSERT INTO part VALUES (?,?)", part_rows)
    con.commit()
    con.close()
    return path


class _PatchedDB:
    """tmp 库注入(#41/#62 单点 patch 契约):zsrc.DB_PATH 是唯一定义,
    connect_ro 函数体读 zsrc 模块全局 —— patch 打单点才生效;de.DB_PATH
    同步打(既有测试同款双保险,覆盖读 de 命名空间的路径)。finally 恢复,
    绝不碰真实 ~/.zcode 库。"""

    def __init__(self, path):
        self.path = str(path)
        self._zsrc, self._de = zsrc.DB_PATH, de.DB_PATH

    def __enter__(self):
        zsrc.DB_PATH = de.DB_PATH = self.path
        return self

    def __exit__(self, *exc):
        zsrc.DB_PATH, de.DB_PATH = self._zsrc, self._de
        return False


class _PatchedPrices:
    """tmp zm_prices.json 注入:load_prices 在调用时读 data_engine 模块级
    PRICES_PATH 全量(deep-merge 默认表),patch 后默认文件(仓库根)整份
    不再参与 —— ③ 的『新模型只给 in/out』才能精确成型。"""

    def __init__(self, path):
        self.path = str(path)
        self._orig = de.PRICES_PATH

    def __enter__(self):
        de.PRICES_PATH = self.path
        return self

    def __exit__(self, *exc):
        de.PRICES_PATH = self._orig
        return False


def _run_cli(argv):
    """main() 全链路跑一次(真实 DataEngine+真实 SQL),捕获 (rc, stdout,
    stderr)。本文件所有全链路断言的统一入口 —— rc 语义(0=可读库,1=库
    故障,2=参数非法)正是 #4 故障通道的可测面。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = zm_cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ========== ① 故障通道(#4):库缺失/坏库 rc=1+stderr;正常库 rc=0 ==========

def test_fault_channel_rc1_vs_empty_window_rc0():
    """rc=0 必须专属于『库可读』:缺失/坏库以 rc=1+stderr(含路径与异常类)
    结束,且不再以『窗口内无 completed 用量记录』的空报表伪装成空窗口 ——
    脚本化调用里故障与真空窗只能靠退出码区分,stdout 在故障路径必须是空的
    (半张报表比没有更误导)。坏库样本=垃圾字节的非 SQLite 文件:sqlite3
    connect 惰性校验,错误在 execute 处才浮出 —— 这正是探针形状『connect_ro
    +SELECT 1』的存在理由(只 connect 探不出坏库)。"""
    import datetime as dtmod
    tmp = Path(tempfile.mkdtemp(prefix="zm_cli1_"))
    try:
        # a) 库缺失:DB_PATH 指向不存在路径 → rc=1,stderr 报错含路径
        with _PatchedDB(tmp / "missing.sqlite"):
            rc, out, err = _run_cli(["cost", "--days", "7"])
        check("故障:库缺失 rc=1", rc == 1, f"rc={rc} err={err}")
        check("故障:库缺失 stderr 含 error 行",
              any(ln.strip().startswith("error") for ln in err.splitlines()),
              err)
        check("故障:库缺失 stderr 报错含库路径",
              str(tmp / "missing.sqlite") in err, err)
        check("故障:库缺失不伪装空窗报(stdout 无输出)",
              rc == 1 and out == "" and "窗口内无 completed" not in out,
              f"rc={rc} stdout={out!r}")

        # b) 坏库:存在但非 SQLite 文件 → rc=1,stderr 含异常类
        bad = tmp / "bad.sqlite"
        bad.write_bytes(b"this is definitely not a sqlite3 database file" * 8)
        with _PatchedDB(bad):
            rc, out, err = _run_cli(["cost", "--days", "7"])
        check("故障:坏库 rc=1", rc == 1, f"rc={rc} err={err}")
        check("故障:坏库 stderr 含 error 行",
              any(ln.strip().startswith("error") for ln in err.splitlines()),
              err)
        check("故障:坏库 stderr 含异常类(sqlite3.Error 家族)",
              "DatabaseError" in err or "OperationalError" in err
              or "Error" in err.replace("error: ", "", 1).replace("error:", "", 1),
              err)
        check("故障:坏库 stderr 报错含库路径",
              str(bad) in err, err)
        check("故障:坏库不伪装空窗报(stdout 无输出)",
              rc == 1 and out == "" and "窗口内无 completed" not in out,
              f"rc={rc} stdout={out!r}")

        # c) 正常 tmp 库(有数据)→ rc=0,报表走通,stderr 静默
        t0 = de.today0_ms()
        make_db(tmp / "ok.sqlite", [
            (t0 + 60_000, "GLM-5.3", "completed", "main_turn",
             2_000_000, 1_000_000, 500_000),
        ])
        with _PatchedDB(tmp / "ok.sqlite"):
            rc, out, err = _run_cli(["cost", "--days", "7"])
        today = dtmod.date.today().isoformat()
        check("正常库 rc=0(库可读与空窗/故障分道)",
              rc == 0, f"rc={rc} err={err}")
        check("正常库 stderr 静默", rc == 0 and err == "", err)
        check("正常库 stdout 有当日数据行与合计行",
              today in out and "合计" in out, out)
        check("正常库口径行含 MAX_SCAN_ROWS 披露(全链路)",
              "历史聚合仅统计最近 100,000 行" in out, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ========== ② 显示宽度(#6):CJK/全角=2 的补位与列头对齐 ==========

def test_display_width_and_alignment():
    """等宽终端里表头/合计的 CJK 单元格按显示宽度补位(tokens/￥ 列头与
    ASCII 数据列同边对齐)。期望行用测试内的独立字符算术拼出(『日期』
    两个 CJK 字符=显示宽 4,右补 8 空格到 12 —— 不引用被测的 _disp_width,
    防止实现与断言用同一把尺子自证)。"""
    # 纯函数:宽度与补位
    check("宽度:纯 ASCII=1/字符", zm_cli._disp_width("tokens") == 6)
    check("宽度:纯 CJK=2/字符", zm_cli._disp_width("日期") == 4)
    check("宽度:全角￥(U+FFE5)=2", zm_cli._disp_width("￥") == 2)
    check("宽度:混合串", zm_cli._disp_width("日期tokens") == 10)
    check("宽度:空串=0", zm_cli._disp_width("") == 0)
    check("宽度:歧义字符(≈)按 1(不进对齐列)",
          zm_cli._disp_width("≈") == 1)
    check("补位:右对齐按显示宽差额(￥ 到 12 恰补 10 空格)",
          zm_cli._pad_l("￥", 12) == " " * 10 + "￥")
    check("补位:左对齐 CJK 到 12 恰补 8 空格",
          zm_cli._pad_r("日期", 12) == "日期" + " " * 8)
    check("补位:零差额恒等", zm_cli._pad_r("日期", 4) == "日期")
    check("补位:超宽不截断", zm_cli._pad_r("日期", 2) == "日期")

    # 报表级:三列(日期 12 / tokens 16 / ￥ 12)显示宽度恒 40,分隔线
    # "-"*40 与之对齐;数据行全 ASCII,左是表头/合计的 CJK 补位
    rows = [("2026-10-06", "GLM-5.3", 1_000_000, 800_000, 200_000, 24.0, False)]
    lines = zm_cli._fmt_report(rows, 1).splitlines()
    header = [ln for ln in lines if "tokens" in ln][0]
    data = [ln for ln in lines if "2026-10-06" in ln][0]
    total = [ln for ln in lines if "合计" in ln][0]
    # 独立算术拼期望:『日期』=4 显示宽,右补 8 到 12;『tokens』ASCII 6,
    # 左补 10 到 16;『￥』=2,左补 10 到 12 —— tokens 列头与数据列右缘
    # 同在显示宽 28(12+16),￥ 列头与金额列右缘同在 40。数据/合计行数字
    # "1,200,000"(9 字符)左补 7 到 16:数据行前缀 2+7、合计行 8+7 空格
    want_header = "日期" + " " * 18 + "tokens" + " " * 10 + "￥"
    want_data = "2026-10-06" + " " * 2 + " " * 7 + "1,200,000" + " " * 7 + "24.00"
    want_total = "合计" + " " * 15 + "1,200,000" + " " * 7 + "24.00"
    check("对齐:表头三列按显示宽度补位", header == want_header, f"{header!r}")
    check("对齐:数据行 ASCII 列不受影响", data == want_data, f"{data!r}")
    check("对齐:合计行 CJK 补位与数据列对齐", total == want_total, f"{total!r}")
    # tokens/￥ 列头的右缘与数据行/合计行逐字符对齐(独立算术:每行按
    # 『CJK(码点>U+2E80)=2、其余=1』计前缀显示宽 —— 列内容右缘必须逐行相等)
    def _disp_prefix_width(ln, n):
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in ln[:n])

    for ln_name, ln in (("表头", header), ("数据行", data), ("合计", total)):
        tok_anchor = "tokens" if ln_name == "表头" else "1,200,000"
        cny_anchor = "￥" if ln_name == "表头" else "24.00"
        idx_tok = ln.index(tok_anchor)
        idx_cny = ln.index(cny_anchor)
        check(f"对齐:{ln_name} tokens 列右缘显示宽==28",
              _disp_prefix_width(ln, idx_tok + len(tok_anchor)) == 28, f"{ln!r}")
        check(f"对齐:{ln_name} ￥ 列右缘显示宽==40",
              _disp_prefix_width(ln, idx_cny + len(cny_anchor)) == 40, f"{ln!r}")
    sep = [ln for ln in lines if set(ln) == {"-"}][0]
    check("对齐:分隔线宽==列总显示宽 40", len(sep) == 40, sep)


# ========== ③ in_cache 上限注脚(#45):只加标记不改计算 ==========

def test_cache_tier_upper_bound_footnote():
    """『在价格表内、in/out 档齐备、但缺 in_cache 档』的模型确有 cache_read
    行时,cost_of 规则 1(cache 按输入全价计,宁可高估)让金额成为上限 →
    打上限注脚(与未知模型的下限注脚分列);零 cache_read 的缺档模型金额
    精确,不触发。分类函数与 cost_of 实算交叉对账,钉死镜像不漂(计算
    本体一字不动 —— 预置裁决 2)。"""
    from zcode_meter.data_engine import cost_of
    prices = {"no-cap-model": {"in": 1.0, "out": 2.0},
              "with-cap-model": {"in": 1.0, "in_cache": 0.25, "out": 2.0}}
    # 分类函数:四形态全覆盖(镜像 cost_of 入口判定)
    check("分类:缺 in_cache 档", zm_cli._cache_tier_missing(prices, "no-cap-model") is True)
    check("分类:有 in_cache 档", zm_cli._cache_tier_missing(prices, "with-cap-model") is False)
    check("分类:未知模型不认(partial 下限族)",
          zm_cli._cache_tier_missing(prices, "unknown") is False)
    check("分类:表内但缺 in/out 档也不认(partial 族)",
          zm_cli._cache_tier_missing({**prices, "bad": {"out": 2.0}}, "bad") is False)
    check("分类:非 dict prices 安全退让",
          zm_cli._cache_tier_missing(None, "no-cap-model") is False)  # type: ignore[arg-type]
    # cost_of 交叉对账:同一模型同一行用量,缺档时 cache 项按 in 全价计 ——
    # 补一个极小 in_cache 档重算金额必然下降(镜像分类族 == 规则 1 生效族)
    c_full, p1 = cost_of(prices, "no-cap-model", 1_000, 100, 1_000)
    c_cap, p2 = cost_of({**prices, "no-cap-model": {"in": 1.0, "in_cache": 0.01, "out": 2.0}},
                        "no-cap-model", 1_000, 100, 1_000)
    check("交叉:缺档模型 cost_of 非 partial", p1 is False and p2 is False)
    check("交叉:补缓存档后金额下降(缺档=按全价计 cache)",
          c_full > c_cap, f"full={c_full} capped={c_cap}")

    # 两注脚并列(分列打印:同窗口既有未覆盖模型又有缺缓存档模型 → 两行
    # 各自独立出现,下限行在先、上限行在后,与 README 的列举序一致)
    rows = [("2026-10-06", "unknown-model", 100_000, 0, 50_000, 0.0, True),
            ("2026-10-06", "no-cap-model", 1_000_000, 800_000, 200_000, 1.4, False)]
    text = zm_cli._fmt_report(rows, 1, prices)
    check("分列:两族注脚可同窗口并列且各自成行",
          "金额为下限" in text and "金额为上限" in text
          and text.index("金额为下限") < text.index("金额为上限"), text)

    # 报表级(全链路:真实引擎 + tmp zm_prices.json + tmp 合成库)
    tmp = Path(tempfile.mkdtemp(prefix="zm_cli3_"))
    try:
        prices_file = tmp / "zm_prices.json"
        prices_file.write_text(
            json.dumps({"no-cap-model": {"in": 1.0, "out": 2.0}}),
            encoding="utf-8")
        t0 = de.today0_ms()
        # 窗口内:no-cap-model(缺缓存档,cache_read>0)+ GLM-5.3(默认表
        # 带缓存档,cache_read>0 也不触发)—— 全模型在表内,partial 恒 False
        make_db(tmp / "t.sqlite", [
            (t0 + 60_000, "no-cap-model", "completed", "main_turn",
             1_000_000, 800_000, 200_000),
            (t0 + 120_000, "GLM-5.3", "completed", "main_turn",
             500_000, 400_000, 100_000),
        ])
        with _PatchedDB(tmp / "t.sqlite"), _PatchedPrices(prices_file):
            rc, out, err = _run_cli(["cost", "--days", "1"])
        check("上限注脚:缺缓存档+确有 cache 行 → 出现且 rc=0",
              rc == 0 and "缺缓存档" in out and "上限" in out, f"rc={rc} {out}")
        check("上限注脚:文案逐字(预置裁决 2)",
              "注:部分模型缺缓存档价格,cache 按输入全价计,金额为上限(≈)" in out,
              out)
        check("上限注脚:下限注脚不出现(全模型在表内,两族分列)",
              "金额为下限" not in out, out)

        # 对照面:同款缺档模型但 cache_read=0(金额精确,规则 1 无从高估)
        make_db(tmp / "t2.sqlite", [
            (t0 + 60_000, "no-cap-model", "completed", "main_turn",
             1_000_000, 0, 200_000),
        ])
        with _PatchedDB(tmp / "t2.sqlite"), _PatchedPrices(prices_file):
            rc, out, err = _run_cli(["cost", "--days", "1"])
        check("上限注脚:零 cache_read 行不触发(金额精确,注脚反而是误导)",
              rc == 0 and "金额为上限" not in out and "金额为下限" not in out,
              f"rc={rc} {out}")

        # 对照面:默认表带缓存档的模型(GLM-5.3)即便 cache_read>0 也不触发
        make_db(tmp / "t3.sqlite", [
            (t0 + 60_000, "GLM-5.3", "completed", "main_turn",
             2_000_000, 1_000_000, 500_000),
        ])
        with _PatchedDB(tmp / "t3.sqlite"), _PatchedPrices(prices_file):
            rc, out, err = _run_cli(["cost", "--days", "1"])
        check("上限注脚:默认表带缓存档模型不触发",
              rc == 0 and "金额为上限" not in out, f"rc={rc} {out}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ========== ④ MAX_SCAN_ROWS 披露(#5):口径行恒含 ==========

def test_scan_rows_disclosure_always_present():
    """口径行恒含『历史聚合仅统计最近 100,000 行,更早记录不计』—— 恒真
    陈述(闸本体不动,预置裁决 1:--days>实际覆盖范围时合计是无声下限,
    披露让截断可预期),与 README 口径表/CLI 节同文。空窗口(常被重定向
    贴_issue 自描述)也必须含。"""
    rows = [("2026-10-06", "GLM-5.3", 1_000_000, 800_000, 200_000, 24.0, False)]
    for label, data in (("有数据", rows), ("空窗口", [])):
        text = zm_cli._fmt_report(data, 1)
        check(f"口径行含 MAX_SCAN_ROWS 披露({label})",
              "历史聚合仅统计最近 100,000 行,更早记录不计" in text, text)
        check(f"口径行其余口径不变({label})",
              "ZCode-DB-only" in text and "刊例价" in text, text)
        check(f"口径行恒打印(空窗口也含表头+口径行,{label})",
              "日期" in text and "合计" in text, text)


# ========== ⑤ boot_queries(#84b):构造+报表全程连接计数==1 ==========

def test_boot_queries_connection_count():
    """CLI 构造+报表全程,connect_ro 连接计数==报表查询本身 1 次(#84b:
    三个启动预热查询与缺库 ROLL_DIR 全目录扫描不再发生)。对照面:同一
    tmp 库、同一计数 harness,boot_queries=True 构造恰为 3 次预热连接、
    False 构造 0 次 —— 两个数一起证明参数确在生效且 CLI 吃到的是 False 路径。
    计数域隔离:探针(#4 故障通道的主动连接)stub 为『已通过』,① 组已
    全链路钉死其行为 —— 本组数到的恰好是构造+报表的连接数,与验收文案
    『CLI 构造+报表全程』严格同界。计数 wrapper 透传真实连接(报表查询
    要真实执行),de 与 zsrc 两侧命名空间都打上(同一函数对象的不同绑定,
    patch 各自命名空间才各自生效)。"""
    tmp = Path(tempfile.mkdtemp(prefix="zm_cli5_"))
    try:
        t0 = de.today0_ms()
        make_db(tmp / "t.sqlite", [
            (t0 + 60_000, "GLM-5.3", "completed", "main_turn",
             2_000_000, 1_000_000, 500_000),
        ])
        with _PatchedDB(tmp / "t.sqlite"):
            # 对照面 1:boot_queries=True(缺省)→ 构造期恰 3 次预热连接
            calls = {"n": 0}
            _orig_de, _orig_zsrc = de.connect_ro, zsrc.connect_ro

            def _counting():
                calls["n"] += 1
                return _orig_de()

            de.connect_ro = zsrc.connect_ro = _counting
            try:
                eng = de.DataEngine(queue.Queue(maxsize=1), boot_queries=True)
                eng.stop()
            finally:
                de.connect_ro, zsrc.connect_ro = _orig_de, _orig_zsrc
            check("对照:boot_queries=True 构造恰 3 次预热连接(参数在生效)",
                  calls["n"] == 3, f"n={calls['n']}")

            # 对照面 2:boot_queries=False → 构造 0 次连接
            calls2 = {"n": 0}

            def _counting2():
                calls2["n"] += 1
                return _orig_de()

            de.connect_ro = zsrc.connect_ro = _counting2
            try:
                eng = de.DataEngine(queue.Queue(maxsize=1), boot_queries=False)
                eng.stop()
            finally:
                de.connect_ro, zsrc.connect_ro = _orig_de, _orig_zsrc
            check("对照:boot_queries=False 构造 0 次连接",
                  calls2["n"] == 0, f"n={calls2['n']}")

            # 主断言:main() 全程(构造+报表,探针已 stub)恰 1 次连接
            calls3 = {"n": 0}

            def _counting3():
                calls3["n"] += 1
                return _orig_de()

            _orig_probe = zm_cli._probe_db
            zm_cli._probe_db = lambda: None     # 探针已通过(① 组专项覆盖)
            de.connect_ro = zsrc.connect_ro = _counting3
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = zm_cli.main(["cost", "--days", "1"])
            finally:
                de.connect_ro, zsrc.connect_ro = _orig_de, _orig_zsrc
                zm_cli._probe_db = _orig_probe
        check("boot_queries:main() 全程 rc=0 且报表真实走通",
              rc == 0 and "合计" in out.getvalue(), f"rc={rc} {out.getvalue()}")
        check("boot_queries:CLI 构造+报表全程连接计数==1(报表查询本身)",
              calls3["n"] == 1, f"n={calls3['n']}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ========== ⑥ connect_ro URI 形态(2026-10-08 P1):'#'/'%' 编码防线 +
# UNC 家目录授权改写 ==========

def test_connect_ro_uri_forms():
    """connect_ro 的两族 URI 语义回归:
    - '#'/'%'(9b6d00a 家族):本地路径含两字符 → 照常打开读值,且不
      在 '#' 截断出的错误路径落新空库(写副作用);
    - UNC(P1 2026-10-08):USERPROFILE 为 \\\\server\\share\\... 时
      as_uri() 产出 file://server/...,授权组件=主机名被 SQLite 解析期
      拒收(invalid uri authority)→ 整源永远 0/[]。修复后改写为空授权
      + 前导 // 形态,授权错误必须消失,让位给 VFS 级 unable to open
      (真库端到端 \\localhost\\D$ 实测见修复探针;套件内用不可达服务
      器钉『授权不再挡路』这一无环境依赖的断言面)。"""
    tmp = Path(tempfile.mkdtemp(prefix="zm_uri_"))
    t0 = zsrc.today0_ms()
    try:
        # ① '#'/'%':读值正确 + 无截断副作用
        hash_dir = tmp / "a#b%c"
        hash_dir.mkdir()
        hash_db = make_db(hash_dir / "db.sqlite",
                          [(t0, "GLM-5.3", "completed", "main_turn", 111, 0, 22)])
        with _PatchedDB(hash_db):
            con = zsrc.connect_ro()
            (n,) = con.execute("SELECT COUNT(*) FROM model_usage").fetchone()
            con.close()
            check("uri:#/'%' 路径照常打开读值", n == 1, f"n={n}")
            check("uri:'#' 截断路径无新空库落盘",
                  not (hash_dir / "a").exists())
        # ② UNC:不可达服务器 —— 错误不得是 invalid uri authority
        with _PatchedDB(r"\\nonexistent-zm-server\share\db\db.sqlite"):
            try:
                zsrc.connect_ro()
                check("uri:UNC 不可达应抛 OperationalError", False, "opened?")
            except sqlite3.OperationalError as e:
                check("uri:UNC 错误为 VFS 级而非授权级(authority 不再挡路)",
                      "authority" not in str(e), str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print("== test_fault_channel_rc1_vs_empty_window_rc0 =="); test_fault_channel_rc1_vs_empty_window_rc0()
    print("== test_display_width_and_alignment ==");             test_display_width_and_alignment()
    print("== test_cache_tier_upper_bound_footnote ==");         test_cache_tier_upper_bound_footnote()
    print("== test_scan_rows_disclosure_always_present ==");     test_scan_rows_disclosure_always_present()
    print("== test_boot_queries_connection_count ==");           test_boot_queries_connection_count()
    print("== test_connect_ro_uri_forms ==");                    test_connect_ro_uri_forms()
    if FAILED:
        print(f"\nFAILED: {FAILED}")
        sys.exit(1)
    print("\nCLI TESTS ALL PASS")
