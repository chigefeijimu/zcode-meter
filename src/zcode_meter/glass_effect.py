"""Liquid Glass 背景管线 v3.1:时域干净缓冲(Temporal Clean Buffer)。

核心洞察(2026-09-29,替代环推断方案):窗口拖到新位置【之前】,那块
屏幕是裸露的 —— 上一拍的抓帧里就有它的真实内容。维护全屏干净背景图
_CLEAN:每拍抓『当前∪上一拍窗口矩形+边距』的区域帧,把【窗口未覆盖】
的部分写入 _CLEAN(_KNOWN 标记);窗口正下方的像素保留【窗口到达前】
的历史真值。玻璃 = 模糊(_CLEAN[窗口矩形]) —— 真内容/真色彩/真形状,
拖到哪都是 1~2 拍前的实况,肉眼即实时。

- 精确窗洞:抓拍瞬间 GetWindowRect;跳写边界 = raw ∪ 上一拍 raw + 8px
  (只作用于跳写,_PREV_HOLE 恒存 raw 防膨胀);
- 启动种子 seed_full():窗口 show 之前抓一帧全屏;
- 首次到达未见过的区域:_KNOWN=False 像素用边缘双线性兜底;
- 拖动中 33ms/拍(30fps),稳态 80ms;静止时 DXcam 返回 None 零处理。
"""

from __future__ import annotations

import time
import threading

import numpy as np

try:
    import dxcam
    import cv2
    _cam_test = dxcam.create(output_color="BGR")
    OK = _cam_test is not None
except Exception:
    OK = False

_CAM = None
_CAM_LOCK = threading.Lock()

_CLEAN = None         # 全屏 BGR 时域干净背景(窗口到达前的真值)
_KNOWN = None         # 全屏 bool:像素有干净样本
_FW, _FH = 0, 0       # 屏幕物理尺寸
_PAD = 60             # 抓取外边距(覆盖拖动轨迹)
_REGION = [0, 0, 0, 0, 1.25]
_REGION_AT = 0.0
_PREV_RECT = None
_PREV_HOLE = None     # 上一拍原始窗洞(恒存 raw,防累积膨胀)
_LAST_BG = None
_LAST_COMPOSED = None
_THREAD = None
_THREAD_STOP = False
_HWND = 0             # 主线程注入的窗口句柄(抓拍瞬间取真实窗洞)
_BUF_LOCK = threading.Lock()   # 缓冲读写互斥(线程写切片 vs paint 裁拷)
_VERSION = 0          # 缓冲版本(paint 视图缓存失效判据)
_VIEW = {"key": None, "np": None}   # paint 侧视图缓存
# 预模糊区域缓存(2026-09-29 卡顿终修):后台线程对窗口外扩 _BPAD 的
# 区域做一次模糊存 RGB,paint 只裁剪(0.3ms memcpy)—— 模糊在 cv2 里
# 释放 GIL,不再占 UI 线程;窗口在缓存内移动零重算
_BPAD = 96
_BLUR = None          # np RGB,预模糊区域图
_BLUR_RECT = None     # (x0,y0,x1,y1) 该图对应的物理屏幕矩形
_BLUR_VER = -1
# 静止盲区刷新(2026-09-29 用户『停下后别的窗口移到卡片下,背景不变』):
# 卡片正下方被自身遮挡,常规抓取永远看不到。触发条件 = 卡片静止 + 环带
# 内容变化(签名不同;有东西路过/壁纸换);动作 = refresh_hole_below()
# 直接 PrintWindow 卡片下方窗口合成(零闪,2026-09-30;旧 opacity 隐身
# 抓拍方案已退役 —— 无论怎么缩短隐身期都有可见闪烁,用户要求彻底无闪)
_RING_SIG = None      # 最近一拍环带内容签名
_BLINK_SIG = None     # 上次盲区刷新时的环带签名
_BLINK_AT = 0.0       # 上次盲区刷新(含失败退避)的 perf_counter 时刻
_BLINK_MUTE = 0.0     # 静默截止时刻(动画源 CPU 退避)
_SIG_CHANGES = 0      # 自上次刷新/稳定以来环带签名实际变化次数

