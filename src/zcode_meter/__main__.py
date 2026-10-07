#!/usr/bin/env python3
"""zcode-meter CLI 伴侣:无界面用量速查(不启 Qt/托盘/轮询线程)。

两种等价调用形态(repo 无 pyproject/setup.py,包在 src/ 下,两种都要能跑):
  cd src && python -m zcode_meter cost --days 30    # 模块形态,cwd 必须在 src
  python src/zcode_meter/__main__.py cost --days 7  # 脚本直跑,任意 cwd 皆可

仓库根直接 `python -m zcode_meter` 不可用:根目录兼容 shim zcode_meter.py
同名遮蔽(`-m` 会执行 shim 转去启动 GUI 而非 CLI)。且 `python -m` 把 cwd
置于 sys.path 首位、先于 PYTHONPATH 解析 —— 从仓库根执行时,即便设了
PYTHONPATH=src 也仍会命中根 shim(先于 PYTHONPATH 里的 src/ 被找到),
PYTHONPATH 不是 -m 模块形态的合法替代,-m 必须 cd src(与 README CLI 节
同文)。本文件顶部 bootstrap 只救脚本直跑形态,救不了 -m。

数据口径与按天图表完全一致(ZCode-DB-only、completed 全部 query_source、
内置刊例价);故障通道两层(#4 探针 + #64b 查询侧):查询前探测 ZCode 库
缺失/打不开,以及探针过闸后查询侧 raise_on_error 上抛的 sqlite3.Error
(0 字节/异构空库的 no such table、探针与查询两次独立连接间的锁/删除
竞态),一律以 stderr 报错(含路径与异常类)并以 rc=1 结束 —— 库故障与
『窗口内无 completed 用量记录』的真空窗按退出码区分,rc=0 专属『库可读
且可查』。除 import data_engine 既有的 faulthandler 追加 zm_crash.log 外,
不写任何 zm_* 业务/状态文件(只读查询,读 zm_config/zm_prices)。
"""
from __future__ import annotations

import argparse
import os
import queue
import sqlite3
import sys
import unicodedata
from pathlib import Path

# 脚本直跑(python src/zcode_meter/__main__.py)时 __package__ 为空:与
# app.py 顶部同款 bootstrap,补 src 进 sys.path 后按包名绝对导入。
# frozen(exe)时 PyInstaller 已内置包,无需引导。-m 形态 __package__ 非
# 空,自然跳过(此时 cwd=src 已在 sys.path,由调用方保证,见模块 docstring)。
if __package__ in (None, "") and not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # .../src

# _is_num 是私有名但刻意同源导入(不本地镜像):_cache_tier_missing 的分类
# 语义是 cost_of 规则 1 的入口判定镜像 —— 判定若与 cost_of 漂移,上限注脚
# 会标错模型族;数值判定的『bool 是 int 子类须显式排除』口径留在唯一定义处。
from zcode_meter.data_engine import (DataEngine, MAX_SCAN_ROWS, _is_num)
# 故障通道探针/上限注脚都不该读 import 时刻的值拷贝:#41/#62 单点 patch 契约
# —— DB_PATH/connect_ro 的唯一定义在 sources/zcode,monkeypatch zsrc 命名
# space 才生效(connect_ro 函数体读的正是 zsrc 模块全局;经 de 再导出的是
# 函数对象/值的拷贝绑定)。探针与引擎的报表查询天然指向同一库文件。
from zcode_meter.sources import zcode as zsrc


