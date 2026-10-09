"""皮肤注册表(v0.9 T2 基建 + T4 八款视觉落地):九款皮肤的数据定义 +
QSS 模板渲染 + 纯 QPainter 背景装饰(deco)。

【导入红线】本模块只允许 import PySide6(QtGui + 本文件实际消费的 QtCore
类型,见下)、标准库、以及 zcode_meter 包自身的 __init__ 字面(CRT 版本行
运行时读 __version__,见 _deco_crt —— __init__ 是纯字面零 import,side-effect
为零)。严禁 import zcode_meter.app / zcode_meter.data_engine —— app 单向
import 本模块,反向依赖即循环导入;sources 包同款红线;data_engine 虽不
import 本模块(无环),但『皮肤层不拉数据层』是分层纪律,test_package 的
子进程红线断言钉死(import skins 后 sys.modules 必不含 data_engine/app)。
全部视觉常量以字面值写死,glass 侧与 app.py 的 C_* 常量互为镜像 —— 由
tests/test_stress.py 的『glass qss 逐位相等』+『glass 绘制色镜像 C_*』
双向断言钉死,任一侧改值而另一侧未跟,回归当场翻车。
deco 同样只消费 QtGui 侧绘制 API + QtCore 几何类型:QPoint(p.drawImage
定位,_deco_liquid 消费)与 QRect(_union 返回值注解)是仅有的两个 QtCore
消费点;QRectF 已删(未消费)。deco 内一律走 int/float 参数重载
(drawText(int,int) / drawEllipse(int×4) / addRoundedRect(qreal×6)),
几何一律取自 win.rect() / label.geometry() 的返回对象 —— 不依赖更多
QtCore 类型(『仅 QtGui』红线的字面遵守,防依赖面扩大)。
glass_effect 是唯一被本模块消费的同包兄弟模块,且只在 _deco_liquid 的
【函数体内】运行时 import(真背景优先路径):它在模块 import 期就可能
初始化屏幕捕获设备,deco 调用点高频于模块 import 点,晚绑定把该成本
留给真正需要真背景的 liquid 绘制路径,其余八款皮肤与所有模块级 import
零关联(app 与 skins 的 import 红线不适用于它:data_engine 不 import
glass_effect,无环)。

【T4 落地范围】八款 deco 按 design/skins-8x3.html 定稿逐款实现(纯
QPainter 零图片依赖:QRadialGradient 模拟 blob/光晕/粉笔灰,QPainterPath
裁剪模拟蒸汽波太阳 mask-composite;禁 QPixmap/SVG/webengine)。三处定稿
微调:①报纸量价双空格档经 today_cost_sep 落地(HTML :484);②液态玻璃
6 只等大药丸 3 列 2 行以 deco 药丸底锚定 grid 六格几何呈现(玻璃 grid
本就是 3 列×2 带,视觉与 HTML 等价);③黑板卡『入/出同一行』是标签重排,
需 _build_card 皮肤分支(app.py,不在 T4 文件清单,T2/T3 未落)—— 未落,
见 _deco_chalk 注释记录,不算已交付。

装饰文本(瑞士 LIVE/报纸报头/CRT 版本行/黑板标题/工业 SYSTEM NOMINAL/
蓝图图签)为 paintEvent 侧静态绘制,不占布局 → 不受 sizeHint/_bar_size
闸约束;玻璃竖条帽标 TOK/S 是既有 QLabel(app.py vcap),皮肤只改其色/
字族,禁止重复绘制(spec F 更正)。

对比度基准 contrast_bg 按评审 D 修订口径申报:该形态 deco 实际铺底中
【承载字段文本的面】的底色(亮装饰带不得充当)—— 由本模块逐款如实
申报、离屏探针(行中心带口径,见 T3 批次 t3_contrast_probe.py)实测
最坏承载面像素复核:蒸汽波=统计暗格与落日亮带最坏合成 #51213d/#503234、
工业=暗材铭牌/readout 暗屏系(#101614 为其代表值)、液态玻璃=【豁免】
(用户 2026-10-06 复看裁决:暗玻璃底衬破坏通透感『好丑』,移除 —— 真背景
透出的皮肤对比度随桌面背景不可控,contrast_bg 退化为 veil 兜底的代表值,
T5 断言对该款跳过,registry #70 改判 maintained);
T5 对比度断言消费本字段。

【F2 纪律延伸】palette 是唯一允许 rgba 字面的色表 —— 仅 soft/half/vstrong/
sep 四档(soft/half/vstrong 是半透明白/半透明主题色的 QSS objectName 字面,
app.py:100-115 N2 裁决同构;sep 是 QFrame#sep 的 QSS 背景,由 #79 焊接
断言钉死与 SkinDef.sep 恒等 —— liquid 双字段均 transparent,玻璃侧
sep 恒 hex #2c2f3a 不在此档)。其余键一律 QColor 可解析 hex。palette 仅供
QSS 模板渲染消费,绘制件(PulseIndicator/Sparkline/Ring)消费的是 QColor
实例字段,rgba 字面永不进 QColor(str) 构造(app.py:78-82 实测 isValid()
==False 会静默画黑 —— 本机 PySide6 6.11.2 复测确认,2026-09-28;
tests/test_stress.py『四档 QSS 侧豁免』与同一纪律)。

【黑板字族偏差(有据)】HTML 速度字体 Segoe Print 实测 2026-09-29:
14px Bold 行高 24 → 横条 _bar_size 高 40,破『横条高 ≤34』断言;规格
允许的 1-2px 降档不救(12px 仍 21 行高 → 条高 37),Comic Sans MS 14px
亦 35>34;各档字号 px 冻结且 app.py 无每皮肤字号钩子。故 chalk 的
font_mono_families 保持 Cascadia 领衔(与玻璃同),手写感由 deco 侧粉笔
字标题承接(自绘文本不经 sizeHint 闸,_deco_chalk 标题装订 families=
['Segoe Print'] —— 装饰字族经 _set_font 的 families 参传给装饰文本,
CJK 字形在 Segoe Print 缺字时仍由系统回退承接,但拉丁字/装饰标点拿到
手写族,2026-10-06 #72 修:此前标题装订未传族,实测落系统默认 YaHei,
『deco 侧承接』的声明是空话)。Georgia(报纸)实测 14px 行高 16 与
Cascadia 同档,横条高 32 ✓ 正常落地。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import (
    QColor, QFont, QLinearGradient, QPainterPath, QPen, QRadialGradient,
)

# 皮肤白名单(右键菜单『皮肤』子菜单 / _apply_skin 白名单 / save_config
# _norm_skin 校验,消费方 app.py)。与 data_engine.SKIN_IDS 是双侧字面
# 镜像:由 test_stress 的相等断言(tuple(m.data_engine.SKIN_IDS)==tuple(
# m.skins.SKIN_IDS))钉死,任一侧单改即测试当场变红。刻意不做单源
# import:数据层 UI 无关不变式(data_engine.py 模块头、_no_persist 刻意
# 不 import UI 模块、run_all data 组无 UI、test_data_engine.py 头部
# 『无 UI 依赖』)优先 —— skins 属 UI 模块(模块级 import PySide6),
# data_engine import 它会拖起整个 Qt。glass=缺省第 0 款恒居首。
SKIN_IDS = ("glass", "swiss", "crt", "chalk", "liquid",
            "industrial", "newspaper", "vaporwave", "blueprint")

# QSS 统一模板:渲染 app.py QSS 的全部 objectName selector(:93-104 逐项,
# 缩进/空格数逐位对齐原文件)。QWidget#root 的 border-radius: 10px 无
# background 属性不绘制任何东西,仅为 qss_bar 的 replace 派生链保活
# (app.py:105-118 的 v0.8.0 裁决,防静默断链)。
_QSS_TEMPLATE = """
QWidget#root {{ border-radius: 10px; }}
QLabel {{ color: {fg}; background: transparent; border: none; }}
QLabel#dim   {{ color: {dim}; }}
QLabel#accent {{ color: {accent}; }}
QLabel#warn  {{ color: {warn}; }}
QLabel#faint {{ color: {faint}; }}
QLabel#soft    {{ color: {soft}; }}
QLabel#half    {{ color: {half}; }}
QLabel#vstrong {{ color: {vstrong}; }}
QFrame#sep {{ background: {sep}; border: none; max-height: 1px; }}
"""


@dataclass(frozen=True)
class SkinDef:
    """单款皮肤的完整数据定义。色字段分两层:

    - palette:QSS 模板色键(fg/dim/accent/warn/faint/soft/half/vstrong/
      sep),值=hex 或 rgba 字面(soft/half/vstrong/sep 四档允许 rgba,
      sep 另允许 transparent,见模块头;#79 起 sep 与 SkinDef.sep 由
      stress 相等断言焊死同值);
    - spark_line/spark_dot/pulse_idle/pulse_active/pulse_core/ring_base:
      绘制件消费的 QColor 实例(含 alpha 档,hex 字面表达不了)。
    qss/qss_bar 由 palette 经统一模板派生(init=False,构造即定),
    qss_bar 与 app.py 同一条 replace 派生链(app.py:118)。

    圆角口径(#34):不再有 SkinDef.radius 影子字段 —— 各 deco 内嵌的
    {"card"/"h"/"v": 圆角px} dict 是唯一实现(与 HTML 定稿逐款对齐),
    app 侧 QSS 派生链(glass)与 paintEvent 玻璃路径按形态分档的常量
    不经本类。同理 font_decor(#71)已删:装饰文本的字族真正生效处是
    各 deco 内 _text(...,families=[...]) 的字面(报纸 Georgia/蓝图
    Consolas/黑板 Segoe Print),字段无人消费只会漂移。
    """

    id: str
    menu_label: str                      # 右键「皮肤」子菜单项文本(T3 消费)
    palette: dict                        # QSS 色键 → hex/rgba 字面
    spark_line: QColor                   # 速度折线(paint_sparkline)
    spark_dot: QColor                    # 折线末端点
    pulse_idle: QColor                   # 脉冲环 idle 静态空心环(含 α)
    pulse_active: QColor                 # 脉冲环 active 环/外扩圈基色
    pulse_core: QColor                   # 脉冲环内核(含 α)
    ring_base: QColor                    # 环形进度底环(含 α)
    sep: str                             # 条内分隔线背景(QSS 字符串)
    sep_card: str                        # 卡片主分节线背景(QSS 字符串)
    sep_card_weak: str                   # 卡片模型行弱分节线(骨架期补充字段:
                                         #   HTML/玻璃现值即两档,glass 逐位不变)
    contrast_bg: dict                    # {"card","h","v"} 对比度基准底色(T5 消费)
    today_cost_sep: str                  # 横条今日段 量↔金额 分隔符(玻璃恒单空格)
    font_mono_families: Tuple[str, ...]  # mk_mono families(玻璃=Cascadia+Consolas)
    deco: Optional[Callable] = None      # (painter, win, form) 背景装饰;
                                         #   None=走 paintEvent 玻璃兜底路径
    qss: str = field(init=False, default="")
    qss_bar: str = field(init=False, default="")

    def __post_init__(self):
        object.__setattr__(self, "qss", _QSS_TEMPLATE.format(**self.palette))
        # 与 app.py:118 同一条 replace 派生链(防静默断链)
        object.__setattr__(self, "qss_bar",
                           self.qss.replace("border-radius: 10px",
                                            "border-radius: 7px"))


# ---- 绘制侧 QColor 速记(含 α 档;hex 字面无法表达 alpha) ----
def _c(r: int, g: int, b: int, a: int = 255) -> QColor:
    return QColor(r, g, b, a)


def _dbg(msg: str) -> None:
    """skins 本地诊断日志(#36/#55a 治理后引入):与 data_engine.dbg 同
    格式落同文件 zm_debug.log、同 ZM_DEBUG=1 门控。刻意本地实现而
    不 import data_engine.dbg —— 模块头导入红线(skins 不得拉起数据层,
    test_package 子进程断言钉死)比复用一行代码更重要;app_dir 的
    『frozen 取 exe 目录 / 否则向上找含 README.md 的祖先目录』路径
    不变式在此逐字镜像(data_engine.py:app_dir docstring 钉死:改锚点
    会让用户现有 zm_config.json 无声失配 —— 两侧必须同步演进);
    data_engine 侧 dbg 与此处格式若漂移只影响日志读法,不影响运行行为。"""
    if os.environ.get("ZM_DEBUG") != "1":
        return
    import sys
    if getattr(sys, "frozen", False):
        d = os.path.dirname(os.path.abspath(sys.executable))
    else:
        d = here = os.path.dirname(os.path.abspath(__file__))
        for _ in range(3):
            d = os.path.dirname(d)
            if d == os.path.dirname(d):
                d = here
                break
            if os.path.isfile(os.path.join(d, "README.md")):
                break
        else:
            d = here
    try:
        with open(os.path.join(d, "zm_debug.log"), "a",
                  encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}."
                    f"{int(time.time()*1000)%1000:03d} {msg}\n")
    except OSError:
        pass                                # 日志失败不打断绘制链(静默)


# ════════════════ T4:deco 公共原语(纯 QPainter,仅 QtGui) ════════════════
# deco(painter, win, form) 由 MeterWindow.paintEvent 在已开 Antialiasing 的
# painter 上调用(form=_bar_form:None=卡 / "h" / "v")。deco 全权铺底,返回
# 后子控件(QLabel 等)再独立绘制 —— 局部底(药丸/暗格/readout/铭牌)锚定
# label 几何画在文字【下方】,数据变化/换挡后每次 paint 现读几何,自愈
# (不做任何几何缓存,v0.3.0『固定尺寸脱节』教训的 deco 版)。

def _base_path(win, r: int) -> QPainterPath:
    """窗口圆角底形(app.py _paint_glass 同款 0.5 内缩,描边不被裁半)。"""
    path = QPainterPath()
    path.addRoundedRect(0.5, 0.5, win.width() - 1.0, win.height() - 1.0,
                        float(r), float(r))
    return path


def _stroke(p, win, r: int, color: QColor, width: float = 1.0) -> None:
    """圆角描边(在 clip restore 之后画,与玻璃 1px 描边同款不裁半)。"""
    p.setPen(QPen(color, width))
    p.setBrush(QColor(0, 0, 0, 0))
    p.drawPath(_base_path(win, r))


def _set_font(p, px: int, families=None, spacing: float = 0.0,
              bold: bool = False) -> None:
    """装订装饰字体到 painter(量宽探针与绘制共用;只设字体不落笔)。"""
    f = QFont()
    f.setPixelSize(px)
    if families:
        f.setFamilies(list(families))
    if bold:
        f.setBold(True)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, float(spacing))
    p.setFont(f)


def _text_w(p, txt: str) -> int:
    """当前字体下文本宽(必须先 _set_font)。"""
    return p.fontMetrics().horizontalAdvance(txt)


def _text(p, txt: str, x: float, y: float, px: int, color: QColor,
          families=None, spacing: float = 0.0, bold: bool = False) -> None:
    """装饰文本(x=左缘、y=baseline;不占布局,不经 sizeHint 闸)。"""
    _set_font(p, px, families, spacing, bold)
    p.setPen(QPen(color))
    p.drawText(int(round(x)), int(round(y)), txt)


def _card_grid_vs(win) -> list:
    """卡片 grid 六格 v label(F4 卡列恒建恒显;grid 直挂 win 根布局,
    geometry() 即 win 坐标)—— 药丸/暗格/铭牌的锚。"""
    return [getattr(win, n) for n in
            ("in_out_lbl", "rate_lbl", "timing_lbl",
             "burn_lbl", "avg_burn_lbl", "avg_lbl")
            if getattr(win, n, None) is not None]


def _union(rects) -> "QRect":
    """矩形并集。空列表返回无效 QRect()(isNull)而非 IndexError ——
    #55a:paintEvent 可能早于首次 _build_card(app.py 自述),deco 锚
    点集合可能为空,交由调用方 isNull() 跳过绘制(裸崩的 _union([])
    离屏实测杀 paintEvent,painter 残留未配对 save)。"""
    if not rects:
        return QRect()
    r = rects[0]
    for q in rects[1:]:
        r = r.united(q)
    return r


def _pill_rect(lb, dx: int, up: int, dn: int):
    """单格局部底矩形:k 行(9px 字 ≈12h)+ grid 纵距 4 在 v label 上方,
    up≈k 行高+间距+呼吸。geometry() 每次 paint 现读(布局/显隐自愈)。"""
    return lb.geometry().adjusted(-dx, -up, dx, dn)


def _hbar_group_labels(win) -> list:
    """横条可关段 label:未建(None,段开关)/隐藏(数据缺席)都跳过 ——
    局部底只跟『真在场』的文字,不悬空。"""
    out = []
    for n in ("plan_lbl", "plan_cd_lbl", "today_lbl", "burn_lbl"):
        lb = getattr(win, n, None)
        if lb is not None and not lb.isHidden():
            out.append(lb)
    return out


def _vbar_group_labels(win) -> list:
    """竖条四组的值行 label(k 行 caption 多为局部变量未存引用,局部底只
    包值行 —— HTML .st/.p2 以值为视觉主体,可接受近似)。"""
    out = []
    for n in ("today_lbl", "plan_lbl", "burn_lbl", "in_lbl"):
        lb = getattr(win, n, None)
        if lb is not None and not lb.isHidden():
            out.append(lb)
    return out


def _striped_sun(p, cx: float, cy: float, r: float,
                 stops, on: int, off: int) -> None:
    """蒸汽波落日条纹太阳(HTML mask: repeating-linear-gradient +
    mask-composite intersect 的 QPainter 等价物):圆内按 [on 条,off 隙]
    横条带填充同一垂直渐变 —— 渐变坐标锚在整圆上,色相跨条带连续。"""
    circle = QPainterPath()
    circle.addEllipse(cx - r, cy - r, r * 2.0, r * 2.0)
    grad = QLinearGradient(0.0, cy - r, 0.0, cy + r)
    for t, col in stops:
        grad.setColorAt(t, col)
    p.save()
    p.setClipPath(circle)
    y = cy - r
    while y < cy + r:
        p.fillRect(int(round(cx - r)), int(round(y)), int(round(r * 2.0)),
                   int(on), grad)
        y += on + off
    p.restore()


def _vp_plate(p, rect) -> None:
    """蒸汽波统计暗格(HTML :217/:227/:238 rgba(13,5,24,.72)+青 .4 边):
    落日亮带上的文字必须有暗底(contrast_bg 申报口径)。2026-10-04 P1
    起也铺到主数字行 —— HTML :208-209/:237 的速度数字压太阳靠双色
    text-shadow(#ff2e97/#00e5ff 偏移影)保分离,Qt 侧是裸 QLabel 无逐件
    描边通道(全库零 setGraphicsEffect,translucent+layered 窗口不引入
    QGraphicsDropShadowEffect),移植时补偿被整段丢弃 → 青字直压条纹
    (v 形态实测 67% 字形面积,对黄段 1.04:1、粉/红 1.77~1.92:1,均低
    于 WCAG 大文本 3:1)。以本皮肤自己的词汇补:同款暗格底衬,最坏
    合成底(衬压太阳最亮带)上青字 ≈7.4:1。"""
    p.setPen(QPen(QColor(0, 229, 255, 102)))
    p.setBrush(QColor(13, 5, 24, 184))
    p.drawRect(rect)


# ══════ ① 瑞士国际主义(HTML :26-51):奶白板 + 墨字 + 红点 LIVE ══════

def _deco_swiss(p, win, form) -> None:
    r = 4 if form is None else 3                     # :27/:38/:45
    path = _base_path(win, r)
    p.fillPath(path, QColor("#f4f2ec"))
    w = win.width()
    p.save()
    p.setClipPath(path)
    if form is None:
        # 右上 LIVE/IDLE 徽标(:289,红点 #e2503c + 墨字 letterspaced):
        # 按 win.snap.state 生成中=LIVE/空闲=IDLE(spec 钉死);顶部 14px
        # 边距带是布局外空当,与状态行(model 名 y≈14 起)零重叠。
        # #55a:getattr 守卫与其余六款同纪律 —— paintEvent 可能早于
        # 首次 _build_card(app.py 自述),该窗口态下直接属性访问
        # AttributeError 裸崩(paint 链 painter 残留未配对 save)。
        snap = getattr(win, "snap", None)
        st = ("LIVE" if snap.state == "generating"
              else "IDLE") if snap is not None else "IDLE"
        _set_font(p, 9, spacing=2.0, bold=True)
        tw = _text_w(p, st)
        x = w - 16 - tw
        _text(p, st, x, 11, 9, QColor("#141414"), spacing=2.0, bold=True)
        p.setPen(QPen(QColor(0, 0, 0, 0)))
        p.setBrush(QColor("#e2503c"))
        p.drawEllipse(x - 12, 4, 7, 7)
        # 大数字行下 2px 墨规(:29 sw-top border-bottom):锚 tps/unit/spark
        # 行几何,行底 +5 —— 信息集恒玻璃全量,规只作分隔不做裁切
        lbs = [getattr(win, n, None) for n in
               ("tps_lbl", "tps_unit_lbl", "spark")]
        lbs = [lb for lb in lbs if lb is not None]
        row = _union([lb.geometry() for lb in lbs])
        if not row.isNull():
            p.fillRect(16, row.bottom() + 5, w - 32, 2, QColor("#141414"))
    p.restore()
    # HTML 卡无 border(仅投影),浅底即边,不描


# ══════ ② 琥珀 CRT 终端(HTML :53-78):#100800 + 扫描线 + 暗棕边框 ══════

def _deco_crt(p, win, form) -> None:
    r = {"card": 10, "h": 6, "v": 8}[form or "card"]   # :54/:64/:71
    path = _base_path(win, r)
    p.fillPath(path, QColor("#100800"))
    p.save()
    p.setClipPath(path)
    # 扫描线(:56 repeating-linear-gradient 1px/3px,α≈8)
    ln = QColor(255, 176, 0, 8)
    y = 0
    while y < win.height():
        p.fillRect(0, y, win.width(), 1, ln)
        y += 3
    # 内沉辉光(:55 inset 0 0 40px amber .06 的近似:边缘向内的琥珀微光)
    glow = QRadialGradient(win.width() / 2.0, win.height() / 2.0,
                           max(win.width(), win.height()) * 0.75)
    glow.setColorAt(0.0, QColor(255, 176, 0, 0))
    glow.setColorAt(0.72, QColor(255, 176, 0, 0))
    glow.setColorAt(1.0, QColor(255, 176, 0, 26))
    p.fillRect(0, 0, win.width(), win.height(), glow)
    if form is None:
        # 版本行(:323『ZCODE-METER v? / TTY-01』):顶部边距带左右分列,
        # 状态行 y≈14 起,零重叠。版本段运行时读 zcode_meter.__version__
        # (#35,2026-10-06):此前字面 "ZCODE-METER v0.8" 是全仓第四处
        # 版本叙事(__init__/CHANGELOG/test_package 各一处),升版永远
        # 漏同步 —— f-string 动态读使 __init__ 升版本行自动跟随。函数体
        # 内 import 包自身 __init__(纯字面零副作用,见模块头导入红线);
        # sys.modules 命中后是常量级 dict 查询,paint 热路径无感。
        from zcode_meter import __version__ as _ver
        amber = QColor(255, 176, 0, 178)
        _text(p, f"ZCODE-METER v{_ver}", 18, 11, 9, amber, spacing=2.0)
        _set_font(p, 9, spacing=2.0)
        tw = _text_w(p, "TTY-01")
        _text(p, "TTY-01", win.width() - 18 - tw, 11, 9, amber, spacing=2.0)
    p.restore()
    _stroke(p, win, r, QColor("#3a2a08"), 2.0)          # :54 border 2px


# ══════ ③ 黑板粉笔(HTML :80-103):木框 #7a5a38 + 墨绿板面 + 粉笔灰 ══════

def _deco_chalk(p, win, form) -> None:
    # 框宽:卡 9(:81)/横 6(:93)/竖 7(:98);外圆角=radius,内圆角随框收
    fw = {"card": 9, "h": 6, "v": 7}[form or "card"]
    r = {"card": 6, "h": 5, "v": 5}[form or "card"]
    w, h = win.width(), win.height()
    p.fillPath(_base_path(win, r), QColor("#7a5a38"))   # 木框
    ir = max(r - fw // 2, 2)
    board = QPainterPath()
    board.addRoundedRect(fw + 0.5, fw + 0.5, w - 2 * fw - 1.0,
                         h - 2 * fw - 1.0, float(ir), float(ir))
    grad = QLinearGradient(0.0, fw, w * 0.6, h - fw)    # :81 160deg 三站
    grad.setColorAt(0.0, QColor("#2e3b34"))
    grad.setColorAt(0.6, QColor("#26332c"))
    grad.setColorAt(1.0, QColor("#223028"))
    p.fillPath(board, grad)
    p.save()
    p.setClipPath(board)
    # 两团粉笔灰(:83-85 radial 白.04/.03 的可视增强档:纯色底上 α12/α9)
    for fx, fy, fr, a in ((0.30, 0.20, 0.30, 12), (0.72, 0.80, 0.28, 9)):
        dust = QRadialGradient(w * fx, h * fy, max(w * fr, 24.0))
        dust.setColorAt(0.0, QColor(255, 255, 255, a))
        dust.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(0, 0, w, h, dust)
    p.restore()
    if form is None:
        # 『今 日 账 目』(:358):HTML 在卡顶,但玻璃全量信息集 + 冻结
        # 341 高下顶部边距带只有 14px 且被状态行(y≈14 起)顶满 —— 落在
        # 底部边距带(居中粉笔字,8px:12px 底边距带内 8px 字 ascent 7+
        # descent 2,基线 h-3 恰容下且不叠模型行尾行);『入/出同一行』
        # (:361)为标签重排,需 _build_card 皮肤分支(app.py,非 T4 文件)
        # —— 未落,卡片 grid 维持玻璃 3 列×2 带结构,特此记录不算已交付。
        # P1(2026-10-08):标题必须画在 restore(板面 clip)之后 —— clip
        # 底缘 h-fw-0.5(卡形态 331.5),而底部边距带(app.py root 底边距
        # 12,内容止于 h-12=329)与板面交集只有 ~3px,基线 h-3 的字形
        # (≈[h-10, h-1])全高落在 clip 之外:原实现标题经 setClipPath
        # 整体裁没,离屏实测整卡 0 个粉笔字像素,该装饰从不出现。移出
        # clip 后墨迹落在无子控件地带(内容区底 329 与字顶 ~331 不叠),
        # 底部 9px 压在木框上 —— 粉笔白字落在木框即铭牌观感,可接受;
        # 板内另无 ≥8px 空带(顶部边距带 4.5px 同样放不下)是冻结 341
        # 高下的几何事实。
        # 标题装订 families=['Segoe Print'](#72,2026-10-06):模块头
        # 『手写感由 deco 侧粉笔字标题承接』的补偿机制此前不存在 ——
        # 装订不传族实测落系统默认 YaHei,声明是空话;装饰文本不经
        # sizeHint 闸,传族零成本,拉丁字/装饰标点拿手写族(CJK 缺字
        # 系统回退承接)。对照机制实测传族生效(#72 探针)。
        t = "今 日 账 目"
        _set_font(p, 8, families=["Segoe Print"], spacing=3.0)
        tw = _text_w(p, t)
        _text(p, t, (w - tw) / 2.0, h - 3.0, 8,
              QColor(232, 230, 223, 210), families=["Segoe Print"],
              spacing=3.0)
    # HTML 木框为 border(无外描边),框即边


# ══════ ④ 液态玻璃药丸(HTML :105-139):暗底 + 双 blob + 白玻璃 + 药丸 ══════

def _liquid_bg(p, win, form) -> QPainterPath:
    r = {"card": 26, "h": 18, "v": 22}[form or "card"]  # :106/:122/:131
    w, h = win.width(), win.height()
    path = _base_path(win, r)
    # 暗幕(stage.dark 透过)。条形态(高30px)整体提亮一档:全黑幕+粗描边
    # 在细条上读作『黑色外框』(用户反馈 2026-09-29),改亮灰蓝幕+细描边。
    # 卡片同病第二轮(用户『黑色的打底还是没有去掉』):#0e1118 近黑幕撑满
    # 整卡,HTML 的透亮玻璃感全无 —— 卡片也换亮灰蓝幕(比条略深保文字对
    # 比),blob 半径/α 加大为视觉主体,『彩泡在浅玻璃后晕开』才成立。
    # (2026-10-06 #36:T4 期曾有 acrylic_native 原生 backdrop 分支 —— 半透
    # 底让系统模糊层透出;该路线因 dxcam 抓屏自反射残影被整体放弃,生产侧
    # 属性(app.py _enable_acrylic)已随死方法链删除,acrylic_native 运行期
    # 恒 False 后本 getattr 读取点成为死分支 —— 塌缩为自绘常值。真背景
    # 路线的系统模糊层体验由 glass_effect 抓屏管线承接,非 QSS 半透明。)
    base = QLinearGradient(0.0, 0.0, 0.0, float(h))
    if form in ("h", "v"):
        base.setColorAt(0.0, QColor("#2a3140"))
        base.setColorAt(1.0, QColor("#232a37"))
    else:
        base.setColorAt(0.0, QColor("#39415a"))
        base.setColorAt(1.0, QColor("#2b3247"))
    p.fillPath(path, base)
    p.save()
    p.setClipPath(path)
    # 双 blob(#38bdf8/#a78bfa,HTML filter:blur → QRadialGradient 模拟;
    # 卡 :110-111 120/100px 在左上/右下,条 :125-126 60px 缩小档)。
    # 卡片档 α 150/140→190/175、半径 95/88→120/110:亮幕下彩泡要撑得住
    blobs = ((44.0, 34.0, 120.0, 190, 56, 189, 248),
             (w - 50.0, h - 44.0, 110.0, 175, 167, 139, 250))
    if form == "h":
        blobs = ((52.0, 0.0, 55.0, 128, 56, 189, 248),
                 (w - 90.0, float(h), 55.0, 115, 167, 139, 250))
    elif form == "v":
        blobs = ((w / 2.0 + 4.0, 36.0, 66.0, 135, 56, 189, 248),)
    for bx, by, br, ba, cr, cg, cb in blobs:
        g = QRadialGradient(bx, by, br)
        g.setColorAt(0.0, QColor(cr, cg, cb, ba))
        g.setColorAt(1.0, QColor(cr, cg, cb, 0))
        p.fillRect(0, 0, w, h, g)
    # 白玻璃面板(:107 150deg 白.16→白.05 的对角近似)。card 的中段 alpha
    # 在 hero 区横穿出一条亮带(用户截图『很多地方看不到/横线』),41→24
    ov = QLinearGradient(0.0, 0.0, w * 0.6, float(h))
    ov.setColorAt(0.0, QColor(255, 255, 255, 41 if form is None else 34))
    ov.setColorAt(1.0, QColor(255, 255, 255, 13))
    p.fillRect(0, 0, w, h, ov)
    # 第三轮补充(用户『底部的黑幕的边界还是在』):药丸区被白.14 垫亮,
    # 幕布渐变到卡底最暗,两者交界显出一条水平边界 —— 幕布尾部再提亮一档
    # (#2b3247→#333a4f),底部与药丸区亮度衔接
    if form is None:
        tail = QLinearGradient(0.0, h * 0.55, 0.0, float(h))
        tail.setColorAt(0.0, QColor(51, 58, 79, 0))
        tail.setColorAt(1.0, QColor(63, 71, 94, 130))
        p.save()
        p.setClipPath(path)
        p.fillRect(0, 0, w, h, tail)
        p.restore()
    p.restore()
    return path


# ---- liquid 文本行暗玻璃底衬(#70,2026-10-06)----
# blob 是本皮肤的视觉主体,但其峰值(α190/135 cyan @blob1 中心、α175/115
def _lens_edge(p, win, r: int, form: str) -> None:
    """Liquid Glass 方案 B:边缘透镜(QPainter 自绘,零平台依赖)。
    折射亮线 + 光密暗带,全部 clip 进圆角 path —— 笔宽中心在边线上时
    圆角外溢出 3.5px 暗色,被 layered 窗口在四角放大成『直角块』
    (用户 2026-09-29 四角直角最终真因)。
    2026-10-06 注:本函数曾在移除暗玻璃底衬的清理中被误删
    (4767 字符块里混着它),真背景路径 _lens_edge 抛 NameError 被
    except 吞掉后『优雅降级』落 veil = 用户所见假背景,从 git HEAD
    原样恢复。"""
    path = _base_path(win, r)
    p.save()
    p.setClipPath(path)                    # 一切边缘光效不得越出圆角
    # ① 折射亮线:细白圈
    p.setPen(QPen(QColor(255, 255, 255, 76), 1.2))
    p.setBrush(QColor(0, 0, 0, 0))
    p.drawPath(path)
    # ② 光密暗带:粗黑线的一半被 clip 裁掉,只剩圆角内侧 3.5px
    p.setPen(QPen(QColor(0, 0, 0, 20), 7.0))
    p.setBrush(QColor(0, 0, 0, 0))
    p.drawPath(path)
    p.restore()
    # ③ 镜面高光弧已去除(用户 2026-09-29『背景里面这两个曲线去掉』——
    #    drawArc 弧线横穿卡片被读成多余曲线;折射亮线+暗带已足够)


def _deco_liquid(p, win, form) -> None:
    r = {"card": 26, "h": 18, "v": 22}[form or "card"]

    # 真背景优先(2026-09-29 卡顿优化重排):先试 view(),有帧直接画帧
    # +收边返回 —— _liquid_bg 的 veil(渐变填充+双径向渐变 blob+尾部
    # 提亮,合计 ~0.5ms raster)只在不透明帧【完全盖住它】的场景下白画,
    # 现在彻底移出有帧路径;无帧(dxcam 缺失/管线未启动)才落 veil。
    try:
        from zcode_meter import glass_effect
        if glass_effect.OK:
            bg = glass_effect.view(win)
            if bg is not None:
                img = glass_effect.to_qimage(bg)
                # 图带 devicePixelRatio → QPoint 定位 = 1:1 位块传输
                # (帧物理尺寸=窗口物理尺寸,免每帧缩放重采样);clip 用
                # 独立路径(不依赖 _liquid_bg 的返回值)。#36(2026-10-06):
                # 原裸 except Exception: pass 会把 setClipPath/drawImage
                # 抛异常后未配对的 p.save() 一路带进 veil 兜底(Unbalanced
                # save/restore + clip 残留),且零诊断 —— 故 try/finally
                # 保证 restore 配对,异常经 _dbg 落 zm_debug.log 后按
                # 优雅降级落 veil(painter 链路恒可继续,玻璃不真了至少
                # 有第一现场线索可查,不再『玻璃不真了』无从排查)。
                clip = _base_path(win, r)
                p.save()
                try:
                    p.setClipPath(clip)
                    p.drawImage(QPoint(0, 0), img)
                finally:
                    p.restore()
                _lens_edge(p, win, r, form)
                return
    except Exception:
        import traceback
        _dbg("liquid deco: real-bg path failed, veil fallback: "
             + traceback.format_exc()[-200:])
    path = _liquid_bg(p, win, form)   # 回退:自绘 veil 亮幕
    # 药丸绘制已全部移除(用户 2026-09-29『横线、竖线和药丸全部都不要,
    # 要像前一版的卡片一样干净纯粹』):卡片六格药丸/横条段底/竖条组底
    # 一并去除 —— 模糊背景 + 文字直读,玻璃本身即层次(前十轮药丸迭代
    # 的机制代码保留在 git 历史,_pill_geo 缓存链路不再被消费)。
    _lens_edge(p, win, r, form)


# ══════ ⑤ 工业机柜(HTML :141-173):金属渐变 + 螺丝/绿灯 + readout/铭牌 ══════

def _deco_industrial(p, win, form) -> None:
    r = {"card": 14, "h": 8, "v": 10}[form or "card"]   # :142/:163/:169
    w, h = win.width(), win.height()
    path = _base_path(win, r)
    grad = QLinearGradient(w * 0.2, 0.0, w * 0.8, float(h))  # :142 160deg
    grad.setColorAt(0.0, QColor("#3a3f45"))
    grad.setColorAt(1.0, QColor("#23272c"))
    p.fillPath(path, grad)
    p.save()
    p.setClipPath(path)
    if form is None:
        # readout 暗屏(:148 #101614):锚主数字行(tps+unit,spark 在外)
        # #55a:getattr 守卫与其余六款同纪律 —— paintEvent 可能早于首次
        # _build_card(app.py 自述),锚件缺席跳过绘制(此前直接属性访问
        # AttributeError 裸崩 + _union([]) IndexError,离屏实测全三形态)
        _nbs = [getattr(win, n, None) for n in ("tps_lbl", "tps_unit_lbl")]
        _nbs = [lb for lb in _nbs if lb is not None]
        if _nbs:
            ro = _union([lb.geometry() for lb in _nbs]).adjusted(-10, -4, 10, 4)
            p.setPen(QPen(QColor("#0a0f0d")))
            p.setBrush(QColor("#101614"))
            p.drawRoundedRect(ro, 4, 4)
        # 铭牌(:151):HTML 为亮黄铜 #c8b891→#b3a276 配深字 #2b2620;但
        # 冻结信息集下 grid 字色走全局 QSS 槽(fg/faint,同槽还服务深底
        # 文本),亮铜底配浅字只剩 ~2.2:1。改为 readout 同材暗 panel
        # (#101614 系)+ 黄铜描边(#8a7d5c)—— 铜质特征保留在边,字段文本
        # 的真实局部底回到与 contrast_bg 申报(#101614)一致的暗材
        # (T5 对比度闸口径不破;首版亮铜实测 2026-09-29 矩阵对版否决)
        #
        # #89(2026-10-06)铭牌向下延伸覆盖模型行:此前铭牌只包 grid 六
        # 格,模型行(faint 10px 名 + half 10px 速度)直接落在金属渐变
        # (#3a3f45→#23272c)上 —— 离屏探针实测 half 档真实金属底上仅
        # 3.85~4.43:1(whole-metal WCAG 复算全程 3.55~4.53 无处真正过
        # 4.5),而旧申报按 readout 暗屏 #101614 算 5.15 放行 —— 与
        # 『liquid contrast_bg 三重失真』同类项。修法=同材质最小改动:
        # 既有暗材铭牌(#151b18→#0e1411,黄铜边)向下延到模型行容器底,
        # 字段文本(含模型行)真实承载面全部回到暗材铭牌面;离屏实测
        # (行中心带口径)延伸后全场最坏 half≈4.9(铭牌顶缘最亮 #151b18
        # 上)、≥4.5 达标,contrast_bg #101614 维持(其在铭牌渐变
        # #151b18~#0e1411 区间内,复核结论记于 SkinDef 申报注释)。
        # P1(2026-10-09)底缘钳制+绘制序:#89 向下延伸后铭牌底(模型行底
        # +6≈h-6)恒伸进底部状态带 —— 不透明铭牌同 painter 后画,把先画的
        # 绿灯(y=h-13..h-4)、SYSTEM NOMINAL 字形(基线 h-5)、两颗底角螺丝
        # (y=h-19..h-11)整套盖死(探针实测绿灯仅剩 2px 绿条/文本墨迹行全 0/
        # 底螺丝中心=铭牌暗色)。修法两半:① 铭牌底缘钳到 h-14:模型行墨迹
        # (末行 label 底=h-13,10px 字墨迹实测≤h-14)仍全落暗材,#89 对比度
        # 闸不回退,黄铜边不再压绿灯/文本;② 底角螺丝与绿灯/SYSTEM
        # NOMINAL 挪到铭牌【之后】绘制 —— 螺丝带 y=h-19..h-11 与模型行末行
        # label(y=h-22..h-13)同高,任何覆盖末行墨迹的铭牌几何上必然压到
        # 螺丝,后画让状态指示结构性不可被铭牌覆盖,不再依赖 ① 的钳制常数。
        _cells = _card_grid_vs(win)
        _plate_rects = []
        if _cells:
            _g = _union([lb.geometry() for lb in _cells])
            if not _g.isNull():
                _plate_rects.append(_g.adjusted(-14, -28, 14, 9))
        _models = getattr(win, "model_rows", None)     # 容器锚(含四行+弱线)
        if _models is not None and not _models.geometry().isNull():
            _plate_rects.append(_models.geometry().adjusted(-14, -4, 14, 6))
        if _plate_rects:
            plate = _union(_plate_rects)
            if plate.bottom() > h - 14:            # ① 底缘钳制(见上)
                plate.setBottom(h - 14)
            pg = QLinearGradient(0.0, plate.top(), 0.0, float(plate.bottom()))
            pg.setColorAt(0.0, QColor("#151b18"))
            pg.setColorAt(1.0, QColor("#0e1411"))
            p.setPen(QPen(QColor("#8a7d5c"), 1.0))
            p.setBrush(pg)
            p.drawRoundedRect(plate, 6, 6)
        # 四角螺丝(:144 radial #9aa2ab→#4a5058,高光偏左上)。P1(2026-
        # 10-09)从铭牌前挪到铭牌后(见上②):底角两颗上半落在铭牌暗材、
        # 下半在金属,跨铭牌底缘 = 面板压在螺丝上的工业形态,且永不被铭牌
        # 盖死;顶角两颗与铭牌无交集,先后无差,随整组搬移保持一处代码。
        for sx, sy in ((11, 11), (w - 19, 11), (11, h - 19), (w - 19, h - 19)):
            sg = QRadialGradient(sx + 3, sy + 2.4, 5.2)
            sg.setColorAt(0.0, QColor("#9aa2ab"))
            sg.setColorAt(1.0, QColor("#4a5058"))
            p.setPen(QPen(QColor(0, 0, 0, 0)))
            p.setBrush(sg)
            p.drawEllipse(sx, sy, 8, 8)
        # 绿灯 + 辉光(:146 #4ade80 + glow)与 SYSTEM NOMINAL(:435):底部
        # 边距带 —— 顶部同位被状态行占用(玻璃全量信息集,顶行不空)。
        # 同 P1(2026-10-09)② 挪到铭牌后:绿灯/文本字形顶=h-13,钳制后
        # 铭牌本已够不着,后画是把『状态带不可被铭牌覆盖』钉成绘制序事实
        # (辉光是灯光,压在铭牌底缘上也符合物理)。
        lg = QRadialGradient(20.5, h - 9.0, 9.0)
        lg.setColorAt(0.0, QColor(74, 222, 128, 110))
        lg.setColorAt(1.0, QColor(74, 222, 128, 0))
        p.fillRect(0, h - 22, 44, 22, lg)
        p.setPen(QPen(QColor(0, 0, 0, 0)))
        p.setBrush(QColor("#4ade80"))
        p.drawEllipse(16, h - 13, 9, 9)
        _text(p, "SYSTEM NOMINAL", 31, h - 5.0, 8, QColor("#9aa2ab"),
              families=["Consolas"], spacing=2.0)
    elif form == "h":
        _nbs = [getattr(win, n, None) for n in ("tps_lbl", "tps_unit_lbl")]
        _nbs = [lb for lb in _nbs if lb is not None]
        if _nbs:
            ro = _union([lb.geometry() for lb in _nbs]) \
                .adjusted(-9, -3, 9, 3)            # :165 mini readout
            p.setPen(QPen(QColor("#0a0f0d")))
            p.setBrush(QColor("#101614"))
            p.drawRoundedRect(ro, 3, 3)
    else:
        _tp = getattr(win, "tps_lbl", None)
        if _tp is not None:
            ro = _tp.geometry().adjusted(-4, -7, 4, 7)   # :171 ro
            p.setPen(QPen(QColor("#0a0f0d")))
            p.setBrush(QColor("#101614"))
            p.drawRoundedRect(ro, 3, 3)
    # 顶缘内高光(:143 inset 0 1px 0 白.1 的等价线)
    p.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
    p.drawLine(int(r), 2, int(w - r), 2)
    p.restore()
    _stroke(p, win, r, QColor("#15181b"))               # :143 border


# ══════ ⑥ 报纸头版(HTML :175-199):奶白纸面 + 报头双细线 + 方角 ══════

def _deco_newspaper(p, win, form) -> None:
    r = 0                                               # :176/:186/:193 方角
    path = _base_path(win, r)
    p.fillPath(path, QColor("#f7f3ea"))
    w = win.width()
    p.save()
    p.setClipPath(path)
    ink = QColor("#1a1a1a")
    if form is None:
        # 报头(:470『The Meter Times / 今日号 · 第 1024 期』):顶部 14px
        # 边距带只容一行(HTML 两行的压缩保真,两段文本都在),其下 3px
        # double 细线(:177)= 1px+隙+1px 两线;状态行 y≈14 起,规则线贴其上
        t1 = "The Meter Times"
        t2 = " · 今日号 · 第 1024 期"
        _set_font(p, 10, families=["Georgia"], spacing=3.0)
        w1 = _text_w(p, t1)
        _set_font(p, 8, families=["Georgia"], spacing=1.0)
        w2 = _text_w(p, t2)
        x = (w - (w1 + w2)) / 2.0
        _text(p, t1, x, 10, 10, ink, families=["Georgia"], spacing=3.0)
        _text(p, t2, x + w1, 10, 8, QColor("#888888"), families=["Georgia"],
              spacing=1.0)
        p.fillRect(16, 12, w - 32, 1, ink)
        p.fillRect(16, 14, w - 32, 1, ink)
    elif form == "h":
        # :186 border-top 3px solid + border-bottom 1px
        p.fillRect(0, 0, w, 3, ink)
        p.fillRect(0, win.height() - 1, w, 1, ink)
    else:
        # :193 border-top 3px + 报眉『METER TIMES』(:491)
        p.fillRect(0, 0, w, 3, ink)
        t = "METER TIMES"
        _set_font(p, 7, families=["Georgia"], spacing=2.0)
        tw = _text_w(p, t)
        _text(p, t, (w - tw) / 2.0, 13, 7, ink, families=["Georgia"],
              spacing=2.0)
    p.restore()
    # 纸面无圆角无描边,方角即边


# ══════ ⑦ 蒸汽波落日(HTML :201-240):落日渐变 + 条纹太阳 + 透视网格 ══════

def _deco_vaporwave(p, win, form) -> None:
    r = {"card": 12, "h": 8, "v": 10}[form or "card"]   # :202/:220/:230
    w, h = win.width(), win.height()
    path = _base_path(win, r)
    sky = QLinearGradient(0.0, 0.0, 0.0, float(h))
    if form is None:
        # :203 五站落日(62%~70% 亮带到 #1a0b2e 暗带的硬切换)
        for t, col in ((0.0, "#2b1055"), (0.40, "#7b2d8b"), (0.62, "#ff6b9d"),
                       (0.70, "#ff9a5a"), (0.705, "#1a0b2e"), (1.0, "#1a0b2e")):
            sky.setColorAt(t, QColor(col))
    elif form == "h":
        # :221 横向落日(90deg)
        sky = QLinearGradient(0.0, 0.0, float(w), 0.0)
        for t, col in ((0.0, "#2b1055"), (0.55, "#7b2d8b"), (1.0, "#ff6b9d")):
            sky.setColorAt(t, QColor(col))
    else:
        # :231 竖向三站
        for t, col in ((0.0, "#2b1055"), (0.55, "#7b2d8b"), (1.0, "#1a0b2e")):
            sky.setColorAt(t, QColor(col))
    p.fillPath(path, sky)
    p.save()
    p.setClipPath(path)
    cyan = QColor(0, 229, 255)
    if form is None:
        # 条纹太阳(:204-207,8 on/3 off)
        _striped_sun(p, w / 2.0, 40.0, 45.0,
                     ((0.0, QColor("#ffe25a")), (0.55, QColor("#ff6b9d")),
                      (1.0, QColor("#ff5a5a"))), 8, 3)
        # 透视网格(:211-215,底部 30%):灭点中上,竖线向下扇形展开 +
        # 横线指数间距,青 .5 的 α 档(96/140)
        hz = h * 0.70
        for i in range(-7, 8):
            p.setPen(QPen(QColor(0, 229, 255, 96), 1.0))
            p.drawLine(int(w / 2 + i * 7), int(hz),
                       int(w / 2 + i * (w / 7.0)), h)
        for t in (0.05, 0.13, 0.24, 0.40, 0.62, 1.0):
            p.setPen(QPen(QColor(0, 229, 255, int(56 + t * 84)), 1.0))
            p.drawLine(0, int(hz + (h - hz) * t), w, int(hz + (h - hz) * t))
        # 统计暗格(:217 rgba(13,5,24,.72) + 青边 .4)锚 grid 六格 ——
        # 落日亮带上的文字必须有自己的暗底(contrast_bg 口径);横向外扩
        # 1px 同液态药丸(列距 8,防压线)
        for lb in _card_grid_vs(win):
            g = _pill_rect(lb, 1, 18, 3)
            p.setPen(QPen(QColor(0, 229, 255, 102)))
            p.setBrush(QColor(13, 5, 24, 184))
            p.drawRect(g)
        # 主数字行底衬(2026-10-04 P1):HTML :208 .vp-sp 故意叠日
        # (margin-top:-36px)+双色 text-shadow 保读,Qt 侧数字左置但宽值
        # (≥7 字符,30pt mono ≈18px/字)尾部会伸进太阳左叶(x≈112 起),
        # 同病同治(衬底最坏合成 #51213d,青字 8.3:1)。锚 tps_lbl+
        # tps_unit_lbl 活几何:值变宽 → label 变宽 → 底衬每 paint 现读
        # 跟随(不缓存,布局/显隐自愈);sparkline 不衬(线件非字段文本,
        # 与太阳重叠仅边缘 7%)。
        _nbs = [getattr(win, n, None) for n in ("tps_lbl", "tps_unit_lbl")]
        _nbs = [lb for lb in _nbs if lb is not None and not lb.isHidden()]
        if _nbs:
            _vp_plate(p, _union([lb.geometry() for lb in _nbs])
                      .adjusted(-6, -4, 6, 4))
    elif form == "h":
        # :223-225 底部 8px 迷你网格
        for i in range(1, int(w / 18) + 1):
            p.setPen(QPen(QColor(0, 229, 255, 110), 1.0))
            p.drawLine(i * 18, win.height() - 7, i * 18, win.height())
        p.setPen(QPen(QColor(0, 229, 255, 90), 1.0))
        p.drawLine(0, win.height() - 7, w, win.height() - 7)
        for lb in _hbar_group_labels(win):               # :227 段暗格
            g = lb.geometry().adjusted(-3, -3, 3, 3)   # 段距 10,±3 不压邻段
            p.setPen(QPen(QColor(0, 229, 255, 102)))
            p.setBrush(QColor(13, 5, 24, 184))
            p.drawRect(g)
    else:
        # :233-235 小太阳(5 on/2 off)+ 组暗格(:238)
        _striped_sun(p, w / 2.0, 42.0, 23.0,
                     ((0.0, QColor("#ffe25a")), (0.60, QColor("#ff6b9d")),
                      (1.0, QColor("#ff5a5a"))), 5, 2)
        for lb in _vbar_group_labels(win):
            g = lb.geometry().adjusted(-9, -3, 9, 4)
            p.setPen(QPen(QColor(0, 229, 255, 102)))
            p.setBrush(QColor(13, 5, 24, 184))
            p.drawRect(g)
        # 主数字底衬(2026-10-04 P1,结构性主病灶):小太阳 (w/2,42,r23)
        # 与居中 24pt 主数字重叠是本形态布局的必然(实测 67% 字形面积压
        # 条纹、裸对比 1.04~1.92:1,核心读数 2/3 面积不可读;HTML :237
        # text-shadow 2px 2px 0 #ff2e97 的补偿移植时被丢,见 _vp_plate)。
        # 与四组值行暗格同款同 padding(-9,-3,9,4),视觉成套。
        _lb = getattr(win, "tps_lbl", None)
        if _lb is not None and not _lb.isHidden():
            _vp_plate(p, _lb.geometry().adjusted(-9, -3, 9, 4))
    p.restore()
    if form is not None:                                 # :221/:231 青边
        _stroke(p, win, r, QColor(0, 229, 255, 128))
    # 卡(:202)无 border,仅投影,不描


# ══════ ⑧ 工程蓝图(HTML :242-277):深蓝底 + 19-20px 网格 + 图签 ══════

def _deco_blueprint(p, win, form) -> None:
    r = {"card": 4, "h": 3, "v": 4}[form or "card"]     # :246/:262/:271
    w, h = win.width(), win.height()
    path = _base_path(win, r)
    grad = QLinearGradient(w * 0.2, 0.0, w * 0.8, float(h))  # :247 160deg
    grad.setColorAt(0.0, QColor("#0d3b66"))
    grad.setColorAt(1.0, QColor("#082c4e"))
    p.fillPath(path, grad)
    p.save()
    p.setClipPath(path)
    # 双向制图网格(:244-245 19px 透明+1px 白.09,pitch 20)
    gl = QColor(255, 255, 255, 23)
    x = 10
    while x < w:
        p.fillRect(x, 0, 1, h, gl)
        x += 20
    y = 10
    while y < h:
        p.fillRect(0, y, w, 1, gl)
        y += 20
    if form is None:
        # 图名(:544『THROUGHPUT — 瞬时吞吐视图』)+ 定线下划线:顶部边距带
        # 左列,状态行 y≈14 起,零重叠(bp-arrow 尺寸箭头让位给冻结结构里的
        # sparkline —— 同行既占,绘制即叠印,列为 HTML 保真偏差记录)
        t = "THROUGHPUT — 瞬时吞吐视图"
        _set_font(p, 9, families=["Consolas"], spacing=2.0)
        tw = _text_w(p, t)
        _text(p, t, 16, 10, 9, QColor(220, 236, 255, 230),
              families=["Consolas"], spacing=2.0)
        p.fillRect(16, 12, tw, 1, QColor(220, 236, 255, 102))
        # 图签章(:543『FIG. 08-B』右上 rotate 4°):右上边距带,model 名
        # y≈14 起,章体 y≤13 零重叠
        st = "FIG. 08-B"
        _set_font(p, 8, families=["Consolas"], spacing=2.0)
        tw = _text_w(p, st)
        p.save()
        p.translate(float(w - 14), 4.0)
        p.rotate(4.0)
        p.setPen(QPen(QColor(220, 236, 255, 128)))
        p.setBrush(QColor(0, 0, 0, 0))
        p.drawRect(int(-tw - 12), 0, int(tw + 12), 11)
        _text(p, st, -tw - 6, 8, 8, QColor(220, 236, 255, 178),
              families=["Consolas"], spacing=2.0)
        p.restore()
    p.restore()
    _stroke(p, win, r, QColor(255, 255, 255, 64))       # :247 border 白.25


# ══════ glass(缺省第 0 款)══════
# palette 值逐字节镜像 app.py C_* 常量(:74-91):fg=C_FG、dim=C_DIM、
# accent=C_ACCENT、warn=C_WARN、sep=C_BORDER;faint 无常量,取 QSS 字面
# #68696c(app.py:99)。改动任一侧必须同步另一侧 —— stress 逐位断言兜底。
# 圆角(#34 后无 SkinDef.radius 影子字段):glass 无 deco,卡/竖 12、横 8
# 在 app.py _paint_glass(paintEvent 既有路径)内逐位分档,不走本类。
_SKIN_GLASS = SkinDef(
    id="glass",
    menu_label="玻璃仪表(默认)",
    palette={"fg": "#e8e8e8", "dim": "#979799", "accent": "#7ad7ff",
             "warn": "#ffd166", "faint": "#68696c",
             "soft": "rgba(255,255,255,204)", "half": "rgba(255,255,255,128)",
             "vstrong": "rgba(255,255,255,217)", "sep": "#2c2f3a"},
    spark_line=_c(96, 205, 255),            # C_ACCENT_DIM #60cdff
    spark_dot=_c(122, 215, 255),            # C_ACCENT #7ad7ff
    pulse_idle=_c(255, 255, 255, 89),       # 白.35(PulseIndicator 既有)
    pulse_active=_c(96, 205, 255),          # C_ACCENT_DIM
    pulse_core=_c(96, 205, 255, 230),       # 内核 opacity .9
    ring_base=_c(255, 255, 255, 26),        # 白.10(paint_ring 既有)
    sep="#2c2f3a",                          # C_BORDER
    sep_card="rgba(255,255,255,0.07)",      # _mk_card_sep 既有缺省(逐位)
    sep_card_weak="rgba(255,255,255,0.05)", # 模型行弱线既有字面(逐位)
    contrast_bg={"card": "#1c1e28", "h": "#1c1e28", "v": "#1c1e28"},
    today_cost_sep=" ",                     # 恒单空格(既有文案逐位不变)
    font_mono_families=("Cascadia Code", "Consolas"),   # mk_mono 既有
)

# ① 瑞士国际主义(浅色,HTML :26-51):奶白 #f4f2ec + 墨字 #141414 + 红点
_SKIN_SWISS = SkinDef(
    id="swiss",
    menu_label="瑞士国际主义",
    palette={"fg": "#141414", "dim": "#555555", "accent": "#e2503c",
             "warn": "#c0392b",
             # faint 原定稿 #999999 对奶白底仅 2.54:1,未过 T5 WCAG 闸微标
             # 签档 ≥3.0 —— 加深为 #8a8a8a(3.08:1,验收票 2026-09-28 落地)
             "faint": "#8a8a8a",
             "soft": "rgba(20,20,20,204)", "half": "rgba(20,20,20,153)",
             "vstrong": "rgba(20,20,20,217)", "sep": "rgba(20,20,20,51)"},
    spark_line=_c(226, 80, 60), spark_dot=_c(20, 20, 20),
    pulse_idle=_c(20, 20, 20, 89), pulse_active=_c(226, 80, 60),
    pulse_core=_c(226, 80, 60, 230), ring_base=_c(20, 20, 20, 26),
    sep="rgba(20,20,20,51)",
    sep_card="rgba(20,20,20,31)", sep_card_weak="rgba(20,20,20,20)",
    contrast_bg={"card": "#f4f2ec", "h": "#f4f2ec", "v": "#f4f2ec"},
    today_cost_sep=" ",
    font_mono_families=("Cascadia Code", "Consolas"),   # .num 同玻璃
    deco=_deco_swiss,
)

# ② 琥珀 CRT 终端(HTML :53-78):#100800 底 + 琥珀 #ffb000 + 暗棕边框
_SKIN_CRT = SkinDef(
    id="crt",
    menu_label="琥珀 CRT 终端",
    palette={"fg": "#ffb000", "dim": "#ab7500", "accent": "#ffb000",
             "warn": "#ff8c00",
             # faint 原定稿 #7b5400 对 #100800 仅 2.94:1(<3.0)、half α153 仅
             # 4.41:1(<4.5),均未过 T5 WCAG 闸 —— faint 加深 #8a5e00(3.48:1)、
             # half α→170(5.28:1,验收票 2026-09-28 落地)
             "faint": "#8a5e00",
             "soft": "rgba(255,176,0,204)", "half": "rgba(255,176,0,170)",
             "vstrong": "rgba(255,176,0,217)", "sep": "rgba(255,176,0,77)"},
    spark_line=_c(255, 176, 0), spark_dot=_c(255, 140, 0),
    pulse_idle=_c(255, 176, 0, 89), pulse_active=_c(255, 176, 0),
    pulse_core=_c(255, 176, 0, 230), ring_base=_c(255, 176, 0, 26),
    sep="rgba(255,176,0,77)",
    sep_card="rgba(255,176,0,38)", sep_card_weak="rgba(255,176,0,26)",
    contrast_bg={"card": "#100800", "h": "#100800", "v": "#100800"},
    today_cost_sep=" ",
    font_mono_families=("Cascadia Code", "Consolas"),
    deco=_deco_crt,
)

# ③ 黑板粉笔(HTML :80-103):墨绿板面 + 木框 #7a5a38 + 粉笔白/鹅黄。
# 字族偏差见模块头【黑板字族偏差】:Segoe Print 实测破横条 ≤34 闸
_SKIN_CHALK = SkinDef(
    id="chalk",
    menu_label="黑板粉笔",
    palette={"fg": "#e8e6df", "dim": "#c4c2ba", "accent": "#ffffff",
             "warn": "#ffe9a8", "faint": "#a5a39a",
             "soft": "rgba(232,230,223,204)", "half": "rgba(232,230,223,153)",
             "vstrong": "rgba(232,230,223,217)", "sep": "rgba(232,230,223,77)"},
    spark_line=_c(255, 255, 255), spark_dot=_c(255, 233, 168),
    pulse_idle=_c(232, 230, 223, 89), pulse_active=_c(255, 255, 255),
    pulse_core=_c(255, 255, 255, 230), ring_base=_c(232, 230, 223, 38),
    sep="rgba(232,230,223,77)",
    sep_card="rgba(232,230,223,46)", sep_card_weak="rgba(232,230,223,31)",
    contrast_bg={"card": "#26332c", "h": "#2e3b34", "v": "#2e3b34"},
    today_cost_sep=" ",
    font_mono_families=("Cascadia Code", "Consolas"),   # Segoe Print 见模块头
    deco=_deco_chalk,
)

# ④ 液态玻璃(HTML :105-139):暗玻璃幕 + 双 blob(#38bdf8/#a78bfa
# QRadialGradient 模拟 blur)+ 白玻璃面板 + 文本行暗玻璃底衬(_liquid_
# plates,2026-10-06 #70 起落地 —— 见该函数 docstring 与 contrast_bg 注释)
_SKIN_LIQUID = SkinDef(
    id="liquid",
    menu_label="液态玻璃药丸",
    palette={"fg": "#ffffff", "dim": "#ccd6e2", "accent": "#7dd8fc",
             "warn": "#c9b8fa", "faint": "#a9b6c6",
             "soft": "rgba(255,255,255,204)", "half": "rgba(255,255,255,153)",
             # sep(#79,2026-10-06 起与 SkinDef.sep 焊接同值):此前本键
             # rgba(255,255,255,77) 与字段 transparent 分叉 —— 但 QFrame#sep
             # 的 QSS selector 全仓零匹配控件(真实分隔线全走 SkinDef.sep
             # 字段内联注入,_mk_sep/_mk_card_sep 零 QSS 依赖),改本键是
             # 无声 no-op;分叉留着只会让后人改错侧。stress 的恒等断言
             # (palette["sep"]==sk.sep)钉死不再漂。
             "vstrong": "rgba(255,255,255,217)", "sep": "transparent"},
    spark_line=_c(125, 216, 252), spark_dot=_c(255, 255, 255),
    pulse_idle=_c(255, 255, 255, 89), pulse_active=_c(56, 189, 248),
    pulse_core=_c(56, 189, 248, 230), ring_base=_c(255, 255, 255, 26),
    # 分隔线全形态 transparent(用户 2026-09-29『横线、竖线和药丸全部
    # 都不要,要干净纯粹』):玻璃本身即分组语言,条内竖线/横线一并去除
    sep="transparent",
    sep_card="transparent", sep_card_weak="transparent",
    # contrast_bg=#70(2026-10-06)按离屏探针(行中心带口径,真实字形
    # span 采样)实测最坏承载面如实申报,取代旧三重失真值 #343b4d(其
    # 漏算 blob 峰值 —— 按该申报底算 dim 7.6 放行,真实 blob 峰上 1.75:1):
    # - card=#24233a:模型行暗玻璃底衬压 blob2(purple,中心 w-50,h-44)
    #   峰后的最坏合成 —— half 档实测 6.40:1(全场字段档最坏);
    # - h=#373e4b:未衬的 plan/cd/today 组行真实玻璃面(速度组/燃速组
    #   已衬更暗,最坏仍在未衬面)—— dim 档实测 7.31:1;
    # - v=#3d4351:未衬的四组 k/v 行玻璃面(速度组已衬)—— vstrong 档
    #   实测 7.71:1。
    # 三值均经 t3_contrast_probe.py 复测(大字/微标签档真实最坏 6.85/
    # 7.57 亦全过),T5 闸按本值复测须全过 —— 申报变严而非维持旧数自证
    # (vaporwave 先例)。
    contrast_bg={"card": "#24233a", "h": "#373e4b", "v": "#3d4351"},
    today_cost_sep=" ",
    font_mono_families=("Cascadia Code", "Consolas"),
    deco=_deco_liquid,
)

# ⑤ 工业机柜(HTML :141-173):#3a3f45 渐变面板 + readout 暗屏 #101614
# + 荧光绿 #7cf7b0;contrast_bg=#101614(#89 复核后维持:卡形态铭牌
# 2026-10-06 起向下延伸覆盖模型行,暗材铭牌 #151b18→#0e1411 全程在其
# 区间内、离屏实测延伸后全场最坏 half≈4.9≥4.5;readout 暗屏/条形态
# 铭牌本就是 #101614 系实色 —— 三形态申报值与真实承载面一致)
_SKIN_INDUSTRIAL = SkinDef(
    id="industrial",
    menu_label="工业机柜",
    palette={"fg": "#e8eef4", "dim": "#b9c2cb", "accent": "#7cf7b0",
             "warn": "#f0a832", "faint": "#9aa2ab",
             # half 原定稿 α153 对 readout #101614 仅 4.37:1(<4.5),α→170
             # (5.14:1,过 T5 WCAG 字段文本档,验收票 2026-09-28 落地)
             "soft": "rgba(232,238,244,204)", "half": "rgba(185,194,203,170)",
             "vstrong": "rgba(232,238,244,217)", "sep": "rgba(255,255,255,51)"},
    spark_line=_c(124, 247, 176), spark_dot=_c(74, 222, 128),
    pulse_idle=_c(154, 162, 171, 89), pulse_active=_c(124, 247, 176),
    pulse_core=_c(74, 222, 128, 230), ring_base=_c(255, 255, 255, 26),
    sep="rgba(255,255,255,51)",
    sep_card="rgba(255,255,255,20)", sep_card_weak="rgba(255,255,255,13)",
    contrast_bg={"card": "#101614", "h": "#101614", "v": "#101614"},
    today_cost_sep=" ",
    font_mono_families=("Cascadia Code", "Consolas"),
    deco=_deco_industrial,
)

# ⑥ 报纸头版(浅色,HTML :175-199):奶白 #f7f3ea + 报头黑 + 棕红 #aa3333;
# 方角(radius 0)与 Georgia 衬线为定稿特征(圆角/装饰字族均在 deco 内
# 字面,见 SkinDef docstring #34/#71 —— 影子字段已删,真正生效处:
# _deco_newspaper 报头 families=['Georgia'])
_SKIN_NEWSPAPER = SkinDef(
    id="newspaper",
    menu_label="报纸头版",
    palette={"fg": "#1a1a1a", "dim": "#555555", "accent": "#aa3333",
             "warn": "#7a1f1f", "faint": "#888888",
             # half 原定稿 α153 对奶白 #f7f3ea 仅 4.41:1(<4.5),α→170
             # (5.35:1,过 T5 WCAG 字段文本档,验收票 2026-09-28 落地)
             "soft": "rgba(26,26,26,204)", "half": "rgba(26,26,26,170)",
             "vstrong": "rgba(26,26,26,217)", "sep": "rgba(0,0,0,51)"},
    spark_line=_c(26, 26, 26), spark_dot=_c(170, 51, 51),
    pulse_idle=_c(26, 26, 26, 89), pulse_active=_c(170, 51, 51),
    pulse_core=_c(170, 51, 51, 230), ring_base=_c(0, 0, 0, 26),
    sep="rgba(0,0,0,51)",
    sep_card="rgba(0,0,0,64)", sep_card_weak="rgba(0,0,0,41)",
    contrast_bg={"card": "#f7f3ea", "h": "#f7f3ea", "v": "#f7f3ea"},
    today_cost_sep="  ",                    # 双空格档=定稿微调(HTML :484)
    font_mono_families=("Georgia", "Times New Roman"),
    deco=_deco_newspaper,
)

# ⑦ 蒸汽波落日(HTML :201-240):紫红落日渐变 + 青 #00e5ff 网格 + 品红
# 描边;contrast_bg=统计暗格 rgba(13,5,24,.72) 与落日亮带的最坏合成
# (评审 D 口径:落日亮带/网格为无字装饰带,不得充当基准 —— 暗格才是
# 字段文本的真实局部底)。v 形态 2026-10-04 P1 主数字底衬上线后,该形态
# 最亮承载面升级为『主数字暗格压太阳最亮带』(y≈33 处黄粉插值 (255,166,
# 124) 合成 → #503234),据此如实申报 —— 高于旧值 #2c1038(组暗格在
# 太阳下方的暗天空上),申报随之变严而非维持旧数自证。
_SKIN_VAPORWAVE = SkinDef(
    id="vaporwave",
    menu_label="蒸汽波落日",
    palette={"fg": "#ffffff", "dim": "#d9c9f2", "accent": "#00e5ff",
             "warn": "#ff2e97", "faint": "#a08cc8",
             "soft": "rgba(255,255,255,204)", "half": "rgba(255,255,255,153)",
             "vstrong": "rgba(255,255,255,217)",
             "sep": "rgba(0,229,255,102)"},
    spark_line=_c(0, 229, 255), spark_dot=_c(255, 46, 151),
    pulse_idle=_c(255, 255, 255, 89), pulse_active=_c(0, 229, 255),
    pulse_core=_c(0, 229, 255, 230), ring_base=_c(0, 229, 255, 38),
    sep="rgba(0,229,255,102)",
    sep_card="rgba(0,229,255,102)", sep_card_weak="rgba(0,229,255,72)",
    contrast_bg={"card": "#51213d", "h": "#51213d", "v": "#503234"},
    today_cost_sep=" ",
    font_mono_families=("Cascadia Code", "Consolas"),
    deco=_deco_vaporwave,
)

# ⑧ 工程蓝图(HTML :242-277):#0d3b66→#082c4e 深蓝底 + 19-20px 双向网格
# (QPainter 直绘)+ 制图白 #dcecff(圆角与图签字族均在 deco 内字面,
# 见 SkinDef docstring #34/#71)
_SKIN_BLUEPRINT = SkinDef(
    id="blueprint",
    menu_label="工程蓝图",
    palette={"fg": "#dcecff", "dim": "#a3bdd8", "accent": "#9fd8ff",
             "warn": "#f0c040", "faint": "#7f9cbc",
             # half 原定稿 α153 对 #0d3b66 仅 4.48:1(<4.5),α→170
             # (5.16:1,过 T5 WCAG 字段文本档,验收票 2026-09-28 落地)
             "soft": "rgba(220,236,255,204)", "half": "rgba(220,236,255,170)",
             "vstrong": "rgba(220,236,255,217)",
             "sep": "rgba(220,236,255,102)"},
    spark_line=_c(159, 216, 255), spark_dot=_c(220, 236, 255),
    pulse_idle=_c(220, 236, 255, 89), pulse_active=_c(159, 216, 255),
    pulse_core=_c(159, 216, 255, 230), ring_base=_c(220, 236, 255, 38),
    sep="rgba(220,236,255,102)",
    sep_card="rgba(220,236,255,102)", sep_card_weak="rgba(220,236,255,72)",
    contrast_bg={"card": "#0d3b66", "h": "#0d3b66", "v": "#0d3b66"},
    today_cost_sep=" ",
    font_mono_families=("Cascadia Code", "Consolas"),
    deco=_deco_blueprint,
)


# 注册表:键序即 SKIN_IDS(T3 菜单按此序出九项)。缺项回退 glass 的
# get-or-fallback 在 app.MeterWindow._skin(),不在本模块 —— 保持纯数据。
REGISTRY = {sk.id: sk for sk in (
    _SKIN_GLASS, _SKIN_SWISS, _SKIN_CRT, _SKIN_CHALK, _SKIN_LIQUID,
    _SKIN_INDUSTRIAL, _SKIN_NEWSPAPER, _SKIN_VAPORWAVE, _SKIN_BLUEPRINT,
)}
