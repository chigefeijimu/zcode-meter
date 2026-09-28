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
from PySide6.QtWidgets import QApplication, QMenu  # noqa: E402

import zcode_meter.app as m  # noqa: E402
import zcode_meter.data_engine as de  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    app = QApplication(sys.argv)
    win = m.MeterWindow()
    # ---- v0.9 T3:钉基线 setup 行升级为 _apply_skin(T2 期原语退役) ----
    # load_config 无守卫直读用户真实 zm_config.json(ZM_NO_STATE 只拦写不拦
    # 读),T1 落地后用户若配了非 glass 皮肤,窗口字族/尺寸会随皮肤漂,既有
    # 断言翻车。这里显式钉回 glass;persist=False 走与用户右键切换同一条
    # 即时重建路径(卡=防钳宽纪律),但不落盘。setup 行允许演进,下方断言
    # 一字不改(断言=check() 调用,setup=其前的窗口准备)。
    win._apply_skin("glass", persist=False)

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

    # ---- 段可关(v0.8.0 对版期『靠边停放自定义显示内容』) ----
    # 横条关三段:速度+今日仍显,套餐/倒计时/燃速件随 sep 未建(None)。
    # 宽度收敛需满载数据(空快照下 spark/套餐/燃速本就隐藏,关段前后同宽)
    win.bar_segments = {"h": ["today"], "v": ["today", "plan", "burn", "in"]}
    win._build_bar()
    win._apply_snapshot(win.snap)
    hseg = win._bar_size(False)
    check("横条关段:plan_ring/plan_cd/burn 未建(None)",
          win.plan_ring is None and win.plan_cd_lbl is None
          and win.burn_lbl is None and win.sep_plan is None,
          f"{win.plan_ring}/{win.plan_cd_lbl}/{win.burn_lbl}")
    check("横条关段:速度+今日仍显", win.tps_lbl is not None
          and win.today_lbl is not None, "")
    _saved = win.snap
    win.snap = m.Snapshot(
        state="idle", model="GLM-5.3", title="t", tps_exact=137.9,
        today_tokens=1_200_000_000, today_cost_cny=2489.0,
        today_by_source=[("ZCode", 1_200_000_000)], plan_remaining_pct=87.0,
        burn_tokens_per_hour=31_300_000, burn_avg_tokens_per_hour=129_700_000,
        session_in=448_000_000, session_out=624_000,
        recent_speeds=[20, 35, 28, 44, 30, 52, 38],
        speed_by_model=[("bigmodel-api", "GLM-5.3", 56.8, 0)])
    win._plan_pct = 87.0
    win._apply_snapshot(win.snap)
    hseg_full_data = win._bar_size(False)
    win.bar_segments = {"h": list(m.BAR_SEGMENTS_H), "v": list(m.BAR_SEGMENTS_V)}
    win._build_bar()
    win._apply_snapshot(win.snap)
    hfull_data = win._bar_size(False)
    check("横条关段:满载数据下宽度收敛(<全开宽)",
          hseg_full_data[0] < hfull_data[0],
          f"seg {hseg_full_data} vs full {hfull_data}")
    win.snap = _saved
    win.bar_segments = {"h": ["today"], "v": ["today", "plan", "burn", "in"]}
    win._build_bar()
    win._apply_snapshot(win.snap)
    check("横条关段:尺寸稳定(重建两次同宽)",
          win._bar_size(False) == hseg, f"{win._bar_size(False)} vs {hseg}")
    # 竖条只关会话入出:其余段照旧
    win.bar_segments = {"h": ["spark", "plan", "cd", "today", "burn"],
                        "v": ["spark", "today", "plan", "burn"]}
    win._build_bar(vertical=True)
    win._apply_snapshot(win.snap)
    check("竖条关段:in_lbl/vbar_in_cap/vsep_in 未建,套餐组正常",
          win.in_lbl is None and win.vbar_in_cap is None
          and win.vsep_in is None and win.plan_lbl is not None
          and win.plan_sub_lbl is not None,
          f"{win.in_lbl}/{win.plan_lbl}")
    check("竖条关段:速度恒显", win.tps_lbl is not None, "")
    # 恢复全开(后续断言依赖全建状态)
    win.bar_segments = {"h": list(m.BAR_SEGMENTS_H),
                        "v": list(m.BAR_SEGMENTS_V)}
    win._unset_dock()

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
    check("横条倒计时入条(plan_cd_lbl 独立段,条形态紧凑无空格)",
          ht == "42% ~2.1B" and "后重置" not in ht and "\n" not in ht
          and win.plan_cd_lbl.isVisible() and win.plan_cd_lbl.text() == "1h30m",
          f"{ht!r}/{win.plan_cd_lbl.text()!r}")
    win._build_bar(vertical=True); win._apply_snapshot(win.snap)
    check("竖条倒计时入条(k 行 [~lt, cd] join)",
          win.plan_lbl.text() == "42%"
          and win.plan_sub_lbl.text() == "~2.1B · 1h30m",
          f"{win.plan_lbl.text()!r}/{win.plan_sub_lbl.text()!r}")
    win._plan_left_tok = None
    win._apply_snapshot(win.snap)
    check("竖条 k 行:lt 缺 → 仅 cd 段",
          win.plan_lbl.text() == "42%" and win.plan_sub_lbl.text() == "1h30m",
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
          win.today_lbl.text() == "今 1.2M ≈¥12", win.today_lbl.text())
    win.snap.today_cost_cny = None
    win._apply_snapshot(win.snap)
    check("横条今日段:cost=0 省金额段", win.today_lbl.text() == "今 1.2M",
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
    win.snap.global_tps = 24.3
    win.snap.gen_elapsed = 12.0
    win.snap.recent_speeds = None
    win._apply_snapshot(win.snap)
    check("卡片状态行:生成中+elapsed 并入;主数字=全局吞吐恒带 ~",
          win.state_lbl.text() == "生成中 12s" and win.tps_lbl.text() == "~24.3",
          f"{win.state_lbl.text()!r}/{win.tps_lbl.text()!r}")
    win.snap.global_tps = 0.0
    win._apply_snapshot(win.snap)
    check("卡片主数字:全局吞吐 0 → 空闲 --",
          win.tps_lbl.text() == "--", win.tps_lbl.text())
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

    # ==== v0.9 T2 皮肤基建:glass 逐位不变 + 九款骨架 + registry 回退 ====
    # ① glass qss/qss_bar 逐位相等:skins 由统一模板渲染,glass palette
    #   镜像 C_* 常量 —— 两侧任一漂移此断言当场翻车(F2 守卫的皮肤版)
    check("皮肤:glass qss/qss_bar 与 m.QSS/m.QSS_BAR 逐位相等",
          m.skins.REGISTRY["glass"].qss == m.QSS
          and m.skins.REGISTRY["glass"].qss_bar == m.QSS_BAR)
    check("皮肤:glass today_cost_sep 恒单空格(今日段文案逐位口径)",
          m.skins.REGISTRY["glass"].today_cost_sep == " ")
    check("皮肤:registry 九款齐全且键序==SKIN_IDS(T1 白名单同源)",
          tuple(m.skins.REGISTRY.keys()) == m.skins.SKIN_IDS
          and m.skins.SKIN_IDS[0] == "glass")
    # ② 九皮肤 palette 守卫(F2 扩展):QSS 侧四档(soft/half/vstrong/sep)
    #   允许 rgba 字面 —— 前三档是 N2 半透明文字档,sep 是 QFrame#sep 的
    #   QSS 背景(T4 起为半透明,叠装饰底做细线);rgba 只活在 QSS、不进
    #   QColor(str)(本机 isValid()==False 实测,app.py:78-82 同源结论),
    #   其余键必须 QColor 可解析 hex(绘制件消费侧红线)
    check("皮肤:九款 palette 非rgba键全 QColor 可解析(soft/half/vstrong/sep 四档 QSS 侧豁免)",
          all(QColor(v).isValid()
              for sk in m.skins.REGISTRY.values()
              for k, v in sk.palette.items()
              if k not in ("soft", "half", "vstrong", "sep")))
    check("皮肤:九款 palette rgba 豁免键恒 rgba( 字面(F2 纪律,soft/half/vstrong)",
          all(str(v).startswith("rgba(")
              for sk in m.skins.REGISTRY.values()
              for k, v in sk.palette.items()
              if k in ("soft", "half", "vstrong")))
    # sep 双形合法:glass 恒 hex(镜像 C_BORDER 逐位红线),八款新皮肤允许
    # rgba 半透明(QSS 背景字面)—— 只验『QSS 可消费』,不强制哪一种
    check("皮肤:九款 palette sep 均为 hex 或 rgba( 字面(QSS 可消费)",
          all(str(v).startswith("rgba(") or QColor(v).isValid()
              for sk in m.skins.REGISTRY.values()
              for k, v in sk.palette.items() if k == "sep"))
    check("皮肤:glass palette 镜像 C_*(fg/dim/accent/warn/sep 防漂)",
          m.skins.REGISTRY["glass"].palette["fg"] == m.C_FG
          and m.skins.REGISTRY["glass"].palette["dim"] == m.C_DIM
          and m.skins.REGISTRY["glass"].palette["accent"] == m.C_ACCENT
          and m.skins.REGISTRY["glass"].palette["warn"] == m.C_WARN
          and m.skins.REGISTRY["glass"].palette["sep"] == m.C_BORDER)
    check("皮肤:glass 绘制侧色镜像 C_*/白α 常量(spark/pulse/ring 零漂)",
          m.skins.REGISTRY["glass"].spark_line == QColor(m.C_ACCENT_DIM)
          and m.skins.REGISTRY["glass"].spark_dot == QColor(m.C_ACCENT)
          and m.skins.REGISTRY["glass"].pulse_idle == QColor(255, 255, 255, 89)
          and m.skins.REGISTRY["glass"].pulse_active == QColor(m.C_ACCENT_DIM)
          and m.skins.REGISTRY["glass"].pulse_core == QColor(96, 205, 255, 230)
          and m.skins.REGISTRY["glass"].ring_base == QColor(255, 255, 255, 26))
    # 骨架结构:九款 SkinDef 字段齐全,三键 radius/contrast_bg 形状恒定
    check("皮肤:九款骨架字段齐全(radius/contrast_bg 三键+色字段)",
          all(all(hasattr(sk, a) for a in
                  ("id", "menu_label", "palette", "radius", "spark_line",
                   "spark_dot", "pulse_idle", "pulse_active", "pulse_core",
                   "ring_base", "sep", "sep_card", "sep_card_weak",
                   "contrast_bg", "today_cost_sep", "font_mono_families",
                   "font_decor", "deco", "qss", "qss_bar"))
              and all(k in sk.radius for k in ("card", "h", "v"))
              and all(k in sk.contrast_bg for k in ("card", "h", "v"))
              for sk in m.skins.REGISTRY.values()))
    check("皮肤:九款绘制侧色字段均 QColor 实例(rgba 字面不进 painter)",
          all(isinstance(getattr(sk, a), QColor)
              for sk in m.skins.REGISTRY.values()
              for a in ("spark_line", "spark_dot", "pulse_idle",
                        "pulse_active", "pulse_core", "ring_base")))
    check("皮肤:玻璃 mono 字族==mk_mono 缺省(字族零漂)",
          m.skins.REGISTRY["glass"].font_mono_families == ("Cascadia Code",
                                                           "Consolas"))
    # ③ wiring:_skin() 分派与 _skin_qss() 形态分派(glass 基线已钉)
    win._build_card()
    check("皮肤:win._skin()==glass 项且卡形态 _skin_qss()==m.QSS",
          win._skin() is m.skins.REGISTRY["glass"]
          and win.skin_id == "glass" and win._skin_qss() == m.QSS)
    win._build_bar()
    check("皮肤:条形态 _skin_qss()==m.QSS_BAR(形态分派不串档)",
          win._skin_qss() == m.QSS_BAR)
    win._build_card()
    # ④【T2 期安全】registry 仅 glass 时,任何合法白名单 id 均 not KeyError:
    #   _skin() get-or-fallback 回退 glass 注册表项(T1 白名单先行落地/
    #   手改文件指向未实现 id 的真实时序,spec 风险表双保险之一)
    _saved_registry = m.skins.REGISTRY
    m.skins.REGISTRY = {"glass": _saved_registry["glass"]}
    try:
        _fb_ok = True
        for _sid in m.skins.SKIN_IDS:
            win.skin_id = _sid
            if win._skin() is not m.skins.REGISTRY["glass"]:
                _fb_ok = False
            if win._skin_qss() != m.QSS:   # 回退玻璃渲染:qss 同 glass 逐位
                _fb_ok = False
        check("皮肤:T2 期安全 registry 仅 glass → 合法 id 不 KeyError 回退玻璃",
              _fb_ok)
    finally:
        m.skins.REGISTRY = _saved_registry
        win.skin_id = "glass"
    # deco 定稿守卫(T4 已逐款实现,原『T2 骨架恒 None』过渡断言随落地退役):
    # glass 无装饰(None=paintEvent 既有玻璃路径逐位不动),八款 deco 可调用;
    # registry 缺项回退玻璃的性质不因此破 —— paintEvent 按 deco 分派,None
    # 即玻璃兜底(上方 ④ 回退断言已覆盖该路径)
    check("皮肤:T4 定稿:glass deco=None(玻璃路径逐位),八款 deco 可调用",
          m.skins.REGISTRY["glass"].deco is None
          and all(callable(m.skins.REGISTRY[_s].deco)
                  for _s in m.skins.SKIN_IDS if _s != "glass"))

    # ==== v0.9 T3 皮肤切换机制:_apply_skin 即时重建/子菜单/持久化守卫/丢键修复 ====
    # 满载快照(注入先例见前 :79-87)+ 套餐/倒计时注入:九皮肤×三形态全走
    # 产品切换路径 _apply_skin(persist=False),与右键菜单同一条代码路径
    win.snap = m.Snapshot(
        state="idle", model="GLM-5.3", title="t", tps_exact=137.9,
        global_tps=42.0,
        today_tokens=1_200_000_000, today_cost_cny=2489.0,
        today_by_source=[("ZCode", 1_200_000_000)], plan_remaining_pct=87.0,
        burn_tokens_per_hour=31_300_000, burn_avg_tokens_per_hour=129_700_000,
        session_in=448_000_000, session_out=624_000,
        recent_speeds=[20, 35, 28, 44, 30, 52, 38],
        speed_by_model=[("bigmodel-api", "GLM-5.3", 56.8, 0)])
    win._plan_pct = 87.0
    win._plan_left_tok = 2_100_000_000.0
    win._plan_next_reset = time.time() * 1000 + 90 * 60_000
    win.bar_segments = {"h": list(m.BAR_SEGMENTS_H), "v": list(m.BAR_SEGMENTS_V)}

    # ① 9×3 切换循环(循环本身无异常即断言的一半):逐皮肤逐形态 ——
    #    卡=防钳宽纪律(313x341);条=尺寸机制对一切皮肤同上限(竖定宽
    #    BAR_V_W/横高≤34);F4 属性矩阵非 None 集合逐皮肤不变(置 None
    #    纪律在任意皮肤下都不漏,v0.4.0 悬空引用教训的皮肤版兜底)
    F4_KEYS = ("dot", "tps_lbl", "tps_unit_lbl", "spark", "today_lbl",
               "today_cost_lbl", "today_src_lbl", "state_lbl", "model_name_lbl",
               "model_rows", "plan_section", "plan_ring", "plan_cap_lbl",
               "plan_tok_lbl", "plan_sub_lbl", "plan_left_k", "plan_left_v",
               "plan_lbl", "in_out_lbl", "rate_lbl", "timing_lbl", "burn_lbl",
               "avg_burn_lbl", "avg_lbl", "in_lbl", "out_lbl", "title_lbl",
               "est_lbl", "elapsed_lbl", "cache_lbl", "ttft_lbl", "dur_lbl",
               "plan_cd_lbl", "vbar_in_cap", "vbar_burn_cap", "sep_plan",
               "sep_burn", "sep_today", "vsep_plan", "vsep_burn",
               "vsep_today", "vsep_in")

    def _f4_nonnone():
        return frozenset(k for k in F4_KEYS if getattr(win, k, None) is not None)

    _f4_glass = {}
    for _sid in m.skins.SKIN_IDS:
        for _form, _dock in (("card", None), ("h", "top"), ("v", "left")):
            win.dock = _dock
            win._apply_skin(_sid, persist=False)   # 切换保持当前形态(dock 不变)
            if _form == "card":
                check(f"皮肤循环 {_sid} 卡:_bar_form 归位+防钳宽(313x341)",
                      win._bar_form is None
                      and win.width() == m.MeterWindow.CARD_W
                      and win.height() == m.MeterWindow.CARD_H,
                      f"{win.width()}x{win.height()}")
            else:
                _vert = _form == "v"
                _w, _h = win._bar_size(_vert)
                check(f"皮肤循环 {_sid} {'竖' if _vert else '横'}条:"
                      + ("宽==BAR_V_W(100 定宽不随皮肤漂)" if _vert
                         else "高<=34(对一切皮肤同上限)"),
                      win._bar_form == _form
                      and ((_w == m.MeterWindow.BAR_V_W) if _vert else (_h <= 34)),
                      f"{_w}x{_h}")
            _cur = _f4_nonnone()
            if _sid == "glass":
                _f4_glass[_form] = _cur
            else:
                check(f"皮肤循环 {_sid} {_form}:F4 非 None 集合与玻璃一致",
                      _cur == _f4_glass[_form], str(_cur ^ _f4_glass[_form]))
    check("皮肤循环:9×3 全矩阵经 _apply_skin 重建(27 次,三形态基准齐全)",
          len(_f4_glass) == 3 and all(_f4_glass.values()))

    # 段开关在新皮肤下仍工作:经「显示内容」菜单动作真实触发(非玻璃皮肤
    # 活动时关 plan 段 → toggle 的重征路径走 _skin_qss,件未建(None))
    win.dock = "top"
    win._apply_skin("swiss", persist=False)
    _seg_host = QMenu()
    win._add_segment_menu(_seg_host)
    _seg_menu = next(a for a in _seg_host.actions()
                     if a.text() == "显示内容").menu()
    _plan_act = next(a for a in _seg_menu.actions()
                     if a.text() == m.BAR_SEGMENT_LABELS_H["plan"])
    # Qt6 检查型 QAction.trigger() 先翻转勾态再发 triggered(新勾态):
    # 当前勾着(True)→ trigger() 发 triggered(False) → toggle("plan", False)
    # → 摘段重建+落盘(ZM_NO_STATE 守卫静默跳过)。实测先 setChecked(False)
    # 再 trigger() 会被翻回 True(等于没关),探针复盘结论
    _plan_act.setChecked(True)
    _plan_act.trigger()
    check("新皮肤下段开关:关 plan → plan_ring/sep_plan 未建,速度/今日仍显",
          win.plan_ring is None and win.sep_plan is None
          and win.tps_lbl is not None and win.today_lbl is not None,
          f"ring={win.plan_ring} segs={win.bar_segments.get('h')}")
    win.bar_segments = {"h": list(m.BAR_SEGMENTS_H), "v": list(m.BAR_SEGMENTS_V)}
    win._apply_skin("swiss", persist=False)          # 复位全段重建(仍 swiss)

    # ② 皮肤子菜单:九项 checkable 单选,勾态唯一且==当前皮肤;triggered →
    #    _apply_skin(persist 默认 True,ZM_NO_STATE 守卫下落盘被静默跳过)
    _skin_host = QMenu()
    win._add_skin_menu(_skin_host)
    _skin_menu = next(a for a in _skin_host.actions()
                      if a.text() == "皮肤").menu()
    _acts = _skin_menu.actions()
    _checked = [a for a in _acts if a.isChecked()]
    check("皮肤子菜单:九项且勾态唯一(当前=swiss)",
          len(_acts) == 9 and len(_checked) == 1
          and _checked[0].text() == m.skins.REGISTRY["swiss"].menu_label,
          f"{len(_acts)}项 勾={[a.text() for a in _checked]}")
    _crt_act = next(a for a in _acts
                    if a.text() == m.skins.REGISTRY["crt"].menu_label)
    _crt_act.setChecked(True)
    _crt_act.trigger()                               # → _apply_skin("crt")
    check("皮肤子菜单 triggered:即时切到 crt 且注册表分派跟随(形态保持横条)",
          win.skin_id == "crt" and win._skin() is m.skins.REGISTRY["crt"]
          and win._bar_form == "h")
    win._apply_skin("glass", persist=False)
    # 此处 dock 仍 "top"(条形态):玻璃复位后应得 qss_bar(形态分派正确性)
    check("皮肤复位:回 glass 且条形态 qss_bar 与玻璃逐位(QActionGroup 可逆入口)",
          win.skin_id == "glass" and win._skin_qss() == m.QSS_BAR)

    # ③ ZM_NO_STATE=1(本文件头注入)下 _save_config_skin 静默跳过:守卫由
    #    save_config 默认路径内建,即便待写值是非 glass 也不得碰真实配置
    _cfg_file = Path(de.CONFIG_PATH)
    _before = _cfg_file.read_bytes() if _cfg_file.exists() else None
    win.skin_id = "vaporwave"
    win._save_config_skin()
    win._save_config_segments()
    _after = _cfg_file.read_bytes() if _cfg_file.exists() else None
    check("ZM_NO_STATE 下 _save_config_skin 静默跳过(真实配置零改写)",
          _before == _after, f"beforeNone={_before is None} afterNone={_after is None}")
    win.skin_id = "glass"

    # ⑥ _apply_config 透传(设置保存丢键修复):monkeypatch m.save_config
    #    捕参 —— 四键输入也恒带 bar_segments(等于内存态);非 glass 时 skin
    #    随行;不受设置窗四键输入影响(glass 不添 skin 键,可选键纪律)
    _captured = []
    _orig_save = m.save_config
    m.save_config = lambda c, path=None: (
        _captured.append(dict(c)), _orig_save(c, path))[1]
    try:
        win._apply_config({"quota_api_key": "", "daily_budget_cny": None,
                           "alert_pct": [20.0, 10.0]})   # 设置窗 _parse_input 四键形状
        check("_apply_config 透传:四键输入恒带 bar_segments(glass 不添 skin 键)",
              bool(_captured)
              and _captured[-1].get("bar_segments") == win.bar_segments
              and "skin" not in _captured[-1],
              str(sorted(_captured[-1])) if _captured else "no-call")
        win._apply_skin("crt", persist=False)
        win._apply_config({"quota_api_key": "", "daily_budget_cny": 5.0,
                           "alert_pct": [30.0, 15.0]})
        check("_apply_config 透传:非 glass 时 skin 随行落盘保活(丢键修复)",
              _captured[-1].get("skin") == "crt"
              and _captured[-1].get("bar_segments") == win.bar_segments,
              str(sorted(_captured[-1])))
    finally:
        m.save_config = _orig_save
    win.dock = None
    win._apply_skin("glass", persist=False)   # 离场复位:玻璃+卡形态(防钳宽路径)
    check("皮肤复位:回 glass 且卡形态 qss 与玻璃逐位(可逆入口)",
          win.skin_id == "glass" and win._bar_form is None
          and win._skin_qss() == m.QSS)

    # ==== v0.9 T3-④(验收 ticket 补齐):报纸双空格档(三处定稿微调之一) ====
    # HTML :484 &nbsp;&nbsp; —— 横条今日段 量↔金额 分隔符经 today_cost_sep
    # 落地,只动静态分隔字符、不碰数值格式化(口径红线);玻璃恒单空格由
    # 上方 T2 组与既有 today 文案断言(:257『今 1.2M ≈¥12』)钉死
    win.dock = "top"
    win._apply_skin("newspaper", persist=False)
    check("报纸双空格档:today_cost_sep='  ' 且横条今日段量价间双空格",
          m.skins.REGISTRY["newspaper"].today_cost_sep == "  "
          and win.today_lbl.text() ==
          f"今 {m.fmt_k(win.snap.today_tokens)}  "
          f"{'≈' if win.snap.today_cost_partial else ''}"
          f"¥{win.snap.today_cost_cny:.0f}",
          repr(win.today_lbl.text()))
    win._apply_skin("glass", persist=False)
    win.dock = None

    # ==== v0.9 T5 浅色可读性:WCAG 对比度机器闸(contrast_bg 口径钉死) ====
    # spec 步骤⑦⑧(D 修订口径),闸的三道钉死:
    # - contrast_bg=该形态 deco 实际铺底中【承载字段文本的面】的底色,由
    #   skins.py 逐款如实申报(蒸汽波=统计暗格与落日亮带最坏合成 #51213d/
    #   #2c1038、工业=readout 暗屏 #101614、液态玻璃=白.14 药丸合成底;
    #   blob/光晕/落日亮带等无字装饰带不得充当 —— 『挑暗底自证』被申报
    #   口径封死);
    # - 半透明档(rgba 字面)按 α 与申报底逐通道合成后再比(WCAG 以实际
    #   渲染色为准,QSS 不透明合成即所见);
    # - 分档按角色语义(spec『字段文本(k/v/段文字)≥4.5:1,主数字/帽标等
    #   大字≥3:1』)映射到 palette 键:
    #   · 字段文本档(数据值与段文字,10-14px)= dim/soft/half/vstrong ≥4.5;
    #   · 大字档(主数字 accent / 今日 hero fg)≥3.0;
    #   · faint 微标签档(卡 k 标与竖条帽标同 role)≥3.0 —— 4.5 对该档在
    #     任何底色上都不可满足:玻璃 faint #68696c 亮度 0.141,纯黑底上也
    #     只有 3.83:1,而玻璃 QSS 已由本文件 T2 组逐位断言冻结(改色即
    #     违约),故按 spec 帽标同档钉 3:1,数值上即『换肤不劣于缺省玻璃
    #     (3.02)』的下限;
    #   · warn 不进闸:唯一消费者 plan_lbl 的文本色恒被 tier 内联覆盖
    #     (本文件 tier 注入断言 styleSheet()=='color: #6ee7a8;' 钉死)或
    #     空文本,QSS 类色不落屏;
    # - tier 三档色(#6ee7a8/#ffd166/#ff6b6b)不进闸:nonGoal 钉死全局色
    #   不随皮肤换色,琥珀在奶白底偏弱已记 README 人工目视清单;
    # - 九皮肤×三形态全测,逐皮肤取三形态最低值达标(spec『三形态各测取
    #   最低者达标』);玻璃同闸通过(最低档 faint 3.02)。
    def _t5_parse(col):
        """'#rrggbb' → (r,g,b,255);'rgba(r,g,b,a)' → 四元组(仅 palette
        半透明档与 sep 用,palette hex 键与 contrast_bg 申报底均为实色)。"""
        c = col.strip()
        if c.startswith("rgba"):
            parts = c[c.index("(") + 1:c.rindex(")")].split(",")
            return tuple(int(p) for p in parts)
        c = c.lstrip("#")
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255)

    def _t5_lin(v8):
        v = v8 / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    def _t5_lum(rgb):
        return (0.2126 * _t5_lin(rgb[0]) + 0.7152 * _t5_lin(rgb[1])
                + 0.0722 * _t5_lin(rgb[2]))

    def _t5_ratio(a, b):
        la, lb = _t5_lum(a), _t5_lum(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    def _t5_worst(sk, roles):
        """roles 各键在 contrast_bg 三形态申报底上的最低对比度(WCAG 2.x
        相对亮度公式;返回 (ratio, role, form) 供断言详情定位)。"""
        worst = (99.0, "", "")
        for form in ("card", "h", "v"):
            bg = _t5_parse(sk.contrast_bg[form])[:3]
            for role in roles:
                fg = _t5_parse(sk.palette[role])
                a = fg[3] / 255.0
                comp = tuple(round(a * fg[i] + (1 - a) * bg[i])
                             for i in range(3))       # rgba 档实色合成
                r = _t5_ratio(comp, bg)
                if r < worst[0]:
                    worst = (r, role, form)
        return worst

    _T5_FIELD = ("dim", "soft", "half", "vstrong")   # 字段文本 ≥4.5
    _T5_BIG = ("fg", "accent")                        # 主数字/hero 大字 ≥3.0
    _T5_MICRO = ("faint",)                            # 微标签/帽标档 ≥3.0
    for _sid in m.skins.SKIN_IDS:
        _sk = m.skins.REGISTRY[_sid]
        _wf, _rf, _ff = _t5_worst(_sk, _T5_FIELD)
        _wb, _rb, _fb = _t5_worst(_sk, _T5_BIG)
        _wm, _rm, _fm = _t5_worst(_sk, _T5_MICRO)
        check(f"皮肤对比度 {_sid}:字段文本档>=4.5(三形态最低)",
              _wf >= 4.5, f"min={_wf:.2f}({_rf}/{_ff})")
        check(f"皮肤对比度 {_sid}:大字档>=3.0",
              _wb >= 3.0, f"min={_wb:.2f}({_rb}/{_fb})")
        check(f"皮肤对比度 {_sid}:微标签档>=3.0(玻璃底线)",
              _wm >= 3.0, f"min={_wm:.2f}({_rm}/{_fm})")

    win.close()
    if FAILED:
        print(f"\nFAILED: {FAILED}")
        return 1
    print("\nLAYOUT STRESS TESTS ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
