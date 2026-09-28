#!/usr/bin/env python3
"""布局切换压力测试:横条 <-> 竖条 <-> 卡片 多轮循环 + 动态 refit + 尺寸动态性。

历史上这里翻过的车:
- 竖条切换时 elapsed_lbl 持有已销毁旧对象(RuntimeError 崩溃)
- 固定尺寸 vs 动态尺寸(必须动态:分辨率/内容变化自愈)
"""
import os
import sys
import time
from pathlib import Path

# 位置记忆隔离:直接运行本文件时也不改写用户真实 zm_state.json
os.environ.setdefault("ZM_NO_STATE", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from PySide6.QtWidgets import QApplication  # noqa: E402

import zcode_meter.app as m  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    app = QApplication(sys.argv)
    win = m.MeterWindow()
    win._apply_snapshot(win.snap)

    sizes = []
    for i in range(4):
        win._build_bar(); win._apply_snapshot(win.snap); win._refit_dock()
        sizes.append(("h", win._bar_size(False)))
        win._build_bar(vertical=True); win._apply_snapshot(win.snap)
        sizes.append(("v", win._bar_size(True)))
        win._unset_dock()
        check(f"循环{i} 卡片恢复 dock=None", win.dock is None)

    hs = [s for k, s in sizes if k == "h"]
    vs = [s for k, s in sizes if k == "v"]
    check("横条尺寸稳定(动态重排无漂移)", all(x == hs[0] for x in hs), str(hs))
    check("竖条尺寸稳定", all(x == vs[0] for x in vs), str(vs))
    # v0.8.0 T3/F1:竖条定宽 BAR_V_W=116(max_w 聚合+cap100 机制删除,断言
    # == 保证机制不漂);横条 14pt 速度+16px sparkline 的实测上限 34(旧 9pt
    # 时代 ≤30 的档位随字号抬升,注释写明)
    check("横条高度=一行文字级(<=34 逻辑px)", hs[0][1] <= 34, str(hs[0]))
    check("竖条宽度==BAR_V_W(100,定宽机制不漂)",
          vs[0][0] == m.MeterWindow.BAR_V_W == 100, str(vs[0]))

    # 动态 refit:卡片模式应 no-op 不崩
    win._refit_dock()
    check("卡片模式 refit no-op", win.dock is None)

    # ---- v0.8.0 T3:F4 修正版属性总表(唯一权威)横/竖列 + 新段序/显隐回归 ----
    # 三形态恒建:dot(=PulseIndicator,QLabel 旧分支已删)/tps/today/burn/
    # plan/spark;横建 plan_ring(12)/plan_cd/sep_plan/sep_burn/sep_today;
    # 竖建 plan_sub/in/vbar_in_cap/vbar_burn_cap/vsep×4;旧 avg/ttft/dur/
    # elapsed/in(横)/out/rate 段随重绘退役(F4:三形态 None 或卡建)
    win._build_bar()
    check("横条构建:dot=PulseIndicator 且三形态恒建件非 None",
          isinstance(win.dot, m.PulseIndicator)
          and win._bar_form == "h"
          and all(getattr(win, a) is not None for a in
                  ("tps_lbl", "today_lbl", "burn_lbl", "plan_lbl", "spark")))
    check("横条构建:横列专属件非 None 且竖列件置 None",
          win.plan_ring is not None and win.plan_cd_lbl is not None
          and win.sep_plan is not None and win.sep_burn is not None
          and win.sep_today is not None
          and win.plan_sub_lbl is None and win.in_lbl is None
          and win.vbar_in_cap is None and win.vbar_burn_cap is None
          and win.vsep_plan is None and win.vsep_burn is None
          and win.vsep_today is None and win.vsep_in is None)
    win._build_bar(vertical=True)
    check("竖条构建:dot=PulseIndicator 且竖列专属件非 None",
          isinstance(win.dot, m.PulseIndicator)
          and win._bar_form == "v"
          and win.plan_sub_lbl is not None and win.in_lbl is not None
          and win.vbar_in_cap is not None and win.vbar_burn_cap is not None
          and all(getattr(win, a) is not None for a in
                  ("vsep_plan", "vsep_burn", "vsep_today", "vsep_in")))
    check("竖条构建:横列件置 None",
          win.plan_ring is None and win.plan_cd_lbl is None
          and win.sep_plan is None and win.sep_burn is None
          and win.sep_today is None)
    check("条形态:F4 已删件/卡列件确为 None(置 None 纪律)",
          all(getattr(win, a) is None for a in
              ("title_lbl", "est_lbl", "elapsed_lbl", "cache_lbl", "ttft_lbl",
               "dur_lbl", "out_lbl", "state_lbl", "avg_lbl", "rate_lbl",
               "model_lbl", "model_rows", "model_name_lbl", "today_cost_lbl",
               "tps_unit_lbl", "timing_lbl", "today_src_lbl", "in_out_lbl",
               "avg_burn_lbl", "plan_cap_lbl", "plan_tok_lbl", "plan_section")))

    # 注入套餐/燃速/入出后再跑双形态循环:显隐切换直接影响 _bar_size(隐藏
    # 控件须被跳过),虚胖会让高度/宽度断言与尺寸稳定断言当场翻车
    win._plan_pct = 42.0
    win._plan_left_tok = 2_100_000_000.0
    win.snap.burn_tokens_per_hour = 12345.0
    win.snap.session_in, win.snap.session_out = 1_234_567, 88
    sizes2 = []
    for i in range(4):
        win._build_bar(); win._apply_snapshot(win.snap); win._refit_dock()
        sizes2.append(("h", win._bar_size(False)))
        check(f"预算段循环{i} 横条:套餐段/燃速段可见且文案正确",
              win.plan_lbl.isVisible() and win.burn_lbl.isVisible()
              and win.plan_ring.isVisible() and win.sep_plan.isVisible()
              and win.sep_burn.isVisible()
              and win.plan_lbl.text() == "42% ~2.1B"
              and win.burn_lbl.text() == "燃速 12.3K/h"
              and not win.plan_cd_lbl.isVisible())
        win._build_bar(vertical=True); win._apply_snapshot(win.snap)
        sizes2.append(("v", win._bar_size(True)))
        check(f"预算段循环{i} 竖条:套餐组/燃速组/入出组可见且文案正确",
              win.plan_lbl.isVisible() and win.plan_lbl.text() == "42%"
              and win.plan_sub_lbl.isVisible()
              and win.plan_sub_lbl.text() == "~2.1B"
              and win.burn_lbl.isVisible() and win.burn_lbl.text() == "12.3K/h"
              and win.vbar_burn_cap.isVisible() and win.vbar_burn_cap.text() == "燃速"
              and win.in_lbl.text() == "1.2M"
              and win.vbar_in_cap.text() == "入 · 出88"
              and win.vsep_plan.isVisible() and win.vsep_burn.isVisible())
        win._unset_dock()
    hs2 = [s for k, s in sizes2 if k == "h"]
    vs2 = [s for k, s in sizes2 if k == "v"]
    check("预算段横条尺寸稳定", all(x == hs2[0] for x in hs2), str(hs2))
    check("预算段竖条尺寸稳定", all(x == vs2[0] for x in vs2), str(vs2))
    check("预算段横条高度仍<=34", hs2[0][1] <= 34, str(hs2[0]))
    check("预算段竖条宽度仍==116(定宽不随数据漂)",
          vs2[0][0] == m.MeterWindow.BAR_V_W, str(vs2[0]))

    # v0.8.0 T3 倒计时新段序:横条 plan_cd_lbl 独立段(倒计时原文,cd 缺→
    # 隐藏);竖条 k 行段列表 [~lt, cd]『 · 』join(空列表→置空但组结构保留)
    import time as _t
    win._plan_next_reset = _t.time() * 1000 + 90 * 60_000     # 90 分钟后
    win._build_bar(); win._apply_snapshot(win.snap)
    ht = win.plan_lbl.text()
    check("横条倒计时入条(plan_cd_lbl 独立段,原文空格保留)",
          ht == "42% ~2.1B" and "后重置" not in ht and "\n" not in ht
          and win.plan_cd_lbl.isVisible() and win.plan_cd_lbl.text() == "1h 30m",
          f"{ht!r}/{win.plan_cd_lbl.text()!r}")
    win._build_bar(vertical=True); win._apply_snapshot(win.snap)
    check("竖条倒计时入条(k 行 [~lt, cd] join)",
          win.plan_lbl.text() == "42%"
          and win.plan_sub_lbl.text() == "~2.1B · 1h 30m",
          f"{win.plan_lbl.text()!r}/{win.plan_sub_lbl.text()!r}")
    win._plan_left_tok = None
    win._apply_snapshot(win.snap)
    check("竖条 k 行:lt 缺 → 仅 cd 段",
          win.plan_lbl.text() == "42%" and win.plan_sub_lbl.text() == "1h 30m",
          f"{win.plan_lbl.text()!r}/{win.plan_sub_lbl.text()!r}")
    win._plan_next_reset = None
    win._apply_snapshot(win.snap)
    check("竖条 k 行:lt/cd 双缺 → 置空但组结构保留(v 行 N% 仍显)",
          win.plan_lbl.isVisible() and win.plan_lbl.text() == "42%"
          and win.plan_sub_lbl.text() == "",
          f"{win.plan_lbl.text()!r}/{win.plan_sub_lbl.text()!r}")
    win._build_bar(); win._apply_snapshot(win.snap)
    check("横条 plan_cd:cd 缺 → 隐藏(条形态隐藏非空文本纪律)",
          not win.plan_cd_lbl.isVisible() and win.plan_lbl.text() == "42%",
          f"{win.plan_lbl.text()!r}")

    # tier 三档注入(条形态 ⑧):横条 ring 色与文本色、竖条文本色==tier_color
    for pct, hexc in ((87.0, "#6ee7a8"), (40.0, "#ffd166"), (15.0, "#ff6b6b")):
        win._plan_pct = pct
        win._build_bar(); win._apply_snapshot(win.snap)
        check(f"横条 tier 注入 {pct:g}%:ring/文本色==tier_color",
              win.plan_ring._color == m.tier_color(pct) == hexc
              and win.plan_ring._pct == pct
              and win.plan_lbl.text() == f"{pct:g}%"
              and win.plan_lbl.styleSheet() == f"color: {hexc};",
              f"{win.plan_ring._color}/{win.plan_lbl.text()!r}")
        win._build_bar(vertical=True); win._apply_snapshot(win.snap)
        check(f"竖条 tier 注入 {pct:g}%:v 行文本色==tier_color",
              win.plan_lbl.text() == f"{pct:g}%"
              and win.plan_lbl.styleSheet() == f"color: {hexc};",
              f"{win.plan_lbl.text()!r}")
    win._plan_pct = None

    # sparkline 三形态恒建(⑩):<2 点隐藏不闪空,隐藏被 _bar_size 跳过
    win._build_bar(); win._apply_snapshot(win.snap)
    check("横条 sparkline:<2 点隐藏", win.spark.isHidden())
    win.snap.recent_speeds = [10.0, 20.0, 15.0, 30.0]
    win._apply_snapshot(win.snap)
    check("横条 sparkline:≥2 点可见", not win.spark.isHidden())
    win._build_bar(vertical=True); win._apply_snapshot(win.snap)
    check("竖条 sparkline:≥2 点可见", not win.spark.isHidden())
    win.snap.recent_speeds = None
    win._apply_snapshot(win.snap)
    check("竖条 sparkline:<2 点隐藏", win.spark.isHidden())

    # 横条今日段(⑦):『今X』+金额段(取整;cost=0 省金额段、partial ≈)
    win._plan_pct = None
    win.snap.today_tokens = 1_234_567
    win.snap.today_cost_cny = 12.34
    win.snap.today_cost_partial = True
    win._build_bar(); win._apply_snapshot(win.snap)
    check("横条今日段:金额段(partial ≈,金额取整)",
          win.today_lbl.text() == "今1.2M ≈¥12", win.today_lbl.text())
    win.snap.today_cost_cny = None
    win._apply_snapshot(win.snap)
    check("横条今日段:cost=0 省金额段", win.today_lbl.text() == "今1.2M",
          win.today_lbl.text())

    # 燃速段均燃(横条拼接后缀/竖条 k 行,口径与卡片一致)
    win.snap.burn_avg_tokens_per_hour = 137_000.0
    win._apply_snapshot(win.snap)
    check("横条燃速段:瞬时优先+均燃(口径不动)",
          win.burn_lbl.text() == "燃速 12.3K/h · 均燃 137.0K/h",
          win.burn_lbl.text())
    win._build_bar(vertical=True); win._apply_snapshot(win.snap)
    check("竖条燃速组:k 行含均燃",
          win.burn_lbl.text() == "12.3K/h"
          and win.vbar_burn_cap.text() == "燃速 · 均137.0K",
          f"{win.burn_lbl.text()!r}/{win.vbar_burn_cap.text()!r}")

    # 数据缺席(⑪):段/组与其前 sep/vsep 一并隐藏,无悬空线。恒显件用
    # not isHidden()(显式标志,与卡片侧断言同约定 —— isVisible 会因『加入
    # 可见父后未 show』在无事件循环的测试环境误报 False)
    win._plan_pct = None
    win.snap.burn_tokens_per_hour = 0.0
    win._build_bar(); win._apply_snapshot(win.snap)
    check("横条数据缺席:套餐段/燃速段与其前 sep 一并隐藏,sep_today 仍显",
          not win.plan_lbl.isVisible() and not win.plan_ring.isVisible()
          and not win.plan_cd_lbl.isVisible() and not win.burn_lbl.isVisible()
          and not win.sep_plan.isVisible() and not win.sep_burn.isVisible()
          and not win.sep_today.isHidden() and not win.today_lbl.isHidden())
    win._build_bar(vertical=True); win._apply_snapshot(win.snap)
    check("竖条数据缺席:套餐组/燃速组与其前 vsep 隐藏,vsep_today/vsep_in 仍显",
          not win.plan_lbl.isVisible() and not win.plan_sub_lbl.isVisible()
          and not win.burn_lbl.isVisible() and not win.vbar_burn_cap.isVisible()
          and not win.vsep_plan.isVisible() and not win.vsep_burn.isVisible()
          and not win.vsep_today.isHidden() and not win.vsep_in.isHidden()
          and not win.today_lbl.isHidden() and not win.in_lbl.isHidden())

    # ---- v0.8.0 T5+T2:卡片重绘(脉冲环/主数字/今日三段/套餐容器化/六格/模型行)----
    # F4 属性表卡列(唯一权威):恒建 dot/tps/today/burn/plan/spark;卡建
    # state/model_rows/today_src/timing/in_out/avg_burn/rate/avg 与
    # plan_cap/plan_tok/plan_sub/plan_ring;三形态全 None 的已删件
    # title/est/cache/ttft/dur(elapsed 已并入状态文本,均随结构退役)
    win._build_card(); win.dock = None
    win._apply_snapshot(win.snap)
    check("卡片构建:dot=PulseIndicator 且三形态恒建件非 None",
          isinstance(win.dot, m.PulseIndicator)
          and win.tps_lbl is not None and win.today_lbl is not None
          and win.burn_lbl is not None and win.plan_lbl is not None
          and win.spark is not None)
    check("卡片构建:属性表卡列非 None + 已删件为 None",
          win.state_lbl is not None and win.model_rows is not None
          and win.today_src_lbl is not None and win.timing_lbl is not None
          and win.in_out_lbl is not None and win.avg_burn_lbl is not None
          and win.rate_lbl is not None and win.avg_lbl is not None
          and win.plan_cap_lbl is not None and win.plan_tok_lbl is not None
          and win.plan_sub_lbl is not None and win.plan_ring is not None
          and win.title_lbl is None and win.est_lbl is None
          and win.ttft_lbl is None and win.dur_lbl is None
          and win.in_lbl is None and win.out_lbl is None)
    # 形态交叉后属性归位(F4 矩阵的循环侧:卡→横→竖 不摸已销毁对象;
    # dot 三形态均 PulseIndicator,M5 旧 QLabel 分支已随 T3 删除)
    win._build_bar()
    check("卡→横:dot=PulseIndicator(旧 QLabel 分支已删)且卡列件置 None",
          isinstance(win.dot, m.PulseIndicator)
          and win.spark is not None and win.plan_ring is not None
          and win.plan_section is None and win.state_lbl is None)
    win._build_bar(vertical=True)
    check("横→竖:plan_sub/入出组在用,plan_ring/plan_cd 置 None",
          win.plan_sub_lbl is not None and win.in_lbl is not None
          and win.vsep_plan is not None
          and win.plan_ring is None and win.plan_cd_lbl is None)
    win._build_card(); win.dock = None

    # timing 四组合(新文案『{tt} / {du}s』,缺参 --;v0.7『⏱ 首字…』随结构退役)
    win.snap.last_ttft, win.snap.last_duration = 0.8, 12.4
    win._apply_snapshot(win.snap)
    check("卡片 timing:双值", win.timing_lbl.text() == "0.8 / 12.4s",
          win.timing_lbl.text())
    win.snap.last_ttft = None
    win._apply_snapshot(win.snap)
    check("卡片 timing:缺 ttft", win.timing_lbl.text() == "-- / 12.4s",
          win.timing_lbl.text())
    win.snap.last_ttft, win.snap.last_duration = 0.8, None
    win._apply_snapshot(win.snap)
    check("卡片 timing:缺 dur", win.timing_lbl.text() == "0.8 / --",
          win.timing_lbl.text())
    win.snap.last_ttft = None
    win._apply_snapshot(win.snap)
    check("卡片 timing:双 None", win.timing_lbl.text() == "-- / --",
          win.timing_lbl.text())

    # plan 新钉死表(v0.8.0):容器化显隐 + ring 中心 tier 色 + 右信息三行;
    # r 行段序与 v0.7 D3 表相反是本轮有意变更:倒计时在前、无前导点
    win._plan_pct = 42.0
    win._plan_fetched_at = time.time() - 180
    win._plan_next_reset = time.time() * 1000 + 90 * 60_000
    win._plan_left_tok = 2_100_000_000.0
    win._apply_snapshot(win.snap)
    check("卡片 plan:段容器可见(pct 在场)+ plan_lbl 同步 42%",
          not win.plan_section.isHidden() and win.plan_lbl.text() == "42%",
          f"{win.plan_lbl.text()!r}")
    check("卡片 plan:ring pct/color == tier_color(42)",
          win.plan_ring._pct == 42.0
          and win.plan_ring._color == m.tier_color(42) == "#ffd166")
    check("卡片 plan:tok 行 + r 行新段序(cd 在前、无前导点)",
          win.plan_tok_lbl.text() == "~2.1B tokens"
          and win.plan_sub_lbl.text() == "1h 30m 后重置 · 3分钟前",
          f"{win.plan_tok_lbl.text()!r}/{win.plan_sub_lbl.text()!r}")
    # tier 三档注入:87/40/15 → ring 色与 plan_lbl 文本色同步 tier_color
    for pct, hexc in ((87.0, "#6ee7a8"), (40.0, "#ffd166"), (15.0, "#ff6b6b")):
        win._plan_pct = pct
        win._apply_snapshot(win.snap)
        check(f"卡片 tier 注入 {pct:g}%:ring/文本色==tier_color",
              win.plan_ring._color == m.tier_color(pct) == hexc
              and win.plan_ring._pct == pct
              and win.plan_lbl.text() == f"{pct:g}%"
              and win.plan_lbl.styleSheet() == f"color: {hexc};",
              f"{win.plan_ring._color}/{win.plan_lbl.text()!r}")
    # r 行省略组合:缺 cd/age 该段自然省略;lt 缺 → 占位『—』
    win._plan_pct = 42.0; win._plan_next_reset = None
    win._apply_snapshot(win.snap)
    check("卡片 plan r 行:仅 age", win.plan_sub_lbl.text() == "3分钟前",
          win.plan_sub_lbl.text())
    win._plan_next_reset = time.time() * 1000 + 90 * 60_000
    win._plan_fetched_at = None
    win._apply_snapshot(win.snap)
    check("卡片 plan r 行:仅 cd", win.plan_sub_lbl.text() == "1h 30m 后重置",
          win.plan_sub_lbl.text())
    win._plan_left_tok = None
    win._apply_snapshot(win.snap)
    check("卡片 plan:lt 缺 → 『—』占位", win.plan_tok_lbl.text() == "—",
          win.plan_tok_lbl.text())
    win._plan_pct = None
    win._apply_snapshot(win.snap)
    check("卡片 plan:pct=None → 容器整组隐藏(分节线不悬空)+ plan_lbl 空",
          win.plan_section.isHidden() and win.plan_lbl.text() == "",
          f"{win.plan_lbl.text()!r}")
    win._plan_next_reset = None
    win._plan_fetched_at = time.time() - 180
    win._plan_left_tok = 2_100_000_000.0

    # today 三段式:数字/金额/『今日』三段;cost=0 省金额段、partial ≈ 前缀、
    # >1 源才显示(口径红线逐字沿用;金额段独立 label,v0.7 单 label 退役)
    win.snap.today_tokens = 1_234_567
    win.snap.today_cost_cny = 12.34
    win.snap.today_cost_partial = True
    win.snap.today_by_source = [("ZCode", 1_234_567), ("Claude", 456_789)]
    win._apply_snapshot(win.snap)
    check("卡片 today 三段:数字/金额(partial ≈)/多源副文本",
          win.today_lbl.text() == "1.2M"
          and win.today_cost_lbl.text() == "≈¥12.34"
          and not win.today_cost_lbl.isHidden()
          # 456_789 经 fmt_k 的 :.1f 缩写为 456.8K(join 文案不变的红线)
          and win.today_src_lbl.text() == "ZCode 1.2M · Claude 456.8K",
          f"{win.today_lbl.text()!r}/{win.today_cost_lbl.text()!r}"
          f"/{win.today_src_lbl.text()!r}")
    win.snap.today_cost_cny = None
    win.snap.today_by_source = [("ZCode", 1_234_567)]
    win._apply_snapshot(win.snap)
    check("卡片 today:cost=0 省金额段 + ≤1 源置空",
          win.today_lbl.text() == "1.2M" and win.today_cost_lbl.isHidden()
          and win.today_src_lbl.text() == "",
          f"{win.today_lbl.text()!r}/{win.today_src_lbl.text()!r}")

    # 状态行/主数字行:elapsed 并入状态文本、流式估算以速度 ~ 前缀表达
    # (F3 裁决:est_lbl/elapsed_lbl 删除,信息并入,tooltip 补偿);
    # sparkline <2 点隐藏不闪空(T-4 数据缺席口径)
    win.snap.state = "generating"
    win.snap.tps_est = 24.3
    win.snap.gen_elapsed = 12.0
    win.snap.recent_speeds = None
    win._apply_snapshot(win.snap)
    check("卡片状态行:生成中+elapsed 并入;速度 ~ 前缀流式估算",
          win.state_lbl.text() == "生成中 12s" and win.tps_lbl.text() == "~24.3",
          f"{win.state_lbl.text()!r}/{win.tps_lbl.text()!r}")
    check("卡片 sparkline:<2 点隐藏", win.spark.isHidden())
    win.snap.recent_speeds = [10.0, 20.0, 15.0, 30.0]
    win._apply_snapshot(win.snap)
    check("卡片 sparkline:≥2 点可见", not win.spark.isHidden())
    win.snap.state = "idle"; win.snap.tps_est = None
    win._apply_snapshot(win.snap)
    check("卡片状态行:idle『空闲』", win.state_lbl.text() == "空闲",
          win.state_lbl.text())

    # grid 六格/入出格:入/出直取 session_in/out(禁碰 session_cache,口径
    # 红线 v0.2.0);燃速格 tooltip 承接 est_hours_left(F3 补偿);
    # 模型行 rows[:4] 封顶、首行不带『均』前缀(v0.7→v0.8 有意变更)
    win.snap.last_ttft, win.snap.last_duration = 0.8, 12.4   # 复位:四组合块尾态是双 None
    win.snap.session_in, win.snap.session_out = 1_234_567, 88
    win.snap.cache_rate = 97.8
    win.snap.burn_tokens_per_hour = 12_345.0
    win.snap.burn_avg_tokens_per_hour = 137_000.0
    win.snap.tps_avg = 69.6
    win.snap.est_hours_left = 3.2
    win.snap.speed_by_model = [
        ("bigmodel", "glm-5.3-flash", 12.3, 1000),
        ("builtin", "glm-5.3-flash", 44.0, 2000)]
    win._apply_snapshot(win.snap)
    check("卡片 grid 六格:入/出、缓存、timing、燃速、均燃、均速",
          win.in_out_lbl.text() == "1.2M / 88"
          and win.rate_lbl.text() == "97.80%"
          and win.timing_lbl.text() == "0.8 / 12.4s"
          and win.burn_lbl.text() == "12.3K/h"
          and win.avg_burn_lbl.text() == "137.0K/h"
          and win.avg_lbl.text() == "69.6 t/s",
          f"{win.in_out_lbl.text()!r}/{win.rate_lbl.text()!r}"
          f"/{win.timing_lbl.text()!r}/{win.burn_lbl.text()!r}"
          f"/{win.avg_burn_lbl.text()!r}/{win.avg_lbl.text()!r}")
    check("卡片燃速格:est_hours_left 降级 tooltip(全量+预估)",
          win.burn_lbl.toolTip() == "12.3K/h · 预算还可撑 3.2h",
          win.burn_lbl.toolTip())
    _nm0, _sp0 = win._model_rows_items[0]
    _nm3, _sp3 = win._model_rows_items[3]
    check("卡片模型行:行内容(无『均』前缀)+ 超出 rows[:4] 隐藏",
          _nm0.text() == "bigmodel / glm-5.3-flash" and _sp0.text() == "12.3 t/s"
          and _nm3.isHidden() and _sp3.isHidden(),
          f"{_nm0.text()!r}/{_sp0.text()!r}")

    # ---- 数据新鲜度视觉化:贴边条形态 + 套餐数据龄超 15min → 半透明 ----
    # 纯函数边界用整值参数(100.0/1000.0):拿浮点时刻做『恰 900』断言会有
    # 舍入噪声;窗口侧只注入 dock/_plan_fetched_at 两状态,不启 monitor
    check("freshness 纯函数:龄恰 900s 不冻结(严格大于)",
          m.frozen_opacity("top", 100.0, now=1000.0) == 1.0)
    check("freshness 纯函数:龄 900.5s 冻结",
          m.frozen_opacity("top", 100.0, now=1000.5) == 0.8)
    check("freshness 纯函数:quota 未配置(fetched_at 缺/0)恒 1.0",
          m.frozen_opacity("bottom", None) == 1.0
          and m.frozen_opacity("bottom", 0) == 1.0)
    win.dock = "bottom"
    win._plan_fetched_at = time.time() - 1200
    win._apply_freshness()
    check("贴边+数据龄20min → 窗口透明度0.8",
          abs(win.windowOpacity() - 0.8) < 1e-9)
    win._plan_fetched_at = time.time()
    win._apply_freshness()
    check("贴边+数据新鲜 → 透明度回 1.0",
          abs(win.windowOpacity() - 1.0) < 1e-9)
    win.dock = None
    win._plan_fetched_at = time.time() - 1200
    win._apply_freshness()
    check("卡片形态恒 1.0(数据再旧也不冻结)",
          abs(win.windowOpacity() - 1.0) < 1e-9)
    # 值缓存:同值重复调用不得再 setWindowOpacity(_poll_queue 200ms 一跳,
    # 防重绘 churn 的钉死断言)—— 实例属性临时遮蔽真方法,记录调用序列
    set_calls = []
    _orig_set_op = win.setWindowOpacity
    win.setWindowOpacity = set_calls.append
    win._apply_freshness()                       # 1.0 == 缓存 → 不 set
    win.dock = "bottom"; win._plan_fetched_at = time.time() - 1200
    win._apply_freshness()                       # 0.8 != 1.0 → set 一次
    win._apply_freshness()                       # 0.8 == 缓存 → 不 set
    win.setWindowOpacity = _orig_set_op
    check("值缓存:仅变化时 setWindowOpacity", set_calls == [0.8])
    # 复位注入态:close 前回到不透明、卡片态
    win.dock = None; win._plan_fetched_at = None
    win._apply_freshness()

    # ---- v0.8.0 T1:tier_color 三档/边界 + F2 色常量 hex 可解析守卫 ----
    # 20 与默认告警首阈值 alert_pct=[20,10] 巧合对齐但刻意不动态绑定
    # (分档色是视觉语言非告警状态);BarChart 直接 QColor(C_*) 消费且
    # 历史窗零测试覆盖 —— rgba 字面实测 isValid()==False 会静默画黑,
    # 故九个 C_* 常量钉死 hex,这里给出唯一机器可查的守卫
    check("tier_color 纯函数:87% → OK 档",
          m.tier_color(87) == "#6ee7a8", m.tier_color(87))
    check("tier_color 纯函数:50.0 边界(含)→ WARN",
          m.tier_color(50.0) == "#ffd166", m.tier_color(50.0))
    check("tier_color 纯函数:20.0 边界(含)→ WARN",
          m.tier_color(20.0) == "#ffd166", m.tier_color(20.0))
    check("tier_color 纯函数:19.9 → DANGER",
          m.tier_color(19.9) == "#ff6b6b", m.tier_color(19.9))
    check("F2:C_* 色常量均 QColor 可解析(hex)",
          all(QColor(c).isValid() for c in
              (m.C_FG, m.C_DIM, m.C_BG, m.C_BORDER, m.C_ACCENT, m.C_WARN,
               m.C_ACCENT_DIM, m.C_TIER_OK, m.C_TIER_DANGER)))

    win.close()
    if FAILED:
        print(f"\nFAILED: {FAILED}")
        return 1
    print("\nLAYOUT STRESS TESTS ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
