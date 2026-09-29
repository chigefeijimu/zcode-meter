"""Liquid Glass 背景管线(DXcam 区域抓取 + 光学处理)。

方案 C 落地路径(2026-09-29,用户拍板『做真实特效的玻璃效果』):
Windows 系统亚克力(backdrop/CompositionAttribute)在 Qt frameless 窗口
上或带矩形黑角或不渲染(实验矩阵见 CHANGELOG),唯一能拿到『玻璃身后
真实世界』的路径是 Desktop Duplication API 屏幕抓取 —— DXcam 封装
(无特权、区域抓取、帧不变优化)。

管线:每 paint 周期抓窗口物理矩形 → 排除自身(前帧回填)→ 高斯模糊 →
折射位移(边缘 SDF 法线)→ QImage 给 paintEvent drawImage。
背景静止时 DXcam 返回 None(帧不变优化)→ 复用上帧,零 CPU。"""

from __future__ import annotations

import time
import threading

import numpy as np

_CAM = None            # 进程级单例(DXcam 设备上下文昂贵)
_LAST_REGION = None
_LAST_BG = None        # 上帧背景(np RGB),自剔除与帧不变复用
OK = False
_THREAD = None         # 后台抓取线程(Qt timer 在 startSystemMove 模态
_THREAD_STOP = False   # 循环里不调度 → 拖动中背景冻结,用户 2026-09-29;
_REGION = [0, 0, 0, 0, 1.25]  # [x,y,w,h,scale] 主线程原子写入,线程读

try:
    import dxcam
    import cv2
    _cam_test = dxcam.create(output_color="BGR")
    OK = _cam_test is not None
except Exception:
    OK = False


_CAM_LOCK = threading.Lock()


def _cam():
    global _CAM
    with _CAM_LOCK:
        if _CAM is None and OK:
            # 单例创建加锁即可;此前误加 __factory.clean_up() 会把实例
            # 清死(线程每次 grab 失败 → latest 恒 None → deco 恒走 veil,
            # 用户『透视永不变化』完整真因,2026-09-29)
            _CAM = dxcam.create(output_color="BGR")
    return _CAM


_PAD = 72               # 环宽(物理 px):窗口外围抓一圈,不含自身


def _edge_fill(inner_shape, ring):
    """用外围环推断内部:(v2 双线性边缘插值,2026-09-29 用户『透视不随
    位置变化』修正)——
      垂直分量:每行 = 顶部条最内行 与 底部条最内行 按 y 距离线性混合;
      水平分量:每列 = 左侧条最内列 与 右侧条最内列 按 x 距离线性混合;
      两者 50/50 加权。移到不同背景上时,四边的真实屏幕色不同,内部即
      呈现这些色的连续过渡 —— 21px 高斯模糊下与真实背景的大尺度色彩
      分布几乎一致(旧版取环均值,任何位置内部都是同一均匀色)。
    ring=(top, bottom, left, right) 条带。"""
    ih, iw = inner_shape
    t, b, l, r = ring
    top_row = t[-1].astype(np.float32)       # (iw,3) 紧邻窗口上缘的屏幕行
    bot_row = b[0].astype(np.float32)
    left_col = l[:, -1].astype(np.float32)   # (ih,3)
    right_col = r[:, 0].astype(np.float32)
    ty = (np.arange(ih, dtype=np.float32) / max(ih - 1, 1))[:, None, None]
    tx = (np.arange(iw, dtype=np.float32) / max(iw - 1, 1))[None, :, None]
    vert = top_row[None, :, :] * (1.0 - ty) + bot_row[None, :, :] * ty
    horz = left_col[:, None, :] * (1.0 - tx) + right_col[:, None, :] * tx
    return np.clip(vert * 0.5 + horz * 0.5, 0, 255).astype(np.uint8)


def _capture_once(hide_cb=None):
    """线程体单拍:抓窗口【外围环】(不含自身)→ 推断内部 → 模糊 → _LAST_BG。
    【环抓取】(不闪的自剔除,2026-09-29 用户『一直在闪』后定稿):opacity
    闪烁方案每拍隐藏 30ms 肉眼可见;环抓取抓窗口外扩 _PAD 的边框区域,
    自身完全不在帧内,内部由环的边条均值+渐变推断 —— 高斯模糊 21px 下
    与真实背景几乎不可区分(玻璃质感主体是模糊色域而非细节)。"""
    global _LAST_REGION, _LAST_BG
    cam = _cam()
    if cam is None:
        return False
    x, y, w, h, scale = _REGION
    if w <= 0 or h <= 0:
        return False
    try:
        full = cam.grab()                    # 全屏帧(物理像素)
    except Exception:
        return False
    if full is None:
        return False
    fh, fw = full.shape[:2]
    px0, py0 = round(x * scale), round(y * scale)
    px1, py1 = round((x + w) * scale), round((y + h) * scale)
    px0, py0 = max(0, min(px0, fw - 1)), max(0, min(py0, fh - 1))
    px1, py1 = max(px0 + 1, min(px1, fw)), max(py0 + 1, min(py1, fh))
    _LAST_REGION = (px0, py0, px1, py1)
    # 外围环四条(各 _PAD 宽,clamp 到屏幕)
    top = full[max(0, py0-_PAD):py0, px0:px1] if py0 > 0 else None
    bot = full[py1:min(fh, py1+_PAD), px0:px1] if py1 < fh else None
    left = full[py0:py1, max(0, px0-_PAD):px0] if px0 > 0 else None
    right = full[py0:py1, px1:min(fw, px1+_PAD)] if px1 < fw else None
    ih, iw = py1 - py0, px1 - px0
    if top is None:
        top = bot if bot is not None else np.zeros((1, 1, 3), np.uint8)
    if bot is None:
        bot = top
    if left is None:
        left = right if right is not None else np.zeros((1, 1, 3), np.uint8)
    if right is None:
        right = left
    canvas = _edge_fill((ih, iw), (top, bot, left, right))
    blur = cv2.GaussianBlur(canvas, (21, 21), 0)
    _LAST_BG = np.ascontiguousarray(blur[:, :, ::-1])
    return True


_HIDE_CB = None         # 主线程注入的 setWindowOpacity 回调(自剔除)


def set_hide_cb(cb):
    global _HIDE_CB
    _HIDE_CB = cb


def _thread_body():
    import time as _t
    while not _THREAD_STOP:
        try:
            _capture_once(_HIDE_CB)
        except Exception:
            pass
        _t.sleep(0.066)


def start_background_thread():
    """启动后台抓取线程(幂等)。主线程只写 _REGION 坐标,线程自主抓。"""
    global _THREAD
    global _THREAD_STOP
    _THREAD_STOP = False
    if _THREAD is None or not _THREAD.is_alive():
        import threading
        _THREAD = threading.Thread(target=_thread_body, daemon=True)
        _THREAD.start()


def stop_background_thread():
    global _THREAD_STOP
    _THREAD_STOP = True


def set_region(x: int, y: int, w: int, h: int, scale: float):
    """主线程更新抓取区域(拖动中每 tick 调;元组赋值原子)。"""
    _REGION[0], _REGION[1] = x, y
    _REGION[2], _REGION[3], _REGION[4] = w, h, scale


def latest():
    """paint 消费:最新模糊背景(np RGB)或 None。"""
    return _LAST_BG


def to_qimage(rgb):
    """np RGB → QImage(调用方持引用防 GC)。"""
    from PySide6.QtGui import QImage
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, w * 3, QImage.Format_RGB888)
