#!/usr/bin/env python3
"""zcode-meter CLI 伴侣:无界面用量速查(不启 Qt/托盘/轮询线程)。

两种等价调用形态(repo 无 pyproject/setup.py,包在 src/ 下,两种都要能跑):
  cd src && python -m zcode_meter cost --days 30    # 模块形态,cwd 必须在 src
  python src/zcode_meter/__main__.py cost --days 7  # 脚本直跑,任意 cwd 皆可

仓库根直接 `python -m zcode_meter` 不可用:根目录兼容 shim zcode_meter.py
同名遮蔽(`-m` 会执行 shim 转去启动 GUI 而非 CLI),模块形态必须 cd src
或 PYTHONPATH=src —— 本文件顶部 bootstrap 只救脚本直跑形态,救不了 -m。

数据口径与按天图表完全一致(ZCode-DB-only、completed 全部 query_source、
内置刊例价);除 import data_engine 既有的 faulthandler 追加 zm_crash.log
外,不写任何 zm_* 业务/状态文件(只读查询,读 zm_config/zm_prices)。
"""
from __future__ import annotations

import argparse
import queue
import sys
from pathlib import Path

# 脚本直跑(python src/zcode_meter/__main__.py)时 __package__ 为空:与
# app.py 顶部同款 bootstrap,补 src 进 sys.path 后按包名绝对导入。
# frozen(exe)时 PyInstaller 已内置包,无需引导。-m 形态 __package__ 非
# 空,自然跳过(此时 cwd=src 已在 sys.path,由调用方保证,见模块 docstring)。
if __package__ in (None, "") and not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # .../src

from zcode_meter.data_engine import DataEngine  # noqa: E402


def _fmt_report(rows: list, days: int) -> str:
    """把 fetch_daily_model_usage 的 (date×model) 明细聚合成按天文本报表。

    tokens 口径 = in+out(in 已含 cache_read,与 fetch_daily_usage_cost 逐字
    对齐,勿再叠加 cache);partial 裁决:仅窗口内出现未覆盖模型时才打
    『下限』注脚 —— 全已知模型的窗口金额是精确值,无条件打注脚反而是误导。
    空窗口也保留表头与口径行:CLI 输出常被重定向/贴_issue,口径行是自描述
    的一部分,恒打印。
    """
    days_agg: dict = {}
    for d, _model, in_tok, _cache, out_tok, cny, partial in rows:
        tok, c, p = days_agg.get(d, (0, 0.0, False))
        days_agg[d] = (tok + (in_tok or 0) + (out_tok or 0),
                       c + (cny or 0.0), p or bool(partial))
    # 表头币种符号必须用全角 ￥(U+FFE5):半角 ¥(U+00A5)在 GBK/cp936 下
    # 不可编码,而重定向/管道时非控制台 stdout 用 locale ANSI 编码(简中
    # Windows 缺省 cp936)—— 查询已成功、整份报表却在 print 处
    # UnicodeEncodeError 崩溃 rc=1、重定向文件 0 字节,恰好砸中 docstring
    # 自述的主用场景(输出常被重定向/贴_issue)。报表其余 CJK/符号(日期/
    # 合计/·/≈/全角￥)均实测 GBK 可编码(2026-09-30 P1)。
    lines = [f"zcode-meter 用量速查 · 近 {days} 天",
             f"{'日期':<12}{'tokens':>16}{'￥':>12}"]
    tot_tok, tot_cny = 0, 0.0
    for d in sorted(days_agg):
        tok, cny, _p = days_agg[d]
        tot_tok += tok
        tot_cny += cny
        lines.append(f"{d:<12}{tok:>16,}{cny:>12.2f}")
    if not days_agg:
        lines.append(f"{'(窗口内无 completed 用量记录)':<12}")
    lines.append("-" * 40)
    lines.append(f"{'合计':<12}{tot_tok:>16,}{tot_cny:>12.2f}")
    if any(p for _t, _c, p in days_agg.values()):
        lines.append("注:含未覆盖模型,金额为下限(≈)")
    lines.append("口径:ZCode-DB-only(completed 全部 query_source),按刊例价估算")
    return "\n".join(lines)


def main(argv=None) -> int:
    """CLI 入口:成功返回 0;参数非法打印错误并返回 2(对齐 argparse 惯例)。"""
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
    # 单测里 stdout 被换成 io.StringIO(无 reconfigure 方法),hasattr 守卫。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    # 构造不 start:DataEngine 是 threading.Thread,CLI 只做一次性只读查询,
    # 绝不起轮询线程(起线程会把进程吊住不退)。queue.Queue(maxsize=1) 是
    # 既有单测的安全构造形态,同款复用。
    eng = DataEngine(queue.Queue(maxsize=1))
    try:
        rows = eng.fetch_daily_model_usage(days)
    finally:
        eng.stop()                    # 未 start 时无害,与既有单测收尾一致
    print(_fmt_report(rows, days))
    return 0


if __name__ == "__main__":            # import zcode_meter.__main__ 不执行 CLI
    sys.exit(main())
