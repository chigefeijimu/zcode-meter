"""Liquid Glass 背景管线 v3.1:时域干净缓冲(Temporal Clean Buffer)。

核心洞察(2026-09-29,替代环推断方案):窗口拖到新位置【之前】,那块
屏幕是裸露的 —— 上一拍的抓帧里就有它的真实内容。维护全屏干净背景图
_CLEAN:每拍抓『窗口矩形+边距』的区域帧,把【窗口未覆盖】的部分写入
_CLEAN;窗口正下方的像素保留【窗口到达前】的历史真值。玻璃 =
模糊(_CLEAN[窗口矩形]) —— 真内容/真色彩/真形状,拖到哪都是
1~2 拍前的实况,肉眼即实时。

- 精确窗洞:抓拍瞬间 GetWindowRect;跳写边界 = raw 外扩 8px(GetWindowRect
  与帧落盘之间窗口还能挪几像素的余量);抓取区 = raw 外扩 _PAD
  (2026-10-06 #68:_PREV_RECT/_PREV_HOLE 双名并集机制整体退役 ——
  两全局在全部赋值点恒同值,docstring 自述的差异化生命周期从未落地,
  派生抓取区的 min/max(prev,…) 穷举+随机证实恒 no-op);
- 启动种子 seed_full():窗口 show 之前抓一帧全屏 → 整屏一次性全知;
  『未知像素/边缘双线性兜底』机制已随 #30 死重清理退役(种子恒覆盖
  全屏,_KNOWN 按构造恒 True,兜底分支不可达);
- 拖动中 16ms/拍(60fps),稳态 100ms(#53:与 _thread_body 尾部
  sleep 的真实节拍一致);静止时 DXcam 返回 None 零处理。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time

import numpy as np

try:
    import cv2
    # 可用性探测不实例化相机(#29):dxcam.create() 含输出适配器枚举 +
    # 内置 0.1s sleep,import 期实测白付 ~0.4s/次(无论皮肤是否 liquid、
    # 每次 pytest 会话同样付费),且 import 期相机与 _cam() 的 create 形成
    # 同键单例二次 create 的 stderr WARNING。find_spec 只查模块可寻址
    # (µs 级),相机唯一创建点收敛到 _cam();真 create 失败由 _cam 缓存
    # 坏账,不再每拍重付枚举。skins 侧 `if glass_effect.OK` 协议不变。
    OK = (importlib.util.find_spec("dxcam") is not None
          and importlib.util.find_spec("cv2") is not None)
except Exception:
    OK = False

_CAM = None
_CAM_RETRY_AT = 0.0    # create 失败后的退避重试时刻(#29 修订:启动竞态
                      # 一次失败曾=永久 veil 无自愈,改 5s 退避重试 —— 只在坏账期
                      # 每 5s 付一次 create 试探,健康期零重试)
_CAM_LOCK = threading.Lock()

_CLEAN = None         # 全屏 BGR 时域干净背景(窗口到达前的真值)
_FW, _FH = 0, 0       # 屏幕物理尺寸(seed_full 锁内与 _CLEAN 同步发布,#51)
_PAD = 60             # 抓取外边距(覆盖拖动轨迹)
_REGION = [0, 0, 0, 0, 1.25]
_REGION_AT = 0.0
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
# 预模糊状态单点发布(#32):旧 _BLUR/_BLUR_RECT 二段连续赋值非原子,
# paint 无锁快路径在换图间隙可交错读到『新数组+旧矩形』→ 裁出错位视图
# 并被 (rect,version) 键长期缓存(静止态版本不推进时错误缓存恒命中)。
# 单一元组一次引用赋值即原子(解读引用后三元组自洽),读取方一次读入。
_BLUR_STATE = None    # (np RGB, (x0,y0,x1,y1), version) 或 None
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


def _dbg_dir() -> str:
    """与 data_engine.app_dir 同锚:zm_debug.log 必须与引擎侧诊断同落点
    (frozen=exe 目录;脚本模式=含 README.md 的祖先目录=仓库根)。刻意
    本地复刻而不 import data_engine —— skins 的导入纪律严禁 data_engine,
    glass_effect 被 skins 函数级 import,不能反向把引擎模块带进 UI 链。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    d = here
    for _ in range(3):                     # src/zcode_meter 距仓库根两级,留裕量
        d = os.path.dirname(d)
        if d == os.path.dirname(d):        # 已到盘根仍未命中锚点
            break
        if os.path.isfile(os.path.join(d, "README.md")):
            return d
    return here


