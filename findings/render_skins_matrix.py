#!/usr/bin/env python3
"""T4 渲染矩阵工具:九皮肤 × 三形态逐一渲染出图,供与 design/skins-8x3.html
(工作区 zm_skins_full.png 为其渲染参照物,zm_fix_chalk/liquid/news.png 对应
三处定稿微调)人工比对。

方法红线(三道闸先例 = findings/measure_card_baseline.py):
1. 原生平台 —— offscreen 下 adjustSize 塌成 200x22 废值(实测证伪过),
   QT_QPA_PLATFORM 显式清除;
2. 注入 → processEvents()(布局激活)→ 再取几何/grab;
3. 不合规打 INVALID 并以退出码 1 中止:grab 空图/尺寸脱常量(卡 313x341、
   竖条宽 BAR_V_W、横条高≤34)/画面近似纯色(deco 没跑)/非 glass 皮肤与
   glass 顶缘采样同色(皮肤没生效)。

驱动原语钉死为 T2 期原语(spec A 修订:不用 T3 的 _apply_skin,保本工具
与 T3 并行可验收;T3 落地后仍按此驱动,产品路径由 stress 断言覆盖):
  win.skin_id = sid
  → 卡 win._build_card() / 条 win._build_bar(vertical=…)
  → win.setStyleSheet(win._skin_qss())
  → win._apply_snapshot(win.snap)

截图红线(CHANGELOG v0.8.0 竖条对版实测):禁 GDI 手工字节通道重排;
win.grab() 直出 QPixmap→QImage→save;像素采样只做『同色/异色』比较与
数色,与通道序无关。
只读渲染,不改任何源码;产出 findings/skins_matrix.png(9×3)与
findings/skins_wallpaper_check.png(瑞士/报纸×3 形态 × 深/浅双色壁纸)。
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("ZM_NO_STATE", "1")
os.environ.pop("QT_QPA_PLATFORM", None)   # 红线 1:禁 offscreen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import zcode_meter.app as m  # noqa: E402
from zcode_meter.data_engine import SKIN_IDS  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent
MATRIX_PATH = OUT_DIR / "skins_matrix.png"
WALLPAPER_PATH = OUT_DIR / "skins_wallpaper_check.png"

FORM_NAMES = ("card", "h-bar", "v-bar")
WALLPAPER_SKINS = ("swiss", "newspaper")   # 浅色两款(spec 步骤 10 人工项)


def full_snapshot() -> "m.Snapshot":
    """满载注入(test_stress :70-77 / measure_card_baseline 同款口径):
    套餐段 87%(OK 档)+ 均燃 + 入出 + 12 点速度曲线 + 双源今日 + 2 行模型
    —— deco 的局部底(工业 readout/铭牌、蒸汽波暗格、液态药丸)锚定这些
    label 的几何,必须满载才摆得出对版位置。"""
    s = m.Snapshot()
    s.state = "generating"
    s.model = "GLM-5.3"
    s.title = "t"
    s.global_tps = 24.3
    s.gen_elapsed = 12.0
    s.tps_avg = 56.8
    s.today_tokens = 1_200_000_000
    s.today_cost_cny = 2489.0
    s.today_by_source = [("ZCode", 1_200_000_000), ("Claude", 456_789)]
    s.last_ttft, s.last_duration = 0.8, 12.4
    s.session_in, s.session_cache, s.session_out = 900_000, 880_000, 624_000
    s.cache_rate = 95.01
    s.recent_speeds = [20, 35, 28, 44, 30, 52, 38, 41, 33, 47, 36, 43]
    s.burn_tokens_per_hour = 28_500_000
    s.burn_avg_tokens_per_hour = 129_700_000
    s.burn_instant_per_hour = 31_300_000
    s.speed_by_model = [("bigmodel-api", "GLM-5.3", 56.8, 0),
                        ("bigmodel-api", "GLM-5.3-Flash", 44.0, 2000)]
    return s


def settle(app, win, w: int, h: int):
    """红线 2:布局激活 + 事件循环排水,再定尺寸(对齐 _unset_dock 的
    『先 activate 再 setGeometry 防最小宽钳制』纪律)。"""
    win.layout().activate()
    app.processEvents()
    win.setGeometry(40, 40, w, h)
    app.processEvents()
    win.repaint()


def render_form(app, win, sid: str, form: str) -> QImage:
    """单一皮肤单一形态渲染 → QImage。驱动原语即 T2 期原语(见文件头)。"""
    win.skin_id = sid
    win.dock = None                      # 工具自管几何,不走 _refit_dock 分支
    if form == "card":
        win._build_card()
        win.setStyleSheet(win._skin_qss())
        win._apply_snapshot(win.snap)
        settle(app, win, m.MeterWindow.CARD_W, m.MeterWindow.CARD_H)
    else:
        vertical = form == "v-bar"
        win._build_bar(vertical=vertical)
        win.setStyleSheet(win._skin_qss())
        win._apply_snapshot(win.snap)
        w, h = win._bar_size(vertical)
        settle(app, win, w, h)
    return win.grab().toImage()          # 红线:QImage 直出,零通道重排


# ---- 健全性闸(INVALID 判据) ----

def color_count(img: QImage, step: int = 7) -> int:
    """降采样数色:纯色/黑屏(deco 或 grab 管线失效)的图片色数个位数;
    正常皮肤画面几十~几百种。与通道序无关。"""
    seen = set()
    for y in range(0, img.height(), step):
        for x in range(0, img.width(), step):
            seen.add(img.pixel(x, y))
            if len(seen) > 60:
                return len(seen)
    return len(seen)


def top_pixel(img: QImage) -> int:
    """顶缘中点采样:圆角只影响近角区域,顶边中点对任意 radius 都在画面
    内,且必落在 deco 铺底上(九款底色互异,该点即皮肤生效指纹)。"""
    return img.pixel(img.width() // 2, 3)


def draw_label(p: QPainter, x: int, y: int, text: str, px: int, color):
    f = p.font()
    f.setPixelSize(px)
    p.setFont(f)
    p.setPen(QPen(color))
    p.drawText(x, y + px, text)


def compose_matrix(cells: dict) -> QImage:
    """cells[(sid, form)] = QImage → 9×3 矩阵(行=皮肤,列=卡/横条/竖条)。"""
    pad, gap, label_h = 16, 14, 24
    col_w = [max(cells[(s, f)].width() for s in SKIN_IDS) + pad * 2
             for f in FORM_NAMES]
    row_h = [max(cells[(s, f)].height() for f in FORM_NAMES) + label_h + gap
             for s in SKIN_IDS]
    W = pad * 2 + sum(col_w) + gap * (len(FORM_NAMES) - 1)
    H = pad * 2 + 30 + sum(row_h)
    img = QImage(W, H, QImage.Format.Format_RGB32)
    img.fill(QColor("#101116"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    f = p.font()
    f.setPixelSize(16)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QPen(QColor("#e8eaf0")))
    p.drawText(pad, 20, "zcode-meter skins matrix · 9 skins x 3 forms "
                        "(card / h-bar / v-bar)  2026-09-28")
    y = pad + 30
    for r, sid in enumerate(SKIN_IDS):
        draw_label(p, pad, y, f"{sid} · "
                   f"{m.skins.REGISTRY[sid].menu_label}", 12, QColor("#8b8f9c"))
        y += label_h
        x = pad
        for c, form in enumerate(FORM_NAMES):
            cell = cells[(sid, form)]
            p.drawImage(x + (col_w[c] - cell.width()) // 2, y, cell)
            x += col_w[c] + gap
        y += row_h[r]
    p.end()
    return img


def wallpaper_layer(w: int, h: int, dark: bool) -> QImage:
    """合成壁纸底:深=#07080a 系(design stage.dark + 暗色形状),浅=米灰系
    (design stage.cream 线性渐变)。窗口不透明,壁纸只影响边缘观感 ——
    合成图供目视边缘/圆角融合(spec 步骤 10 人工项)。"""
    img = QImage(w, h, QImage.Format.Format_RGB32)
    p = QPainter(img)
    if dark:
        img.fill(QColor("#07080a"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#16181f"))
        p.drawRect(int(w * 0.55), int(h * 0.6), int(w * 0.4), int(h * 0.3))
        p.setBrush(QColor("#101319"))
        p.drawRect(int(w * 0.05), int(h * 0.1), int(w * 0.3), int(h * 0.45))
    else:
        grad = QLinearGradient(0, 0, w, h)
        grad.setColorAt(0.0, QColor("#ded8ca"))
        grad.setColorAt(1.0, QColor("#c9c2b2"))
        p.fillRect(0, 0, w, h, grad)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#bdb6a6"))
        p.drawRect(int(w * 0.6), int(h * 0.55), int(w * 0.35), int(h * 0.35))
    p.end()
    return img


def compose_wallpaper(cells: dict) -> QImage:
    """瑞士/报纸(浅色两款)×3 形态,叠加深/浅双色壁纸 → 单张对比图。
    直接复用主循环的 grab(窗口不透明,壁纸不进 grab,合成即真实观感)。"""
    pad, gap, label_h = 16, 18, 22
    forms = FORM_NAMES
    col_w = [max(cells[(s, f)].width() for s in WALLPAPER_SKINS) + pad * 2
             for f in forms]
    content_h = max(cells[(s, f)].height() for s in WALLPAPER_SKINS
                    for f in forms)
    band_h = label_h + content_h * 2 + gap * 3
    W = pad * 2 + sum(col_w) + gap * (len(forms) - 1)
    H = (28 + band_h) * 2
    img = QImage(W, H, QImage.Format.Format_RGB32)
    p = QPainter(img)
    p.drawImage(0, 0, wallpaper_layer(W, H // 2, dark=True))
    p.drawImage(0, H // 2, wallpaper_layer(W, H - H // 2, dark=False))
    f = p.font()
    f.setPixelSize(15)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QPen(QColor("#ffffff")))
    p.drawText(pad, 20, "light skins x 3 forms on dark (top) / light "
                        "(bottom) wallpaper")
    for band, (name, fg) in enumerate((("dark", "#e8eaf0"),
                                       ("light", "#3a3529"))):
        y0 = band * (H // 2)
        draw_label(p, pad, y0 + 28, f"{name} wallpaper", 12, QColor(fg))
        x = pad
        for c, form in enumerate(forms):
            y = y0 + 28 + label_h
            for sid in WALLPAPER_SKINS:
                cell = cells[(sid, form)]
                p.drawImage(x + (col_w[c] - cell.width()) // 2, y, cell)
                draw_label(p, x, y + cell.height() + 2, sid, 10, QColor(fg))
                y += cell.height() + gap
            x += col_w[c] + gap
    p.end()
    return img


def main() -> int:
    app = QApplication(sys.argv)
    win = m.MeterWindow()
    # 三闸先例:停引擎/队列/UI 轮询/呼吸定时器 —— 渲染只由本脚本注入驱动,
    # 防真实快照竞态(measure_card_baseline 486px 假峰教训)
    win.eng.stop()
    win.q.queue.clear()
    win._poll_timer.stop()
    win._breath_timer.stop()
    win.snap = full_snapshot()
    # 段全开钉死:ZM_NO_STATE 只拦写不拦读,用户真实 zm_config.json 的
    # bar_segments(可能关了段)会改变条形态渲染 —— 矩阵必须满段对版
    win.bar_segments = {"h": list(m.BAR_SEGMENTS_H),
                        "v": list(m.BAR_SEGMENTS_V)}
    win._plan_pct = 87.0
    win._plan_left_tok = 2_100_000_000.0
    win._plan_fetched_at = time.time() - 180
    win._plan_next_reset = time.time() * 1000 + 90 * 60_000
    print(f"DPI scale = {win.devicePixelRatioF()}")

    failures = []
    cells = {}
    glass_top = None
    for sid in SKIN_IDS:
        for form in FORM_NAMES:
            img = render_form(app, win, sid, form)
            cells[(sid, form)] = img
            tag = f"{sid}/{form}"
            if img.isNull() or img.width() < 40 or img.height() < 16:
                failures.append(f"{tag}: grab 空图 {img.width()}x{img.height()}")
                continue
            # 尺寸红线:卡=常量 313x341;竖条宽==BAR_V_W;横条高≤34
            if form == "card" and (img.width(), img.height()) != (
                    m.MeterWindow.CARD_W, m.MeterWindow.CARD_H):
                failures.append(
                    f"{tag}: 卡尺寸 {img.width()}x{img.height()} != "
                    f"{m.MeterWindow.CARD_W}x{m.MeterWindow.CARD_H}")
            if form == "v-bar" and img.width() != m.MeterWindow.BAR_V_W:
                failures.append(
                    f"{tag}: 竖条宽 {img.width()} != BAR_V_W"
                    f"({m.MeterWindow.BAR_V_W})")
            if form == "h-bar" and img.height() > 34:
                failures.append(f"{tag}: 横条高 {img.height()} > 34")
            cc = color_count(img)
            if cc < 12:
                failures.append(f"{tag}: 画面近似纯色(deco/grab 失效?)")
            # 皮肤生效检查:非 glass 的顶缘中点像素必须 ≠ glass(九款底色
            # 互异,该点即指纹;deco 没接/回退玻璃时逐款同色当场暴露)
            if form == "card":
                px = top_pixel(img)
                if sid == "glass":
                    glass_top = px
                elif glass_top is not None and px == glass_top:
                    failures.append(f"{tag}: 顶缘采样与 glass 同色(皮肤未生效?)")
            print(f"  rendered {tag}: {img.width()}x{img.height()} colors={cc}")

    win.close()
    if failures:
        print("INVALID: 渲染矩阵不合规")
        for f_ in failures:
            print(f"  - {f_}")
        return 1

    matrix = compose_matrix(cells)
    matrix.save(str(MATRIX_PATH), "PNG")
    wall = compose_wallpaper(cells)
    wall.save(str(WALLPAPER_PATH), "PNG")
    print(f"OK matrix -> {MATRIX_PATH} ({matrix.width()}x{matrix.height()})")
    print(f"OK wallpaper -> {WALLPAPER_PATH} ({wall.width()}x{wall.height()})")
    print("人工比对指引:逐款对照 design/skins-8x3.html(参照物 zm_skins_full.png);"
          "三微调对照 zm_fix_chalk.png / zm_fix_liquid.png / zm_fix_news.png;"
          "浅色两款另看 skins_wallpaper_check.png 深/浅壁纸边缘观感。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