def _disp_width(s: str) -> int:
    """s 在等宽终端里的显示宽度(#6):CJK/全角=2、ASCII/半角=1。

    表头/合计行的『日期/合计/￥』等 CJK 单元格若按字符数补位,恰比 ASCII
    数据列宽出 1 个 CJK 字宽(≈2 列)—— tokens/￥ 列头与数据列错位的来源。
    东亚宽度取 'W'(宽)与 'F'(全角,如全角￥ U+FFE5)按 2 计;'A'(歧义,
    如 ≈/·)按 1 计 —— 它们只出现在注脚/口径行,不进对齐列,Windows cp936
    控制台实测窄 1 计即可;其余(Na/N/H)一律 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in s)


def _pad_r(s: str, w: int, fill: str = " ") -> str:
    """左对齐补位到显示宽度 w(右补 fill,按显示宽度差额;超宽不截断)。"""
    return s + fill * max(0, w - _disp_width(s))


def _pad_l(s: str, w: int, fill: str = " ") -> str:
    """右对齐补位到显示宽度 w(左补 fill)—— 数值列头与 ASCII 数据列同边
    对齐的关键:tokens/￥ 列头与当日数据按右边缘对齐。"""
    return fill * max(0, w - _disp_width(s)) + s


def _cache_tier_missing(prices: dict, model: str) -> bool:
    """model 是否为『在价格表内、in/out 档齐备、但缺 in_cache 档』的形态
    (#45:cost_of 规则 1 —— cache_read 按输入全价计,金额为上限)。

    分类刻意与 cost_of 的入口判定同形、逐字段镜像:不在表内、或表内但
    in/out 必备档缺失的,cost_of 同样按未知模型置 partial(¥0、下限注脚族),
    本函数一律不认 —— 两族注脚分列,检测面收窄到规则 1 恰好生效的形态。
    与 cost_of 的口径一致性由 test_cli.py 的交叉对账钉死(镜像不漂)。"""
    p = prices.get(model) if isinstance(prices, dict) else None
    if not isinstance(p, dict):
        return False                    # 不在表内 → partial 族,由 cost_of 置 partial
    p_in, p_out = p.get("in"), p.get("out")
    if not _is_num(p_in) or not _is_num(p_out):
        return False                    # 表内但必备档缺失 → cost_of 同样按未知模型计 ¥0
    return not _is_num(p.get("in_cache"))


def _fmt_report(rows: list, days: int, prices: "dict | None" = None) -> str:
    """把 fetch_daily_model_usage 的 (date×model) 明细聚合成按天文本报表。

    tokens 口径 = in+out(in 已含 cache_read,与 fetch_daily_usage_cost 逐字
    对齐,勿再叠加 cache);金额注脚有两种、分列打印(条件性同款裁决:
    仅窗口内确实出现对应形态时才打 —— 全已知模型且金额精确时无条件打注脚
    反而是误导):
    - 下限:窗口内出现价格表未覆盖的模型(cost_of 置 partial,未知模型计
      ¥0)→『注:含未覆盖模型,金额为下限(≈)』;
    - 上限(#45,只加标记不改计算):窗口内出现『在价格表内、in/out 档
      齐备、但缺 in_cache 档』的模型且其确有 cache_read 行 → cost_of 规则 1
      对 cache 按输入全价计(宁可高估,计算一字不动)→『注:部分模型缺
      缓存档价格,cache 按输入全价计,金额为上限(≈)』。零 cache_read 的
      缺档模型金额不受规则 1 影响,不触发。检测用 prices(缺省 None 时跳过
      —— 如既有单测注入的 FakeEngine 无 prices 属性,行为退化到只看
      partial 族,既有断言不受影响)。
    对齐(#6):表头/合计行的 CJK 按显示宽度补位(_pad_r/_pad_l,基于
    _disp_width:CJK/全角=2、ASCII=1)—— f-string 的 :<12/:>16 按字符数
    补位,CJK 单元格恰比 ASCII 数据列宽出 1 个 CJK 字宽(≈2 列),tokens/￥
    列头与数据列因此错位;补位后三列(12/16/12)显示宽度恒 40,与分隔线
    "-"*40 对齐。
    空窗口也保留表头与口径行:CLI 输出常被重定向/贴_issue,口径行是自描述
    的一部分,恒打印(含 MAX_SCAN_ROWS 扫描闸披露,与 README 同文)。
    """
    days_agg: dict = {}
    for d, _model, in_tok, cache, out_tok, cny, partial in rows:
        tok, c, p, cap = days_agg.get(d, (0, 0.0, False, False))
        # 上限注脚检测(cache>0 的精确性条件:cache_read=0 时规则 1 不产生
        # 任何高估,金额是精确值 —— 与下限注脚『仅窗口内出现未覆盖模型时
        # 才打』同款条件性裁决)
        cap_row = (prices is not None and (cache or 0) > 0
                   and _cache_tier_missing(prices, _model))
        days_agg[d] = (tok + (in_tok or 0) + (out_tok or 0),
                       c + (cny or 0.0), p or bool(partial), cap or cap_row)
    # 表头币种符号必须用全角 ￥(U+FFE5):半角 ¥(U+00A5)在 GBK/cp936 下
    # 不可编码,而重定向/管道时非控制台 stdout 用 locale ANSI 编码(简中
    # Windows 缺省 cp936)—— 查询已成功、整份报表却在 print 处
    # UnicodeEncodeError 崩溃 rc=1、重定向文件 0 字节,恰好砸中 docstring
    # 自述的主用场景(输出常被重定向/贴_issue)。报表其余 CJK/符号(日期/
    # 合计/·/≈/全角￥)均实测 GBK 可编码(2026-09-30 P1)。
    lines = [f"zcode-meter 用量速查 · 近 {days} 天",
             _pad_r("日期", 12) + _pad_l("tokens", 16) + _pad_l("￥", 12)]
    tot_tok, tot_cny = 0, 0.0
    for d in sorted(days_agg):
        tok, cny, _p, _cap = days_agg[d]
        tot_tok += tok
        tot_cny += cny
        lines.append(_pad_r(d, 12) + _pad_l(f"{tok:,}", 16)
                     + _pad_l(f"{cny:.2f}", 12))
    if not days_agg:
        lines.append(_pad_r("(窗口内无 completed 用量记录)", 12))
    lines.append("-" * 40)
    lines.append(_pad_r("合计", 12) + _pad_l(f"{tot_tok:,}", 16)
                 + _pad_l(f"{tot_cny:.2f}", 12))
    if any(p for _t, _c, p, _cap in days_agg.values()):
        lines.append("注:含未覆盖模型,金额为下限(≈)")
    if any(cap for _t, _c, _p, cap in days_agg.values()):
        lines.append("注:部分模型缺缓存档价格,cache 按输入全价计,金额为上限(≈)")
    # 口径行披露 MAX_SCAN_ROWS 行闸(#5,预置裁决 1:恒真陈述只加披露,
    # 闸本体不动 —— 更早记录不计入,--days>实际覆盖范围时合计是无声下限,
    # 披露让『近 N 天』的截断可预期);数字取 MAX_SCAN_ROWS 常量派生
    # ("100,000"),常量若调整披露随之自洽,不产生第二处硬编码。
    lines.append(f"口径:ZCode-DB-only(completed 全部 query_source),按刊例价估算;"
                 f"历史聚合仅统计最近 {MAX_SCAN_ROWS:,} 行,更早记录不计")
    return "\n".join(lines)


def _probe_db() -> "str | None":
    """ZCode 库可读性探测(#4 故障通道):返回 None=可读;非 None=stderr
    就绪的单行故障文案(以 "error:" 开头,含库路径与异常类)。

    探测形状(#4 预置裁决,与 README 故障通道条款同文):DB_PATH 缺失 →
    明确的『不存在』文案(不伪装成 SQLite 异常);存在但 connect_ro+
    SELECT 1 探针抛 sqlite3.Error → 含异常类与消息的『不可读』文案 ——
    sqlite3.connect 惰性校验,坏库(非 SQLite 文件/占用锁死/权限)的错误
    在 execute 处才浮出,SELECT 1 恰好触发它。已知边界:合法 SQLite 但无
    model_usage 表的文件(如 0 字节空库/异构库)探针放行 —— SELECT 1 在
    合法空库上恒过闸,『不是 ZCode 库』不属本探针判定的故障形态;该形态
    由 main() 查询侧故障通道收口(fetch_daily_model_usage 的
    raise_on_error=True 把 no such table: model_usage 上抛 → rc=1,见
    main 内注释)。读 zsrc.DB_PATH/zsrc.connect_ro(调用时属性查找,单点
    patch 契约 #41/#62)—— 与引擎的报表查询天然指向同一库文件,不存在
    『探针查 A、报表查 B』的撕裂。"""
    if not os.path.exists(zsrc.DB_PATH):
        return f"error: 无法生成用量报表:ZCode 用量库不存在:{zsrc.DB_PATH}"
    try:
        con = zsrc.connect_ro()
        try:
            con.execute("SELECT 1").fetchone()
        finally:
            con.close()                 # 先查后关纪律:close 在 finally,查询在 try
    except sqlite3.Error as e:
        return (f"error: 无法生成用量报表:ZCode 用量库不可读:{zsrc.DB_PATH}"
                f"({type(e).__name__}: {e})")
    return None


def main(argv=None) -> int:
    """CLI 入口:成功返回 0;参数非法打印错误并返回 2(对齐 argparse 惯例);
    ZCode 库缺失/打不开/查询失败返回 1(stderr 报错,#4 探针 + #64b 查询侧
    故障通道)。"""
    parser = argparse.ArgumentParser(
        prog="zcode-meter", description="zcode-meter 无界面用量速查")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_cost = sub.add_parser("cost", help="按天用量与金额报表")
    p_cost.add_argument("--days", type=int, default=30,
                        help="统计窗口天数(默认 30,范围 1..366,超界截断)")
    try:
        # argparse 的 usage 错误走 SystemExit(2):捕获转返回值,保 main()
        # 恒返回 int(单测免捕获异常;真跑时 sys.exit(main()) 语义不变)
        args = parser.parse_args(list(sys.argv[1:]) if argv is None else list(argv))
    except SystemExit as e:
        return int(e.code or 0)
    # clamp 而非报错:窗口参数是便利旋钮,0/400 这类手滑值截到边界比拒跑更友好;
    # 真正的非法(非整数)已在 type=int 处拦下 rc=2
    days = max(1, min(366, args.days))

    # 输出层兜底(与表头全角 ￥ 双保险):重定向/管道时非控制台 stdout 按
    # locale ANSI 编码(cp936)严格模式工作,未来任何不可编码字符都会让
    # print 抛 UnicodeEncodeError —— 查询成功而报表整体丢失。errors=
    # 'replace' 把最坏情况降级为个别字符变 '?',报表结构/数字/口径行恒全。
    # stderr 同款(#4 故障文案含用户库路径,路径里的奇异字符不能让故障通道
    # 自身崩掉)。单测里 stdout/stderr 被换成 io.StringIO(无 reconfigure
    # 方法),hasattr 守卫。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")

    # 故障通道(#4):查询前探测 —— 缺失/打不开(非 SQLite 文件、占用锁死
    # 等)rc=1+stderr 报错(含路径与异常类),不再以『窗口内无 completed
    # 用量记录』的 rc=0 空报表伪装成空窗口:脚本化调用里故障与真空窗只能
    # 靠退出码区分,rc=0 必须专属于『库可读可查』(哪怕窗口真为空)。注意
    # 0 字节文件【不】在本探针判定面内 —— SQLite 把 0 字节视为合法空库,
    # SELECT 1 恒过闸;它由下方查询侧故障通道按 no such table 收口(旧注释
    # 曾把『0 字节损坏盘』列为探针形态,与实测行为相反,2026-10-07 修正)。
    fault = _probe_db()
    if fault is not None:
        print(fault, file=sys.stderr)
        return 1

    # 构造不 start:DataEngine 是 threading.Thread,CLI 只做一次性只读查询,
    # 绝不起轮询线程(起线程会把进程吊住不退)。queue.Queue(maxsize=1) 是
    # 既有单测的安全构造形态,同款复用。boot_queries=False(#84b):一次性
    # 只读报表不消费 _latest_session/_max_usage_rowid/_max_completed_rowid
    # 三个启动预热查询(它们的消费者全在引擎线程,不 start 即用不到)——
    # 冷启在探针之外恰好只付报表查询本身那 1 次连接;DB 缺失时 _latest_
    # session 兜底的 ROLL_DIR 全目录扫描也随之消失(GUI 默认 True 路径不受
    # 影响,run() 序幕 _ensure_boot_state 补齐)。
    eng = DataEngine(queue.Queue(maxsize=1), boot_queries=False)
    try:
        # 查询侧故障通道(#64b → CLI,2026-10-07):raise_on_error=True 让
        # 探针过闸后【查询连接自身】的 sqlite3.Error 原样上抛,而非被
        # data_engine 缺省吞错返 [] —— 探针与报表查询是两次独立 connect_ro,
        # 其间的锁死/删除竞态窗口真实存在;0 字节/异构空库更是探针
        # (SELECT 1)恒放行、查询(no such table: model_usage)必炸的形态。
        # 缺省吞错曾让这三类故障全部伪装成 rc=0 空窗报表(stderr 静默),
        # 击穿上文『rc=0 专属库可读』契约 —— GUI 导出 CSV(app._export_csv)
        # 已用同款参数+except,这里对齐。错误文案与 _probe_db 逐字同形
        # (含路径与异常类),脚本化调用按 rc=1 单一信号分道即可。
        try:
            rows = eng.fetch_daily_model_usage(days, raise_on_error=True)
        except sqlite3.Error as e:
            print(f"error: 无法生成用量报表:ZCode 用量库不可读:"
                  f"{zsrc.DB_PATH}({type(e).__name__}: {e})", file=sys.stderr)
            return 1
    finally:
        eng.stop()                    # 未 start 时无害,与既有单测收尾一致
    # prices 透传上限注脚检测(#45);getattr 缺省 None 容忍注入式单测的
    # FakeEngine(无 prices 属性 → 检测跳过,只看 partial 族)
    print(_fmt_report(rows, days, getattr(eng, "prices", None)))
    return 0


if __name__ == "__main__":            # import zcode_meter.__main__ 不执行 CLI
    sys.exit(main())