def needs_blink() -> bool:
    """静止(0.5s 无 region 变更)且环带内容自上次盲区刷新后变过。

    节流(2026-09-30 零闪改版后纯属 CPU 经济):PrintWindow 直抓不再有
    视觉代价,但一次要抓数十个相交窗口(数十到数百 ms 后台开销),保留
    冷却 2.5s + 动画源静默 15s(视频垫背时玻璃 15s 更新一帧,可接受)。
    动画判据用【变化次数】而非『变了没有』:冷却期内签名变 ≥10 次 =
    连续动画源 → 静默;窗口划过是阶跃(3-6 次)→ 正常刷新。"""
    global _SIG_CHANGES, _BLINK_MUTE
    now = time.perf_counter()
    if now < _BLINK_MUTE:
        return False
    if now - _BLINK_AT < 2.5:
        return False
    if _RING_SIG is None or _RING_SIG == _BLINK_SIG:
        _SIG_CHANGES = 0            # 稳定:历史计数作废
        return False
    if time.perf_counter() - _REGION_AT <= 0.5:
        return False
    if _SIG_CHANGES >= 10:
        _BLINK_MUTE = now + 15.0    # 连续动画:静默 15s
        _SIG_CHANGES = 0
        return False
    return True


def blink_capture() -> bool:
    """【已退役 2026-09-30,零闪改版】原方案:opacity 0 隐身 ~40ms 抓洞区。
    用户要求彻底无闪 → 改为 capture_below() 直接 PrintWindow 卡片下方的
    窗口合成洞区真背景(卡片全程不动);本函数保留仅作历史参照,无调用方。"""
    return False


# ---- Win32 直抓卡片下方窗口(零闪盲区刷新,2026-09-30) ----
# 背景:屏幕合成帧里卡片区域=卡片自己画的不透明缓冲背景图(α≈1),
# 反合成不可行;隐身抓拍必有可见窗口期。PrintWindow(PW_RENDERFULLCONTENT)
# 逐个抓 z 序在卡片之下的相交窗口,按 z 序合成洞区 —— 实测保真度精确
# (黄窗垫背:合成中段 BGR=(0,221,255) 与 #ffdd00 逐值一致),且卡片
# 全程纹丝不动,零闪烁。走 GDI 与 DXcam 相机无共享状态,可在抓取线程
# 内与 _capture_once 并行无争用。
_W32 = None            # (user32, dwapi, gdi32, wintypes) 惰性句柄
_PW_CAP = 24           # 单次刷新最多抓的窗口数(性能护栏)


def _win32():
    global _W32
    if _W32 is not None:
        return _W32
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    dwapi = ctypes.windll.dwmapi
    gdi32 = ctypes.windll.gdi32
    dwapi.DwmGetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                            ctypes.c_void_p, ctypes.c_uint]
    user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_uint]
    _W32 = (user32, dwapi, gdi32, wintypes)
    return _W32


def _print_window_bgra(hwnd, w, h):
    """PrintWindow(RENDERFULLCONTENT) 到 32bpp DIB,返回 BGRA 或 None。
    注意:CreateDIBSection 第 4 参才是位图数据指针,HBITMAP 句柄不是
    (实验期拿句柄当地址读 → 段错误,2026-09-30)。"""
    import ctypes
    user32, dwapi, gdi32, wintypes = _win32()
    bmi = ctypes.create_string_buffer(48)
    import struct
    ctypes.memmove(bmi, struct.pack("lllhhllllll", 40, w, -h, 1, 32,
                                    0, 0, 0, 0, 0, 0), 40)
    hdc = user32.GetDC(0)
    memdc = gdi32.CreateCompatibleDC(hdc)
    ppv = ctypes.c_void_p()
    dib = gdi32.CreateDIBSection(hdc, bmi, 0, ctypes.byref(ppv), None, 0)
    if not dib or not ppv.value:
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(0, hdc)
        return None
    old = gdi32.SelectObject(memdc, dib)
    ok = user32.PrintWindow(hwnd, memdc, 2)     # PW_RENDERFULLCONTENT
    gdi32.SelectObject(memdc, old)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(0, hdc)
    if not ok:
        gdi32.DeleteObject(dib)
        return None
    arr = np.ctypeslib.as_array(
        (ctypes.c_ubyte * (w * h * 4)).from_address(ppv.value)
    ).reshape(h, w, 4).copy()
    gdi32.DeleteObject(dib)
    return arr