_DEBUG = os.environ.get("ZM_DEBUG") == "1"
_DBG_PATH = os.path.join(_dbg_dir(), "zm_debug.log")


def _dbg(msg: str):
    """诊断出口(#52):本模块曾因裸 except 无声吞异常,自记历史就是
    『NameError 被吞、_RING_SIG 恒 None,整条 blink 链从未点火』—— 持续
    性异常 = 玻璃静默冻结且零诊断。ZM_DEBUG=1 才落盘,任何 IO 失败静默
    (诊断不得反噬管线;exe 可能被放进只读目录)。"""
    if _DEBUG:
        try:
            with open(_DBG_PATH, "a", encoding="utf-8") as f:
                f.write("%s.%03d %s\n" % (time.strftime("%H:%M:%S"),
                                          int(time.time() * 1000) % 1000, msg))
        except OSError:
            pass


def needs_blink() -> bool:
    """静止(0.5s 无 region 变更)且环带内容自上次盲区刷新后变过。

    节流(2026-09-30 零闪改版后纯属 CPU 经济):PrintWindow 直抓不再有
    视觉代价,但一次要抓数十个相交窗口(数十到数百 ms 后台开销),保留
    冷却 2.5s + 动画源静默。动画判据用【变化次数】而非『变了没有』:
    冷却期内签名变 ≥10 次 = 连续动画源 → 静默;窗口划过是阶跃(3-6 次)
    → 正常刷新。
    静默的真实节拍(#78 如实化):持续动画期间是【开端 1 帧后 0 帧,
    直到动画停止满一个静默周期才恢复】—— mute 置位时清零 _SIG_CHANGES,
    但静默期内动画照常每拍累积计数(15s×100ms/拍≈150 次),期满评估
    必然再 ≥10 → 再 mute。旧注『视频垫背时玻璃 15s 更新一帧,可接受』
    与实现不符,勿再引用。"""
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
    (壁纸/无窗口区)保留调用方旧值,不用黑色覆写。

    锚定纪律(#50 修正案,2026-10-06):PrintWindow 的内容恒锚定
    GetWindowRect 原点 —— 探针实证(带 DWM 边框常规窗,纯红客户区):
    红块在 GetWindowRect 尺寸 DIB 中的偏移与 GW 锚定假设逐像素一致,
    与 extended-bounds 假设差 8px;按 EB 尺寸建 DIB 还会裁掉右/下内容。
    故 DIB 尺寸与 blit 数学全用 GetWindowRect;DWMWA_EXTENDED_FRAME_
    BOUNDS(9) 只用于两件事 —— 相交判定(不可见边框带不算窗口占位)
    与 covered 裁剪(GW-DIB 里 EB 之外的边框带是 PrintWindow 渲染出的
    不透明灰带,物理屏上透出的是身后内容,不得当真背景写洞区)。"""
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
            # 自家窗不能整 pid 一刀切排除(P1,2026-10-07):HistoryWindow/
            # SettingsDialog 是普通 band 窗(实测无 WS_EX_TOOLWINDOW),永远
            # 压在置顶卡片之下 —— 恰是洞区合成的合法对象;整 pid 排除把它们
            # 踢出合成,卡片悬于其上时洞区显示更深层窗口或 _CLEAN 旧值,
            # needs_blink 对同一排除反复触发,无提示无自愈。而旧注释要排除
            # 的『自家 tooltip』:z 序在置顶卡片之上的 tooltip 恒在 found 哨
            # 之前被跳过,pid 检查对它零作用 —— 该过滤实际能拦的只有卡片
            # 之下的自家窗。收窄为 WS_EX_TOOLWINDOW 位:真机实测 Qt.Tool
            # (卡片)/QMenu 的 exstyle=0x88(位在),普通 Qt.Window/
            # QDialog=0x100(位不在),两类按位精确分家;QToolTip 同属
            # tool window 族(即便个别形态缺位漏入合成,悬停中的 tooltip
            # 本就是卡片身后可见的真背景,合成它反而更真)。
            if user32.GetWindowLongW(h, -20) & 0x80:  # GWL_EXSTYLE & WS_EX_TOOLWINDOW
                return True                  # 自家 tooltip/菜单族(卡片之下)
            # 其余自家普通窗 = 洞区合法背景,放行入列
        gw = wintypes.RECT()
        if not user32.GetWindowRect(h, ctypes.byref(gw)):
            return True
        eb = wintypes.RECT()
        if dwapi.DwmGetWindowAttribute(h, 9, ctypes.byref(eb),
                                       ctypes.sizeof(eb)) != 0:
            eb.left, eb.top = gw.left, gw.top            # 查询失败:可见区
            eb.right, eb.bottom = gw.right, gw.bottom    # 退化为 GW 全域
        # 相交判定用可见边界(EB):只有不可见边框带压着洞区的窗口不算
        if eb.left < x1 and eb.right > x0 and eb.top < y1 and eb.bottom > y0:
            wins.append((h,
                         (gw.left, gw.top, gw.right, gw.bottom),
                         (eb.left, eb.top, eb.right, eb.bottom)))
        return True

    user32.EnumWindows(_cb, 0)
    if not found.value or not wins:
        return None, None

    canvas = np.zeros((H, W, 3), np.uint8)
    covered = np.zeros((H, W), bool)
    for h, gwr, ebr in reversed(wins):       # 底 → 顶
        bgra = _print_window_bgra(h, gwr[2] - gwr[0], gwr[3] - gwr[1])
        if bgra is None:
            continue
        # 写入区 = 洞区 ∩ 可见边界(EB)。源/目的坐标从【同一世界坐标】
        # 折算:DIB 源按 GW 原点(PrintWindow 内容锚定 GW,不是 EB),
        # 画布目的按洞区原点 —— 旧版目的按 EB 原点折算,系统性错位
        # 8~9px(registry#50 实测;修正后探针对位 ±0px)。
        vx0, vy0 = max(ebr[0], x0), max(ebr[1], y0)
        vx1, vy1 = min(ebr[2], x1), min(ebr[3], y1)
        sx0, sy0 = vx0 - gwr[0], vy0 - gwr[1]
        dx0, dy0 = vx0 - x0, vy0 - y0
        dx1, dy1 = vx1 - x0, vy1 - y0
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
    写 _CLEAN。

    退避首行无条件推进(#69,2026-10-06 覆盖全部退出路径):本函数四类
    出口 ——『_CLEAN 未就绪/_HWND 空』早退、_window_rect_now 无解、
    capture_below 抛异常、锁内写异常、成功 —— 一律先把 _BLINK_AT 推到
    当前时刻(统一 2.5s 冷却)。旧版只在两条 return False 路径赋值,
    异常路径永不推进 → needs_blink 每拍放行,16~100ms 节拍的
    EnumWindows+PrintWindow 无退避 GDI 热循环(实测重播种竞态可稳定
    触发 IndexError 进入该洞)。"""
    global _VERSION, _BLINK_SIG, _BLINK_AT, _SIG_CHANGES
    _BLINK_AT = time.perf_counter()
    if _CLEAN is None or not _HWND:
        return False
    raw = _window_rect_now()
    if raw is None:
        return False
    try:
        bgr, covered = capture_below(_HWND, raw)
    except Exception as e:
        _dbg("refresh_hole_below capture_below: %r" % (e,))
        return False
    if bgr is None:
        return False
    x0, y0, x1, y1 = raw
    try:
        with _BUF_LOCK:
            sub = _CLEAN[y0:y1, x0:x1]
            sub[covered] = bgr[covered]         # 未覆盖区保留旧真值
            _VERSION += 1
    except Exception as e:
        _dbg("refresh_hole_below write: %r" % (e,))
        return False
    _BLINK_SIG = _RING_SIG
    _SIG_CHANGES = 0
    return True


def _cam():
    """相机单例(唯一 create 点,#29):find_spec 只保证模块可寻址,真正
    可用与否(输出适配器/DXGI 权限)由 create 试探。失败 5s 退避重试
    (2026-10-06 修订:首版一次性坏账在启动竞态下=永久 veil 无自愈,
    用户实测『假背景』;退避让坏账期每 5s 仅付一次 create 试探,
    健康期零重试,两个意图兼得)。"""
    global _CAM, _CAM_RETRY_AT
    with _CAM_LOCK:
        if _CAM is None and OK and time.monotonic() >= _CAM_RETRY_AT:
            try:
                import dxcam                   # 延迟到首个真实用点(#29)
                _CAM = dxcam.create(output_color="BGR")
                if _CAM is None:
                    _CAM_RETRY_AT = time.monotonic() + 5.0
            except Exception as e:
                _CAM_RETRY_AT = time.monotonic() + 5.0
                _dbg("dxcam.create failed: %r" % (e,))
    return _CAM


def seed_full():
    """启动种子:窗口显示前抓一帧全屏 → 整屏干净(失败静默)。"""
    global _CLEAN, _FH, _FW, _VERSION
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
    # _FH/_FW 与 _CLEAN 同一临界区发布(#51 反方向撕裂,2026-10-06):
    # 旧版尺寸在锁外先落、缓冲锁内置换,运行期重播种(_resync_output 可
    # 自后台线程触发)交错时,paint 侧在拿锁【前】用新 _FW/_FH 夹矩形、
    # 切旧 _CLEAN → numpy 越界切片静默缩成 0 行数组,cv2.GaussianBlur 抛
    # 错被 _deco_liquid 吞成 veil 一帧 —— 与下方旧注释自述的撕裂方向
    # 相反的一侧原本敞着;view() 已同步改锁内取景,两端闭合。
    # 本函数不再只在线程启动前调用 —— _resync_output 会在运行期重播种,
    # paint 侧 view() 持同一把锁切片,不锁同样读到撕裂组合(2026-09-30 P1)。
    with _BUF_LOCK:
        _FH, _FW = f.shape[:2]
        _CLEAN = f.copy()
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
    global _BLUR_STATE
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
    # 旧屏坐标系的缓存必须一并作废(#68 后 _PREV_* 已随机制退役):
    # 预模糊区域/视图缓存按旧屏坐标裁剪,版本虽已 bump,这里显式清防
    # 快路径旧帧。
    _BLUR_STATE = None
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


def _capture_once():
    """线程单拍:抓窗口矩形周边区域 → 增量写干净缓冲(挖窗洞)。

    raw = 抓拍瞬间精确窗洞(签名与写入口径,绝不膨胀);跳写边界 =
    raw 外扩 8px;抓取区 = raw 外扩 _PAD(#68:_PREV_RECT/_PREV_HOLE
    双名并集机制退役 —— 两全局在全部赋值点恒同值,docstring 自述的
    差异化生命周期从未落地,派生抓取区的 min/max(prev,…) 穷举+随机
    证实恒 no-op;跳写边界同步直取 raw±8,暴露的旧窗口位置在帧中已是
    真背景,写入正是时域干净缓冲的本意)。
    尾部的『每帧 GaussianBlur → _LAST_BG』合成链已随 #30 死重清理退役
    (生产代码零消费者,skins 走 view();latest()/blink_capture()/
    _KNOWN 兜底同批删除,『未知像素』按构造不存在)。
    """
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
    # width/height,而 _FW/_FH 只在 seed_full 赋值 —— 每拍用相机当前
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
    rx0, ry0, rx1, ry1 = raw
    # 跳写边界(GetWindowRect 与帧落盘之间的位移余量)
    sx0 = max(0, rx0 - 8)
    sy0 = max(0, ry0 - 8)
    sx1 = min(_FW, rx1 + 8)
    sy1 = min(_FH, ry1 + 8)
    # 抓取区(覆盖拖动轨迹)
    gx0 = max(0, rx0 - _PAD)
    gy0 = max(0, ry0 - _PAD)
    gx1 = min(_FW, rx1 + _PAD)
    gy1 = min(_FH, ry1 + _PAD)

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

    if frame is None:
        # 静止:DXcam 无新帧,缓冲与签名都无需推进
        return False

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
    global _RING_SIG, _SIG_CHANGES, _VERSION
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
        if hy1 < fh:
            _CLEAN[gy0 + hy1:gy0 + fh, gx0:gx0 + fw] = frame[hy1:]
        if hx0 > 0:
            _CLEAN[gy0 + hy0:gy0 + hy1, gx0:gx0 + hx0] = \
                frame[hy0:hy1, :hx0]
        if hx1 < fw:
            _CLEAN[gy0 + hy0:gy0 + hy1, gx0 + hx1:gx0 + fw] = \
                frame[hy0:hy1, hx1:]
        _VERSION += 1
    return True


def _refresh_blur():
    """预模糊区域:窗口外扩 _BPAD 一次模糊成 RGB(线程内 cv2 释放 GIL,
    ~1.8ms);窗口移出内半区或缓冲版本变了才重算。"""
    global _BLUR_STATE
    if _CLEAN is None:
        return
    with _BUF_LOCK:
        # 锁内取景(#51 同款):_window_rect_now 经 _FW/_FH 夹矩形,尺寸
        # 与 _CLEAN 已同锁发布,锁外取景在重播种交错时可拿到失配组合
        raw = _window_rect_now()
        if raw is None:
            return          # 窗口在相机覆盖外:保留旧缓存,view() 自会返回 None
        st = _BLUR_STATE
        need = (st is None or st[2] != _VERSION or
                raw[0] < st[1][0] + _BPAD // 2 or
                raw[1] < st[1][1] + _BPAD // 2 or
                raw[2] > st[1][2] - _BPAD // 2 or
                raw[3] > st[1][3] - _BPAD // 2)
        if not need:
            return
        x0 = max(0, raw[0] - _BPAD)
        y0 = max(0, raw[1] - _BPAD)
        x1 = min(_FW, raw[2] + _BPAD)
        y1 = min(_FH, raw[3] + _BPAD)
        ver = _VERSION               # 与切片内容配套的版本(锁内定格)
        sub = _CLEAN[y0:y1, x0:x1].copy()
    blur = cv2.GaussianBlur(sub, (21, 21), 0)   # 重活锁外(cv2 释放 GIL)
    # 单点原子发布(#32):(rgb, rect, ver) 一次引用赋值,paint 侧一次
    # 读入即得自洽三元组 —— 旧 _BLUR/_BLUR_RECT 二段赋值间隙的无锁快
    # 路可交错读到『新数组+旧矩形』的错位视图并被 (rect,version) 键
    # 长期缓存(静止态版本不推进时错误缓存恒命中)
    _BLUR_STATE = (np.ascontiguousarray(blur[:, :, ::-1]),   # RGB
                   (x0, y0, x1, y1), ver)
    # 预模糊图换新 → paint 视图缓存里【同键位下裁自旧图】的结果全部
    # 失效:抓取线程『bump 版本 → 稍后刷新预模糊』的间隙里若有一次
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
            # 2.5s 冷却 + 动画静默,refresh_hole_below 首行无条件退避,
            # 失败路径不会热循环(#69)
            if needs_blink():
                refresh_hole_below()
            _refresh_blur()
        except Exception as e:
            # 单拍异常不再无声吞(#52):本文件注释自记的历史事故就是
            # 裸吞让 NameError 逃逸、『_RING_SIG 恒 None,整条 blink 链
            # 从未点火』—— 持续性异常(GDI 失败/cv2.error)= 玻璃静默
            # 冻结且零诊断。ZM_DEBUG=1 经 _dbg 落 zm_debug.log,未开时
            # 保持旧静默语义(零开销零打扰)
            _dbg("thread tick: %r" % (e,))
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
    故未暴露)。

    值变更同时清零 _SIG_CHANGES(#77,2026-10-06):自身拖动期间每拍
    环带签名必变(窗口移动 = 环带内容变),高频变化会被 needs_blink 的
    『≥10 次 = 连续动画』判据误读 —— 停拖后首次评估即 mute 15s,新位置
    的洞区真背景被推迟整个静默期,恰是该功能要解决的场景。拖动自己造成
    的计数在此作废:停拖 0.5s 后首评即刷新(拖动结束即刷新);真正的
    动画源(卡片静止、背景视频)不触发 region 变更,计数照常累积,
    静默判据不受影响。"""
    global _REGION_AT, _SIG_CHANGES
    if (x, y, w, h, scale) != tuple(_REGION):
        _REGION[0], _REGION[1] = x, y
        _REGION[2], _REGION[3], _REGION[4] = w, h, scale
        _REGION_AT = time.perf_counter()
        _SIG_CHANGES = 0


def view(win):
    """paint 线程调用:窗口【此刻位置】的实时视图 —— 真玻璃/放大镜原理
    (世界=静态干净缓冲,取景框=当前窗口矩形,paint 时现场裁剪+模糊,
    与窗口移动逐帧锁步、零步进;与线程合成共用同一数学与 21px 模糊,
    像素逐位一致)。按 (rect, version) 缓存,静止命中零成本。

    取景恒锚定 set_hwnd 钉住的模块级 _HWND(#88 如实化):win 形参为
    API 稳定性保留(skins._deco_liquid 的 per-window 词汇表),函数体
    不读它 —— 当前唯一调用方传入的 win 即 set_hwnd 的同一窗,无现行
    错误;未来双窗/测试/工具传他窗将拿到 _HWND 窗口的视图,那是
    documented 行为而非意外(改签名需动 skins.py 消费点,不值)。"""
    if _CLEAN is None:
        return None
    with _BUF_LOCK:
        # 锁内取景(#51):_window_rect_now 经 _FW/_FH 夹矩形,seed_full
        # 已把尺寸与 _CLEAN 同锁发布 —— 锁外取景在运行期重播种交错时
        # 可拿『新尺寸+旧缓冲』,numpy 越界切片静默缩成 0 行数组,
        # cv2 抛错被 _deco_liquid 吞成 veil 一帧(2026-09-30 P1 注释
        # 只封了写侧撕裂,读侧这半原本敞着,现已两端闭合)
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
        # 快路径:窗口在预模糊区域内 → 纯裁剪(0.3ms memcpy,零模糊零
        # GIL)。_BLUR_STATE 单点原子发布(#32):一次读入的
        # (rgb, rect, ver) 自洽,不存在『新数组+旧矩形』交错窗口。
        # 版本不齐可接受:预模糊落后一两版在 21px 模糊下不可辨
        st = _BLUR_STATE
        if st is not None:
            rgb_b, br, _ = st
            if rx0 >= br[0] and ry0 >= br[1] \
                    and rx1 <= br[2] and ry1 <= br[3]:
                rgb = np.ascontiguousarray(
                    rgb_b[ry0 - br[1]:ry1 - br[1], rx0 - br[0]:rx1 - br[0]])
                _VIEW["key"] = key
                _VIEW["np"] = rgb
                return rgb
        sub = _CLEAN[ry0:ry1, rx0:rx1].copy()
    blur = cv2.GaussianBlur(sub, (21, 21), 0)
    rgb = np.ascontiguousarray(blur[:, :, ::-1])
    _VIEW["key"] = key
    _VIEW["np"] = rgb
    return rgb


def to_qimage(rgb):
    """np RGB → QImage(带 devicePixelRatio → 1:1 位块传输)。"""
    from PySide6.QtGui import QImage
    h, w = rgb.shape[:2]
    img = QImage(rgb.data, w, h, w * 3, QImage.Format_RGB888)
    img.setDevicePixelRatio(float(_REGION[4]) if _REGION[4] else 1.0)
    return img
