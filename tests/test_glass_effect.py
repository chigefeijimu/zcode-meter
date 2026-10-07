#!/usr/bin/env python3
"""glass_effect 玻璃管线护栏(v0.9.2 T2:registry open #29/30/32/51/68/69/77 的
最小可执行断言)。

背景:tests/ 此前零处 import glass_effect(registry#52/#88 自记),管线
治理没有任何护栏。本文件终结该状态 —— 全部用模块级状态注入 + 假时钟 +
合成相机,不触碰真实屏幕、不依赖 dxcam 安装(相机直接注入 _CAM,绕过
_cam() 的真实 create;无 dxcam 机器上 OK 探测断言同样成立 —— find_spec
缺席即 False,与实现互为镜像)。

standalone 可跑:T6 统一注册进 run_all 默认组。
"""
import importlib.util
import os
import sys
from pathlib import Path

# 与 test_stress/test_package 同款隔离:直接运行本文件也不触碰用户状态
os.environ.setdefault("ZM_NO_STATE", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

# #29 断言素材:import 后立即定格 dxcam 是否被拉入(必须在任何后续
# 操作前取快照)
import zcode_meter.glass_effect as ge  # noqa: E402

DXCAM_AT_IMPORT = "dxcam" in sys.modules
CAM_AT_IMPORT = ge._CAM
CAM_BROKEN_AT_IMPORT = ge._CAM_RETRY_AT

try:
    import cv2  # noqa: F401
    HAVE_CV2 = True
except Exception:
    HAVE_CV2 = False

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class FakeTime:
    """假时钟:perf_counter 读当前值,sleep 推进 —— needs_blink/
    set_region/refresh_hole_below 的全部时间门可在 µs 级确定性驱动。"""

    def __init__(self, t0=1000.0):
        self.t = float(t0)

    def perf_counter(self):
        return self.t

    def sleep(self, s):
        self.t += s


FT = FakeTime()
ge.time = FT          # 模块级注入:ge 内所有 time.* 走假时钟


def reset_blink(region_age=5.0):
    """复位 blink 判定链的五个模块级状态(默认:静止远超 0.5s)。"""
    ge._RING_SIG = None
    ge._BLINK_SIG = None
    ge._SIG_CHANGES = 0
    ge._BLINK_AT = 0.0
    ge._BLINK_MUTE = 0.0
    ge._REGION_AT = FT.t - region_age


# ══════════════════ #29 import 期零相机实例化 ══════════════════

def test_import_probe():
    check("#29 import glass_effect 不拉 dxcam(延迟到 _cam 唯一 create 点)",
          not DXCAM_AT_IMPORT)
    check("#29 import 期未创建相机实例(_CAM/_CAM_RETRY_AT 初值)",
          CAM_AT_IMPORT is None and CAM_BROKEN_AT_IMPORT == 0.0)
    check("#29 OK=find_spec 探测且为 bool(与实现互为镜像,无 dxcam 机器同样成立)",
          isinstance(ge.OK, bool)
          and ge.OK == (importlib.util.find_spec("dxcam") is not None
                        and importlib.util.find_spec("cv2") is not None),
          f"OK={ge.OK}")
    check("#29 预模糊状态改为单一元组 _BLUR_STATE(初值 None)",
          hasattr(ge, "_BLUR_STATE") and ge._BLUR_STATE is None)


# ══════════════════ #30/#68 死重符号清偿 ══════════════════

def test_dead_names_gone():
    gone = ("latest", "blink_capture", "_edge_fill", "_KNOWN", "_LAST_BG",
            "_LAST_COMPOSED", "_PREV_RECT", "_PREV_HOLE",
            "_BLUR", "_BLUR_RECT", "_BLUR_VER")
    for n in gone:
        check(f"#30/#68 死重符号 {n} 已删除(防回归复活)", not hasattr(ge, n))
    check("#30 to_qimage 保留(skins 消费)", callable(getattr(ge, "to_qimage", None)))


# ══════════════════ #30/#88 view 无缓冲路径 ══════════════════

def test_view_none_when_no_buffer():
    ge._CLEAN = None
    ge._VIEW["key"] = None
    # 传 object():win 是 documented 死参(#88),任何垃圾对象都不该被读
    check("#30/#88 _CLEAN None 时 view() 返回 None(deco 落 veil 兜底)",
          ge.view(object()) is None)


# ══════════════════ #32 view 快路径:_BLUR_STATE 单点原子 ══════════════════

def test_view_fast_path_blur_state():
    W, H = 200, 150
    ge._HWND = 0
    ge._FW, ge._FH = W, H
    ge._REGION[:] = [40, 30, 20, 10, 1.0]      # raw = (40,30,60,40)
    ge._VIEW["key"] = None
    ge._CLEAN = np.zeros((H, W, 3), np.uint8)
    src = np.arange(H * W * 3, dtype=np.uint8).reshape(H, W, 3)
    # 单一元组发布:一次读入的 (rgb, rect, ver) 自洽 —— 快路径裁剪必与
    # 声明矩形配套,不存在『新数组+旧矩形』交错(#32)
    ge._BLUR_STATE = (np.ascontiguousarray(src), (0, 0, W, H), 1)
    v = ge.view(object())
    ok = (v is not None and v.shape == (10, 20, 3)
          and (v == src[30:40, 40:60]).all())
    check("#32 view 快路径裁自 _BLUR_STATE[0] 且偏移按 _BLUR_STATE[1]", ok,
          f"v={None if v is None else v.shape}")
    check("#32 (rect,version) 键命中:静止第二次调用零成本(identity)",
          ge.view(object()) is v)
    # 慢路径:_BLUR_STATE 缺席 → _CLEAN 切片 + 高斯模糊(cv2 依赖;
    # 缺 cv2 的机器跳过 —— 模块 import 期同样会失败,属环境前提)
    if HAVE_CV2:
        ge._BLUR_STATE = None
        ge._VIEW["key"] = None
        ge._CLEAN[:] = 128                       # 恒色底:模糊后仍恒 128
        v3 = ge.view(object())
        check("#51/#32 view 慢路径走锁内取景+切片模糊(_BLUR_STATE=None)",
              v3 is not None and v3.shape == (10, 20, 3)
              and bool((v3 == 128).all()),
              f"v3={None if v3 is None else (v3.shape, int(v3.min()), int(v3.max()))}")


# ══════════════════ #68 合成相机:跳写边界 raw±8 / 抓取区 raw±_PAD ══════════════════

def test_capture_once_geometry():
    class FakeCam:
        """合成相机:返回恒色帧,记录每次 grab 的 region(不触真实屏)。"""

        def __init__(self):
            self.regions = []

        def grab(self, region=None):
            self.regions.append(tuple(region) if region else None)
            x0, y0, x1, y1 = region
            return np.full((y1 - y0, x1 - x0, 3), 200, np.uint8)

    cam = FakeCam()
    ge._CAM = cam                    # 直接注入,_cam() 原样返回(#29 不触发 create)
    ge._CAM_RETRY_AT = 0.0
    ge._HWND = 0
    ge._FW, ge._FH = 200, 150
    ge._CLEAN = np.zeros((150, 200, 3), np.uint8)
    ge._VERSION = 0
    ge._RING_SIG = None
    ge._SIG_CHANGES = 0
    ge._REGION_AT = FT.t - 10.0
    ge._REGION[:] = [40, 30, 20, 10, 1.0]        # raw = (40,30,60,40)

    ok = ge._capture_once()
    # 抓取区 = raw±_PAD(40-60→0, 30-60→0, 60+60=120, 40+60=100)
    check("#68 第一拍抓取区 = raw±_PAD(并集机制退役)",
          ok is True and cam.regions[-1] == (0, 0, 120, 100),
          f"regions={cam.regions[-1:]}")
    # 跳写边界 = raw±8 → 洞 (32,22,68,48):四带写入 200,洞内保持旧值 0
    bands = (bool((ge._CLEAN[0:22, 0:120] == 200).all())
             and bool((ge._CLEAN[48:100, 0:120] == 200).all())
             and bool((ge._CLEAN[22:48, 0:32] == 200).all())
             and bool((ge._CLEAN[22:48, 68:120] == 200).all()))
    check("#68/#30 四环带写入且洞(raw±8)不写(_KNOWN 链删除后口径不变)",
          bands and bool((ge._CLEAN[22:48, 32:68] == 0).all())
          and bool((ge._CLEAN[130, 190] == 0).all()))
    check("#30 _capture_once 无参数签名(hide_cb 死参删除)",
          ge._capture_once.__code__.co_argcount == 0,
          f"argcount={ge._capture_once.__code__.co_argcount}")
    # 第二拍窗口移动:raw=(80,30,100,40),抓取区必须【不含】与上一拍的并集
    ge._REGION[:] = [80, 30, 20, 10, 1.0]
    ok2 = ge._capture_once()
    check("#68 移动后抓取区 = 新 raw±_PAD,不再 ∪ 上一拍 raw",
          ok2 is True and cam.regions[-1] == (20, 0, 160, 100),
          f"regions={cam.regions[-1:]}")
    # 新右带 [120:160]×[22:48] 写入;新洞 [72:108]×[22:48] 保留第一拍真值 200
    check("#68 移动后新洞保留时域旧真值(洞=只跳写不清写)",
          bool((ge._CLEAN[22:48, 120:160] == 200).all())
          and bool((ge._CLEAN[30, 90] == 200).all())
          and ge._VERSION == 2)
    check("#29 全程未触发 dxcam import(合成相机绕过 _cam create)",
          "dxcam" not in sys.modules)
    ge._CAM = None
    ge._CAM_RETRY_AT = 0.0


# ══════════════════ set_region:值不变不刷新 + #77 拖动清零 ══════════════════

def test_set_region():
    ge._REGION[:] = [10, 20, 30, 40, 1.0]
    ge._REGION_AT = 123.456
    ge._SIG_CHANGES = 9
    ge.set_region(10, 20, 30, 40, 1.0)           # 值相同(66ms 定时器重复调)
    check("set_region 值不变不刷新 _REGION_AT(静止判定不被定时器打死)",
          ge._REGION_AT == 123.456 and ge._SIG_CHANGES == 9)
    ge.set_region(11, 20, 30, 40, 1.0)           # 值变更(拖动/移动)
    check("set_region 值变更刷新 _REGION_AT 且同步清零 _SIG_CHANGES(#77)",
          ge._REGION_AT == FT.t and ge._SIG_CHANGES == 0 and ge._REGION[0] == 11)


# ══════════════════ needs_blink 纯逻辑注入(#77/#78 判据) ══════════════════

def test_needs_blink_gates():
    # 冷却 2.5s
    reset_blink()
    ge._RING_SIG, ge._BLINK_SIG, ge._SIG_CHANGES = 100, None, 3
    ge._BLINK_AT = FT.t - 1.0
    check("needs_blink 冷却期内 False(2.5s)", ge.needs_blink() is False)
    FT.sleep(2.6)
    check("needs_blink 冷却期满+环带变化+静止 → True", ge.needs_blink() is True)
    # 动画静默期内恒 False
    ge._BLINK_MUTE = FT.t + 10.0
    check("needs_blink 静默期内 False(15s)", ge.needs_blink() is False)
    ge._BLINK_MUTE = 0.0
    # 稳定环带:计数作废
    reset_blink()
    ge._RING_SIG = ge._BLINK_SIG = 100
    ge._SIG_CHANGES = 8
    check("needs_blink 环带稳定 → False 且计数作废",
          ge.needs_blink() is False and ge._SIG_CHANGES == 0)
    # 刚移动过(<0.5s)
    reset_blink(region_age=0.2)
    ge._RING_SIG, ge._BLINK_SIG, ge._SIG_CHANGES = 100, None, 3
    ge._BLINK_AT = FT.t - 10.0
    check("needs_blink 0.5s 内有 region 变更 → False", ge.needs_blink() is False)
    # 真动画:≥10 次 → mute 15s
    reset_blink()
    ge._RING_SIG, ge._BLINK_SIG, ge._SIG_CHANGES = 100, None, 12
    ge._BLINK_AT = FT.t - 10.0
    fired = ge.needs_blink()
    check("needs_blink 连续变化 ≥10 次 → False 并置 mute=now+15",
          fired is False and abs(ge._BLINK_MUTE - (FT.t + 15.0)) < 1e-6
          and ge._SIG_CHANGES == 0)
    # 窗口划过:阶跃 3-6 次 → 正常放行
    reset_blink()
    ge._RING_SIG, ge._BLINK_SIG, ge._SIG_CHANGES = 100, None, 5
    ge._BLINK_AT = FT.t - 10.0
    check("needs_blink 阶跃 5 次(窗口划过)→ True", ge.needs_blink() is True)


def test_needs_blink_drag_not_muted():
    """#77:自身拖动的高频环带签名变化不得被判成连续动画源。

    复刻真实时间线(实测复现脚本 _conf_ge_drag_mute_indep.py:拖 25 拍
    →_SIG_CHANGES=24→停拖 0.6s 后 mute 15s):每拍『先 set_region(值变,
    moveEvent)再签名计数(_capture_once)』—— 修复后 set_region 清零,
    停拖首次评估必须放行刷新而非 mute。"""
    reset_blink()
    for i in range(25):
        ge.set_region(100 + i * 3, 200, 313, 341, 1.0)   # moveEvent 每拍
        s = 5000 + i                                      # 环带签名每拍变
        if ge._RING_SIG is not None and s != ge._RING_SIG:
            ge._SIG_CHANGES += 1                          # 复刻计数三行
        ge._RING_SIG = s
        FT.sleep(0.016)
    FT.sleep(0.6)                                         # 停拖静止 0.6s
    ge._BLINK_AT = FT.t - 10.0                            # 冷却早已过
    mute_before = ge._BLINK_MUTE
    fired = ge.needs_blink()
    check("#77 拖 25 拍后停拖:首次评估放行刷新(不误判动画)",
          fired is True and ge._BLINK_MUTE == mute_before,
          f"fired={fired} changes={ge._SIG_CHANGES}")
    # 对照:真动画(卡片静止无 region 变更)仍要 mute —— 防修过头
    reset_blink()
    for i in range(12):
        s = 9000 + i
        if ge._RING_SIG is not None and s != ge._RING_SIG:
            ge._SIG_CHANGES += 1
        ge._RING_SIG = s
    ge._BLINK_AT = FT.t - 10.0
    fired = ge.needs_blink()
    check("#77 对照:静止卡片的连续动画计数仍触发 mute(判据未被稀释)",
          fired is False and ge._BLINK_MUTE > FT.t)


# ══════════════════ #69 refresh_hole_below 全退出路径退避 ══════════════════

def test_refresh_backoff_all_paths():
    orig_rect = ge._window_rect_now
    orig_cap = ge.capture_below
    try:
        ge._HWND = 1
        ge._FW, ge._FH = 200, 150
        ge._window_rect_now = lambda: (40, 30, 100, 70)
        reset_blink()
        ge._RING_SIG = 777
        ge._SIG_CHANGES = 5
        # ① _CLEAN None 早退
        ge._CLEAN = None
        FT.t += 1.0
        r = ge.refresh_hole_below()
        check("#69 早退路径(_CLEAN None)推进 _BLINK_AT",
              r is False and ge._BLINK_AT == FT.t)
        # ② capture_below 抛异常
        ge._CLEAN = np.zeros((150, 200, 3), np.uint8)
        calls = {"n": 0}

        def boom(hwnd, rect):
            calls["n"] += 1
            raise RuntimeError("simulated GDI failure")

        ge.capture_below = boom
        FT.t += 1.0
        r = ge.refresh_hole_below()
        check("#69 capture_below 抛异常:函数内捕获返回 False 且退避推进",
              r is False and ge._BLINK_AT == FT.t and calls["n"] == 1)
        # ③ bgr=None 路径
        ge.capture_below = lambda hwnd, rect: (None, None)
        FT.t += 1.0
        r = ge.refresh_hole_below()
        check("#69 bgr=None 路径推进 _BLINK_AT", r is False and ge._BLINK_AT == FT.t)
        # ④ 锁内写异常(蒙版形状失配 IndexError)
        ge.capture_below = lambda hwnd, rect: (
            np.full((40, 60, 3), 255, np.uint8),
            np.ones((30, 30), bool))                # 错形蒙版 → IndexError
        FT.t += 1.0
        r = ge.refresh_hole_below()
        check("#69 锁内写异常:函数内捕获返回 False 且退避推进",
              r is False and ge._BLINK_AT == FT.t)
        # ⑤ 成功路径
        ge._CLEAN = np.zeros((150, 200, 3), np.uint8)
        ge.capture_below = lambda hwnd, rect: (
            np.full((40, 60, 3), 255, np.uint8),
            np.ones((40, 60), bool))
        ver = ge._VERSION
        FT.t += 1.0
        r = ge.refresh_hole_below()
        check("#69 成功路径:写洞+版本推进+签名记账",
              r is True and ge._BLINK_AT == FT.t
              and bool((ge._CLEAN[30:70, 40:100] == 255).all())
              and ge._VERSION == ver + 1
              and ge._BLINK_SIG == 777 and ge._SIG_CHANGES == 0)
    finally:
        ge._window_rect_now = orig_rect
        ge.capture_below = orig_cap


def test_no_gdi_hot_loop_on_persistent_failure():
    """#69 核心:capture_below 持续抛异常时不得形成 16~100ms GDI 热循环。

    复刻 _thread_body 稳态节拍(100ms/拍)15 拍:修复前 15/15 拍全部
    放行+重入(实测脚本 _conf4_blink_exception_backoff_indep);修复后
    首拍退避 2.5s,后续拍 needs_blink 全拦 —— capture_below 只被调 1 次。"""
    orig_rect = ge._window_rect_now
    orig_cap = ge.capture_below
    try:
        ge._HWND = 1
        ge._FW, ge._FH = 200, 150
        ge._CLEAN = np.zeros((150, 200, 3), np.uint8)
        ge._window_rect_now = lambda: (40, 30, 100, 70)
        calls = {"n": 0}

        def boom(hwnd, rect):
            calls["n"] += 1
            raise RuntimeError("escape")

        ge.capture_below = boom
        reset_blink()
        ge._RING_SIG = 555001
        ge._SIG_CHANGES = 4
        ge._BLINK_AT = 0.0
        triggers = 0
        for _ in range(15):
            if ge.needs_blink():
                triggers += 1
                ge.refresh_hole_below()      # 异常在函数内部捕获(#69)
            FT.sleep(0.100)
        check("#69 持续异常 15 拍:仅首拍触发,无 EnumWindows/PrintWindow 热循环",
              triggers == 1 and calls["n"] == 1,
              f"triggers={triggers} calls={calls['n']}")
    finally:
        ge._window_rect_now = orig_rect
        ge.capture_below = orig_cap


def main() -> int:
    print("== glass_effect 玻璃管线护栏 ==")
    test_import_probe()
    test_dead_names_gone()
    test_view_none_when_no_buffer()
    test_view_fast_path_blur_state()
    test_capture_once_geometry()
    test_set_region()
    test_needs_blink_gates()
    test_needs_blink_drag_not_muted()
    test_refresh_backoff_all_paths()
    test_no_gdi_hot_loop_on_persistent_failure()
    if FAILED:
        print(f"\nFAILED: {FAILED}")
        return 1
    print("\nGLASS EFFECT TESTS ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