def capture_below(hwnd, rect):
    """合成 rect 处『卡片正下方』的真背景:枚举 z 序在 hwnd 之下且与
    rect 相交的可见窗口,自底向顶 PrintWindow 合成。
    返回 (bgr, covered_mask) 或 (None, None)。covered=False 的像素
    (壁纸/无窗口区)保留调用方旧值,不用黑色覆写。"""
    import ctypes
    user32, dwapi, gdi32, wintypes = _win32()
    x0, y0, x1, y1 = rect
    W, H = x1 - x0, y1 - y0
    if W <= 0 or H <= 0:
        return None, None

    wins = []
    found = ctypes.c_bool(False)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, lp):
        if not found.value:
            if h == hwnd:
                found.value = True
            return True
        if len(wins) >= _PW_CAP:
            return False
        if not user32.IsWindowVisible(h):
            return True
        cloaked = ctypes.c_uint(0)
        if dwapi.DwmGetWindowAttribute(h, 14, ctypes.byref(cloaked), 4) == 0 \
                and cloaked.value:
            return True                      # cloaked 幽灵窗(UWP 挂起)
        pid = ctypes.c_uint(0)
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value == ctypes.windll.kernel32.GetCurrentProcessId():
            return True                      # 自家 tooltip 等顶层
        r = wintypes.RECT()
        if dwapi.DwmGetWindowAttribute(h, 9, ctypes.byref(r),
                                       ctypes.sizeof(r)) != 0:
            if not user32.GetWindowRect(h, ctypes.byref(r)):
                return True
        if r.left < x1 and r.right > x0 and r.top < y1 and r.bottom > y0:
            wins.append((h, (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumWindows(_cb, 0)
    if not found.value or not wins:
        return None, None

    canvas = np.zeros((H, W, 3), np.uint8)
    covered = np.zeros((H, W), bool)
    for h, (wx0, wy0, wx1, wy1) in reversed(wins):     # 底 → 顶
        bgra = _print_window_bgra(h, wx1 - wx0, wy1 - wy0)
        if bgra is None:
            continue
        sx0 = max(wx0, x0) - wx0
        sy0 = max(wy0, y0) - wy0
        dx0 = max(wx0, x0) - x0
        dy0 = max(wy0, y0) - y0
        dx1 = min(wx1, x1) - x0
        dy1 = min(wy1, y1) - y0
        if dx1 <= dx0 or dy1 <= dy0:
            continue
        sub = bgra[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)]
        a = sub[:, :, 3].astype(np.float32) / 255.0
        am = a > 0.02
        dst = canvas[dy0:dy1, dx0:dx1]
        dst[:] = (sub[:, :, :3] * a[:, :, None]
                  + dst * (1.0 - a)[:, :, None]).astype(np.uint8)
        covered[dy0:dy1, dx0:dx1] |= am
    if not covered.any():
        return None, None
    return canvas, covered


def refresh_hole_below() -> bool:
    """零闪盲区刷新(needs_blink 触发,线程内执行):PrintWindow 直抓洞区
    写 _CLEAN。失败也推进 _BLINK_AT(2.5s 重试节拍),防止失败拍每
    100ms 热循环。"""
    global _VERSION, _BLINK_SIG, _BLINK_AT, _SIG_CHANGES
    if _CLEAN is None or not _HWND:
        return False
    raw = _window_rect_now()
    if raw is None:
        _BLINK_AT = time.perf_counter()
        return False
    bgr, covered = capture_below(_HWND, raw)
    now = time.perf_counter()
    if bgr is None:
        _BLINK_AT = now                     # 失败退避到下个冷却期
        return False
    x0, y0, x1, y1 = raw
    with _BUF_LOCK:
        sub = _CLEAN[y0:y1, x0:x1]
        sub[covered] = bgr[covered]         # 未覆盖区保留旧真值
        _KNOWN[y0:y1, x0:x1] = True
        _VERSION += 1
    _BLINK_SIG = _RING_SIG
    _BLINK_AT = now
    _SIG_CHANGES = 0
    return True


def _cam():
    global _CAM
    with _CAM_LOCK:
        if _CAM is None and OK:
            _CAM = dxcam.create(output_color="BGR")
    return _CAM


def seed_full():
    """启动种子:窗口显示前抓一帧全屏 → 整屏干净(失败静默)。"""
    global _CLEAN, _KNOWN, _FH, _FW
    cam = _cam()
    if cam is None:
        return False
    try:
        f = cam.grab()
        if f is None:
            time.sleep(0.08)
            f = cam.grab()
    except Exception:
        return False
    if f is None:
        return False
    global _VERSION
    _FH, _FW = f.shape[:2]
    # 缓冲交换持锁:本函数不再只在线程启动前调用 —— _resync_output 会在
    # 运行期(后台线程/UI 线程)重播种,paint 侧 view() 持同一把锁切片,
    # 不锁会读到新 _CLEAN 配旧 _FW/_FH 的撕裂组合(2026-09-30 P1)。
    with _BUF_LOCK:
        _CLEAN = f.copy()
        _KNOWN = np.ones((_FH, _FW), dtype=bool)
        _VERSION += 1
    return True


def _resync_output() -> bool:
    """输出(分辨率/显示器拓扑)变化后的自愈:整屏抓一帧,与 _FW/_FH
    失配则重播种干净基准并清旧坐标系缓存。

    _FW/_FH 原本只在 seed_full 赋值一次,输出变化后 dxcam 会在自身
    access-loss 恢复里更新 width/height,而本模块的夹取仍按旧屏计算:
    分辨率缩小 → 抓取区域越界,cam.grab(region=...) 抛 ValueError 被
    _capture_once 吞掉,管线永久冻结;分辨率增大 → 矩形被夹回旧尺寸,
    新屏区域坐标错位(2026-09-30 P1)。整屏 grab 不走区域校验、且内部
    会触发相机自身的输出恢复,是取真实尺寸对账的唯一可靠途径。"""
    global _PREV_RECT, _PREV_HOLE, _BLUR, _BLUR_RECT
    cam = _cam()
    if cam is None:
        return False
    try:
        f = cam.grab()
    except Exception:
        return False
    if f is None:
        return False
    fh, fw = f.shape[:2]
    if fw == _FW and fh == _FH:
        return False                  # 尺寸一致:并非输出变化,无需重播种
    if not seed_full():
        return False
    # 旧屏坐标系的缓存必须一并作废:_PREV_HOLE 的『恒存 raw 防膨胀』设计
    # 前提是坐标系不变,残留会让下一拍跳写边界继续 union 回越界区域;
    # 预模糊区域/视图缓存同理(版本已 bump,这里显式清防快路径旧帧)。
    _PREV_RECT = _PREV_HOLE = None
    _BLUR = _BLUR_RECT = None
    _VIEW["key"] = None
    return True


def set_hwnd(hwnd: int):
    global _HWND
    _HWND = int(hwnd)


def _window_rect_now():
    """抓拍/合成瞬间的窗口物理矩形(hwnd 优先;回退 _REGION)。

    返回 None = 窗口与相机覆盖区无有效交集(2026-09-30 P1):相机只复制
    主输出 [0,_FW]×[0,_FH],多显示器下卡片拖到副屏后旧实现把矩形硬夹进
    主屏 —— 副屏坐标被重映射到主屏左上/夹成边缘细条,玻璃静默显示主屏
    内容的裁剪或过期缓存,无提示无回退。None 让各调用方显式失败:
    view()→None 交 _deco_liquid 落自绘 veil,抓取/blink 放弃本拍。
    跨屏边缘(部分交集)仍按旧语义夹取交集 —— 与拖动中的视觉连续性
    兼容,只有整卡离屏才判无解。"""
    if _HWND:
        import ctypes
        import ctypes.wintypes as wt
        r = wt.RECT()
        if ctypes.windll.user32.GetWindowRect(_HWND, ctypes.byref(r)):
            x0, y0 = max(0, r.left), max(0, r.top)
            x1, y1 = min(_FW, r.right), min(_FH, r.bottom)
            if x1 - x0 < 2 or y1 - y0 < 2:
                return None
            return (x0, y0, x1, y1)
    x, y, w, h, scale = _REGION
    rx0, ry0 = round(x * scale), round(y * scale)
    rx1, ry1 = rx0 + round(w * scale), ry0 + round(h * scale)
    px0, py0 = max(0, rx0), max(0, ry0)
    px1, py1 = min(_FW, rx1), min(_FH, ry1)
    if px1 - px0 < 2 or py1 - py0 < 2:
        return None
    return (px0, py0, px1, py1)


def _edge_fill(inner_shape, ring):
    """兜底插值(_KNOWN=False 区域):四边双线性,cv2.resize SIMD。"""
    ih, iw = inner_shape
    t, b, l, r = ring
    vert_src = np.vstack([t[-1][None], b[0][None]])
    vert = cv2.resize(vert_src, (iw, ih), interpolation=cv2.INTER_LINEAR)
    horz_src = np.hstack([l[:, -1][:, None], r[:, 0][:, None]])
    horz = cv2.resize(horz_src, (iw, ih), interpolation=cv2.INTER_LINEAR)
    return cv2.addWeighted(vert, 0.5, horz, 0.5, 0)


def _capture_once(hide_cb=None):
    """线程单拍:抓轨迹区域 → 增量写干净缓冲(挖窗洞)→ 合成玻璃视图。

    raw = 抓拍瞬间精确窗洞(合成与记录用,绝不膨胀);
    跳写边界 = raw ∪ 上一拍 raw,再外扩 8px(只决定哪些像素不写缓冲)。
    """
    global _PREV_RECT, _LAST_BG, _LAST_COMPOSED, _PREV_HOLE, _VERSION
    cam = _cam()
    if cam is None:
        return False
    if _CLEAN is None:
        # 启动竞态自愈(2026-09-30 打包 exe 实测):seed_full 在 show 前
        # 播种,若此刻 DXGI 复制器未释放(上一个实例刚被杀,<4s 窗口)
        # 会静默失败,而种子原本只播一次 → 整场 veil 假背景无自愈。
        # 线程每拍补种(seed_full 可重入、持锁换缓冲),成功即恢复。
        if not seed_full():
            return False
    # 输出变化对账(2026-09-30 P1):相机在 access-loss 恢复后会自己更新
    # width/height,而 _FW/_FH 只在 seed_full 赋值一次 —— 每拍用相机当前
    # 尺寸对账,失配即整屏重播种(旧分辨率的干净基准已无意义)。覆盖
    # 『分辨率增大』方向:那边抓取不抛错,只有坐标被旧尺寸夹错的静默
    # 错位。属性缺失(异常实现)时跳过,越界方向仍有下方 ValueError 兜。
    cw, ch = getattr(cam, "width", 0), getattr(cam, "height", 0)
    if (cw or ch) and (cw != _FW or ch != _FH):
        _resync_output()
        return False
    x, y, w, h, scale = _REGION
    if w <= 0 or h <= 0:
        return False

    raw = _window_rect_now()
    if raw is None:
        return False     # 窗口在相机覆盖(主输出)之外:本拍无事可做
    sx0, sy0, sx1, sy1 = raw
    if _PREV_HOLE is not None:
        ox0, oy0, ox1, oy1 = _PREV_HOLE
        sx0 = max(0, min(sx0, ox0) - 8)
        sy0 = max(0, min(sy0, oy0) - 8)
        sx1 = min(_FW, max(sx1, ox1) + 8)
        sy1 = min(_FH, max(sy1, oy1) + 8)

    prev = _PREV_RECT or raw
    gx0 = max(0, min(prev[0], sx0) - _PAD)
    gy0 = max(0, min(prev[1], sy0) - _PAD)
    gx1 = min(_FW, max(prev[2], sx1) + _PAD)
    gy1 = min(_FH, max(prev[3], sy1) + _PAD)

    frame = None
    try:
        frame = cam.grab(region=(gx0, gy0, gx1, gy1))
    except ValueError:
        # 区域非法 ≈ 越界:大概率 _FW/_FH 停在旧屏尺寸。dxcam 的自愈
        # (_recover_output)只在真正尝试抓取后触发,纯区域校验失败不会,
        # 旧代码把本异常与其它异常一并吞掉 → 每拍都失败、玻璃永久冻结
        # (2026-09-30 P1)。主动对账重播种,新基准就位后下一拍恢复。
        _resync_output()
        return False
    except Exception:
        return False
    _PREV_RECT = raw
    _PREV_HOLE = raw

    if frame is not None:
        # 帧内洞坐标(签名与四带写入共用;必须在签名前定义 —— 首版放在
        # 锁内签名之后,签名块 NameError 被线程 except 吞掉,_RING_SIG
        # 恒 None,整条 blink 链从未点火,2026-09-29)
        fh, fw = frame.shape[:2]
        hx0 = sx0 - gx0
        hy0 = sy0 - gy0
        hx1 = sx1 - gx0
        hy1 = sy1 - gy0
        # 环带签名(v2,2026-09-29):只采样【洞外】的环带区域 —— 全帧版
        # 会把卡片自己的呼吸点动画算进签名(无限自激 blink);排除洞后
        # 签名变化 = 真的有别的窗口穿过环带。带内降采样求和。
        global _RING_SIG, _SIG_CHANGES
        s = (int(frame[0:hy0:2, ::8].sum())
             + int(frame[hy1:, ::8].sum())
             + int(frame[hy0:hy1:2, 0:hx0:8].sum())
             + int(frame[hy0:hy1:2, hx1:, ::8].sum()))
        if _RING_SIG is not None and s != _RING_SIG:
            _SIG_CHANGES += 1     # 防闪判据:变化【次数】(动画=高频,阶跃=3-6)
        _RING_SIG = s
        with _BUF_LOCK:
            if hy0 > 0:
                _CLEAN[gy0:gy0 + hy0, gx0:gx0 + fw] = frame[:hy0]
                _KNOWN[gy0:gy0 + hy0, gx0:gx0 + fw] = True
            if hy1 < fh:
                _CLEAN[gy0 + hy1:gy0 + fh, gx0:gx0 + fw] = frame[hy1:]
                _KNOWN[gy0 + hy1:gy0 + fh, gx0:gx0 + fw] = True
            if hx0 > 0:
                _CLEAN[gy0 + hy0:gy0 + hy1, gx0:gx0 + hx0] = \
                    frame[hy0:hy1, :hx0]
                _KNOWN[gy0 + hy0:gy0 + hy1, gx0:gx0 + hx0] = True
            if hx1 < fw:
                _CLEAN[gy0 + hy0:gy0 + hy1, gx0 + hx1:gx0 + fw] = \
                    frame[hy0:hy1, hx1:]
                _KNOWN[gy0 + hy0:gy0 + hy1, gx0 + hx1:gx0 + fw] = True
            _VERSION += 1
    elif raw == _LAST_COMPOSED:
        return False          # 静止且矩形未变:无需重合成

    rx0, ry0, rx1, ry1 = raw
    sub = _CLEAN[ry0:ry1, rx0:rx1]
    unk = ~_KNOWN[ry0:ry1, rx0:rx1]
    if unk.any():
        ih, iw = ry1 - ry0, rx1 - rx0
        ey0 = max(0, ry0 - 2)
        ey1 = min(_FH, ry1 + 2)
        ex0 = max(0, rx0 - 2)
        ex1 = min(_FW, rx1 + 2)
        top = _CLEAN[ey0:ry0, rx0:rx1] if ry0 > ey0 else \
            _CLEAN[ry0:ry0 + 1, rx0:rx1]
        bot = _CLEAN[ry1:ey1, rx0:rx1] if ry1 < ey1 else \
            _CLEAN[ry1 - 1:ry1, rx0:rx1]
        left = _CLEAN[ry0:ry1, ex0:rx0] if rx0 > ex0 else \
            _CLEAN[ry0:ry1, rx0:rx0 + 1]
        right = _CLEAN[ry0:ry1, rx1:ex1] if rx1 < ex1 else \
            _CLEAN[ry0:ry1, rx1 - 1:rx1]
        canvas = _edge_fill((ih, iw), (top, bot, left, right))
        sub = np.where(unk[:, :, None], canvas, sub)
    blur = cv2.GaussianBlur(sub, (21, 21), 0)
    _LAST_BG = np.ascontiguousarray(blur[:, :, ::-1])
    _LAST_COMPOSED = raw
    return True


def _refresh_blur():
    """预模糊区域:窗口外扩 _BPAD 一次模糊成 RGB(线程内 cv2 释放 GIL,
    ~1.8ms);窗口移出内半区或缓冲版本变了才重算。"""
    global _BLUR, _BLUR_RECT, _BLUR_VER
    if _CLEAN is None:
        return
    raw = _window_rect_now()
    if raw is None:
        return          # 窗口在相机覆盖外:保留旧缓存,view() 自会返回 None
    need = (_BLUR is None or _BLUR_RECT is None or _BLUR_VER != _VERSION or
            raw[0] < _BLUR_RECT[0] + _BPAD // 2 or
            raw[1] < _BLUR_RECT[1] + _BPAD // 2 or
            raw[2] > _BLUR_RECT[2] - _BPAD // 2 or
            raw[3] > _BLUR_RECT[3] - _BPAD // 2)
    if not need:
        return
    x0 = max(0, raw[0] - _BPAD)
    y0 = max(0, raw[1] - _BPAD)
    x1 = min(_FW, raw[2] + _BPAD)
    y1 = min(_FH, raw[3] + _BPAD)
    with _BUF_LOCK:
        sub = _CLEAN[y0:y1, x0:x1].copy()
    blur = cv2.GaussianBlur(sub, (21, 21), 0)
    _BLUR = np.ascontiguousarray(blur[:, :, ::-1])   # RGB
    _BLUR_RECT = (x0, y0, x1, y1)
    _BLUR_VER = _VERSION
    # 预模糊图换新 → paint 视图缓存里【同键位下裁自旧图】的结果全部
    # 失效:抓取线程『bump 版本 → 稍后刷新 _BLUR』的间隙里若有一次
    # paint,快路径会把旧图裁剪缓存进新版本键并永久命中(静止态版本
    # 不再推进),此处作废后下一拍重裁即愈
    _VIEW["key"] = None


def _thread_body():
    while not _THREAD_STOP:
        try:
            _capture_once()
            # 零闪盲区刷新(2026-09-30):线程内直抓卡片下方窗口合成,
            # 替代旧 opacity 隐身路径(主线程 QTimer + setWindowOpacity)。
            # 走 GDI 与相机无共享状态,无需让路;needs_blink 自带
            # 2.5s 冷却 + 动画静默,失败也有退避,不会热循环
            if needs_blink():
                refresh_hole_below()
            _refresh_blur()
        except Exception:
            pass
        # 拖动中 16ms/拍(60fps:内容刷新率;位置跟手由 paint 取景保证,
        # 无需 120fps —— 8ms 时代的双线程 GIL 争用是卡顿主源);稳态 100ms
        time.sleep(0.016 if (time.perf_counter() - _REGION_AT) < 0.6
                   else 0.100)


def start_background_thread():
    """启动后台抓取线程(幂等)。主线程只写 _REGION 坐标,线程自主抓。"""
    global _THREAD, _THREAD_STOP
    _THREAD_STOP = False
    if _THREAD is None or not _THREAD.is_alive():
        _THREAD = threading.Thread(target=_thread_body, daemon=True)
        _THREAD.start()


def stop_background_thread():
    global _THREAD_STOP
    _THREAD_STOP = True


def set_region(x: int, y: int, w: int, h: int, scale: float):
    """主线程更新抓取区域。_REGION_AT 只在【值真的变了】时刷新 ——
    _glass_timer 每 66ms 也调本函数,无条件刷新会让静止判定
    (needs_blink 的 0.5s 无变化)永远不成立,blink 从未触发
    (2026-09-29 用户实测静止盲区不更新,模块级测试无 Qt 定时器
    故未暴露)。"""
    global _REGION_AT
    if (x, y, w, h, scale) != tuple(_REGION):
        _REGION[0], _REGION[1] = x, y
        _REGION[2], _REGION[3], _REGION[4] = w, h, scale
        _REGION_AT = time.perf_counter()


def view(win):
    """paint 线程调用:窗口【此刻位置】的实时视图 —— 真玻璃/放大镜原理
    (世界=静态干净缓冲,取景框=当前窗口矩形,paint 时现场裁剪+模糊,
    与窗口移动逐帧锁步、零步进;与线程合成共用同一数学与 21px 模糊,
    像素逐位一致)。按 (rect, version) 缓存,静止命中零成本。"""
    if _CLEAN is None:
        return None
    raw = _window_rect_now()
    if raw is None:
        # 窗口在相机覆盖(主输出)之外 —— 多显示器副屏卡片没有真实背景
        # 可取。旧实现把矩形硬夹进主屏,这里返回过期缓存(_VIEW["np"]),
        # 副屏玻璃永远显示冻结的主屏内容且无回退;显式返回 None 才能让
        # _deco_liquid 落到自绘 veil 兜底(2026-09-30 P1)。
        return None
    rx0, ry0, rx1, ry1 = raw
    # 视图缓存键=(矩形, 版本):静止时矩形不变,但 blink 会写洞区并
    # 递增版本 —— 键缺版本时静止态永远命中旧缓存,blink 成果进不了
    # 视图(2026-09-29 R6:blinked=True 但视图恒旧)。拖动时矩形每帧
    # 变,版本在键中不产生额外未命中。呼吸点动画在洞内、洞不写入,
    # 版本不动,静止态不会无谓失效。
    key = (rx0, ry0, rx1, ry1, _VERSION)
    if _VIEW["key"] == key:
        return _VIEW["np"]
    # 快路径:窗口在预模糊区域内 → 纯裁剪(0.3ms memcpy,零模糊零 GIL)。
    # 版本不齐可接受:预模糊落后一两版在 21px 模糊下不可辨
    if (_BLUR is not None and _BLUR_RECT is not None
            and rx0 >= _BLUR_RECT[0] and ry0 >= _BLUR_RECT[1]
            and rx1 <= _BLUR_RECT[2] and ry1 <= _BLUR_RECT[3]):
        bx0, by0 = _BLUR_RECT[0], _BLUR_RECT[1]
        rgb = np.ascontiguousarray(
            _BLUR[ry0 - by0:ry1 - by0, rx0 - bx0:rx1 - bx0])
        _VIEW["key"] = key
        _VIEW["np"] = rgb
        return rgb
    with _BUF_LOCK:
        sub = _CLEAN[ry0:ry1, rx0:rx1].copy()
        unk = ~_KNOWN[ry0:ry1, rx0:rx1]
        if unk.any():
            ey0, ey1 = max(0, ry0 - 2), min(_FH, ry1 + 2)
            ex0, ex1 = max(0, rx0 - 2), min(_FW, rx1 + 2)
            top = _CLEAN[ey0:ry0, rx0:rx1] if ry0 > ey0 else                 _CLEAN[ry0:ry0 + 1, rx0:rx1]
            bot = _CLEAN[ry1:ey1, rx0:rx1] if ry1 < ey1 else                 _CLEAN[ry1 - 1:ry1, rx0:rx1]
            left = _CLEAN[ry0:ry1, ex0:rx0] if rx0 > ex0 else                 _CLEAN[ry0:ry1, rx0:rx0 + 1]
            right = _CLEAN[ry0:ry1, rx1:ex1] if rx1 < ex1 else                 _CLEAN[ry0:ry1, rx1 - 1:rx1]
            canvas = _edge_fill((ry1 - ry0, rx1 - rx0),
                                (top, bot, left, right))
            sub = np.where(unk[:, :, None], canvas, sub)
    blur = cv2.GaussianBlur(sub, (21, 21), 0)
    rgb = np.ascontiguousarray(blur[:, :, ::-1])
    _VIEW["key"] = key
    _VIEW["np"] = rgb
    return rgb


def latest():
    """paint 消费:最新模糊背景(np RGB)或 None。"""
    return _LAST_BG


def to_qimage(rgb):
    """np RGB → QImage(带 devicePixelRatio → 1:1 位块传输)。"""
    from PySide6.QtGui import QImage
    h, w = rgb.shape[:2]
    img = QImage(rgb.data, w, h, w * 3, QImage.Format_RGB888)
    img.setDevicePixelRatio(float(_REGION[4]) if _REGION[4] else 1.0)
    return img
