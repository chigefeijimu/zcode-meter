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
    check("横条高度=一行文字级(<=30 逻辑px)", hs[0][1] <= 30, str(hs[0]))
    check("竖条宽度<=90 逻辑px(tok/s大字撑宽)", vs[0][0] <= 90, str(vs[0]))

    # 动态 refit:卡片模式应 no-op 不崩
    win._refit_dock()
    check("卡片模式 refit no-op", win.dock is None)

    # ---- v0.5.0:贴边条预算段(plan/burn)结构与显隐回归 ----
    # 结构:横条 plan+burn 都创建(带 _budget_sep);竖条只建紧凑 plan,
    # burn 显式置 None(置 None 纪律防另一形态循环摸已销毁 QLabel —— 文件头
    # 记载的 v0.4.0 同类 bug);_bar_form 与实际标签集合一一对应
    win._build_bar()
    check("横条构建:plan/burn 均非 None",
          win.plan_lbl is not None and win.burn_lbl is not None)
    check("横条构建:_bar_form='h'", win._bar_form == "h")
    win._build_bar(vertical=True)
    check("竖条构建:plan/plan_sub/burn 均 None 安全且存在",
          win.plan_lbl is not None and win.plan_sub_lbl is not None
          and win.burn_lbl is not None)
    check("竖条构建:_bar_form='v'", win._bar_form == "v")

    # 注入套餐剩余/燃速后再跑双形态循环:显隐切换直接影响 _bar_size(隐藏
    # 控件须被跳过),虚胖会让高度/宽度断言与尺寸稳定断言当场翻车
    win._plan_pct = 42.0
    win.snap.burn_tokens_per_hour = 12345.0
    sizes2 = []
    for i in range(4):
        win._build_bar(); win._apply_snapshot(win.snap); win._refit_dock()
        sizes2.append(("h", win._bar_size(False)))
        check(f"预算段循环{i} 横条:plan/burn 可见且文案正确",
              win.plan_lbl.isVisible() and win.burn_lbl.isVisible()
              and win.plan_lbl.text() == "剩 42%"
              and win.burn_lbl.text() == "燃速 12.3K/h"
              and win._budget_sep.isVisible())
        win._build_bar(vertical=True); win._apply_snapshot(win.snap)
        sizes2.append(("v", win._bar_size(True)))
        check(f"预算段循环{i} 竖条:紧凑 plan 可见",
              win.plan_lbl.isVisible() and win.plan_lbl.text() == "42%")
        win._unset_dock()
    hs2 = [s for k, s in sizes2 if k == "h"]
    vs2 = [s for k, s in sizes2 if k == "v"]
    check("预算段横条尺寸稳定", all(x == hs2[0] for x in hs2), str(hs2))
    check("预算段竖条尺寸稳定", all(x == vs2[0] for x in vs2), str(vs2))
    check("预算段横条高度仍<=30", hs2[0][1] <= 30, str(hs2[0]))
    # v0.7.x:注入重置倒计时后,条形态 plan 文案应含倒计时段(横条单行/竖条两行)
    import time as _t
    win._plan_next_reset = _t.time() * 1000 + 90 * 60_000     # 90 分钟后
    win._build_bar(); win._apply_snapshot(win.snap)
    ht = win.plan_lbl.text()
    check("横条倒计时入条", ht.startswith("剩 42%") and "后重置" in ht, ht)
    check("横条仍单行(高度<=30)", "\n" not in ht)
    win._build_bar(vertical=True); win._apply_snapshot(win.snap)
    vt = win.plan_lbl.text()
    check("竖条倒计时入条(plan_sub 独立行)",
          vt.startswith("42%") and win.plan_sub_lbl is not None
          and win.plan_sub_lbl.text() != "" and len(win.plan_sub_lbl.text()) <= 7,
          f"{vt!r}/{win.plan_sub_lbl.text() if win.plan_sub_lbl else None!r}")
    win._plan_next_reset = None
    check("预算段竖条宽度仍<=90", vs2[0][0] <= 90, str(vs2[0]))
    # 数据缺席:整段隐藏(含分隔线),不残留空占位
    win._plan_pct = None
    win.snap.burn_tokens_per_hour = 0.0
    win._build_bar(); win._apply_snapshot(win.snap)
    check("数据缺席:plan/burn/分隔线整段隐藏",
          not win.plan_lbl.isVisible() and not win.burn_lbl.isVisible()
          and not win._budget_sep.isVisible())

    # ---- v0.7 T-A:卡片信息层级重排(today/plan 两级 hero + timing 合并单格)----
    # 结构:卡片建 timing/today_src/plan_sub 三新 label 且不再建 ttft/dur;
    # 两种条形态三者都不建(置 None 纪律:漏一处即形态循环摸已销毁 QLabel)
    win._build_card(); win.dock = None
    win._apply_snapshot(win.snap)
    check("卡片构建:timing/today_src/plan_sub 非 None 且 ttft/dur 为 None",
          win.timing_lbl is not None and win.today_src_lbl is not None
          and win.plan_sub_lbl is not None
          and win.ttft_lbl is None and win.dur_lbl is None)
    win._build_bar()
    check("横条构建:timing/today_src 为 None(plan_sub 横条不建)",
          win.timing_lbl is None and win.today_src_lbl is None)
    win._build_bar(vertical=True)
    check("竖条构建:timing/today_src 为 None(plan_sub 竖条在用)",
          win.timing_lbl is None and win.today_src_lbl is None)

    # D3 钉死表:卡片 timing 合并单格逐串断言(分段占位,整串结构恒保留)
    win.snap.last_ttft, win.snap.last_duration = 0.8, 12.4
    win._build_card(); win.dock = None
    win._apply_snapshot(win.snap)
    check("卡片 timing:双值", win.timing_lbl.text() == "⏱ 首字 0.8s · 总 12.4s",
          win.timing_lbl.text())
    win.snap.last_ttft = None
    win._apply_snapshot(win.snap)
    check("卡片 timing:缺 ttft", win.timing_lbl.text() == "⏱ 首字 -- · 总 12.4s",
          win.timing_lbl.text())
    win.snap.last_ttft, win.snap.last_duration = 0.8, None
    win._apply_snapshot(win.snap)
    check("卡片 timing:缺 dur", win.timing_lbl.text() == "⏱ 首字 0.8s · 总 --",
          win.timing_lbl.text())
    win.snap.last_ttft = None
    win._apply_snapshot(win.snap)
    check("卡片 timing:双 None", win.timing_lbl.text() == "⏱ 首字 -- · 总 --",
          win.timing_lbl.text())

    # D3 钉死表:plan 两级 hero/sub 逐串断言(180s 龄→3分钟前,90min→1h 30m)
    win._plan_pct = 42.0
    win._plan_fetched_at = time.time() - 180
    win._plan_next_reset = time.time() * 1000 + 90 * 60_000
    win._apply_snapshot(win.snap)
    check("卡片 plan hero", win.plan_lbl.text() == "剩 42%",
          win.plan_lbl.text())
    check("卡片 plan sub:age+cd 双在场",
          win.plan_sub_lbl.text() == "· 3分钟前 · 1h 30m 后重置",
          win.plan_sub_lbl.text())
    win._plan_next_reset = None
    win._apply_snapshot(win.snap)
    check("卡片 plan sub:仅 age", win.plan_sub_lbl.text() == "· 3分钟前",
          win.plan_sub_lbl.text())
    win._plan_next_reset = time.time() * 1000 + 90 * 60_000
    win._plan_fetched_at = None
    win._apply_snapshot(win.snap)
    check("卡片 plan sub:仅 cd", win.plan_sub_lbl.text() == "· 1h 30m 后重置",
          win.plan_sub_lbl.text())
    win._plan_pct = None
    win._apply_snapshot(win.snap)
    check("卡片 plan:pct=None → hero/sub 双空",
          win.plan_lbl.text() == "" and win.plan_sub_lbl.text() == "",
          f"{win.plan_lbl.text()!r}/{win.plan_sub_lbl.text()!r}")

    # 卡片 today hero/多源副文本:『今日』前缀 + cost=0 省金额段 + partial ≈
    # 口径 + >1 源才显示(口径红线,搬动中弄丢即新一轮口径 bug)
    win.snap.today_tokens = 1_234_567
    win.snap.today_cost_cny = 12.34
    win.snap.today_cost_partial = True
    win.snap.today_by_source = [("ZCode", 1_234_567), ("Claude", 456_789)]
    win._apply_snapshot(win.snap)
    check("卡片 today hero:partial ≈ + 金额段",
          win.today_lbl.text() == "今日 1.2M · ≈¥12.34", win.today_lbl.text())
    check("卡片 today 多源副文本",
          # 456_789 经 fmt_k 的 :.1f 缩写为 456.8K(join 文案不变的红线)
          win.today_src_lbl.text() == "ZCode 1.2M · Claude 456.8K",
          win.today_src_lbl.text())
    win.snap.today_cost_cny = None
    win.snap.today_by_source = [("ZCode", 1_234_567)]
    win._apply_snapshot(win.snap)
    check("卡片 today:cost=0 省金额段 + ≤1 源置空",
          win.today_lbl.text() == "今日 1.2M" and win.today_src_lbl.text() == "",
          f"{win.today_lbl.text()!r}/{win.today_src_lbl.text()!r}")

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

    win.close()
    if FAILED:
        print(f"\nFAILED: {FAILED}")
        return 1
    print("\nLAYOUT STRESS TESTS ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
