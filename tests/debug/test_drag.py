"""zcode-meter 拖动自动化测试:SendInput 合成 120Hz 拖动,逐帧量化窗口跟随情况。

用法: python test_drag.py <pid> <case>
  case: move    屏中拖到另一处(验证跟手/乱飞/迟滞)
        dock    拖到底边(验证吸附)
        undock  从条中心拖到屏中(验证恢复卡片)
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

user32 = ctypes.windll.user32
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x2, 0x4


def get_hwnd(pid):
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0]


def rect(hwnd):
    rc = wt.RECT()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rc))
    return rc.left, rc.top, rc.right, rc.bottom


def drag(hwnd, from_pt, to_pt, steps=40, interval=0.008):
    """合成拖动:按下 → 120Hz 步进移动(每帧记录鼠标/窗口位置) → 松开。"""
    user32.SetCursorPos(*from_pt)
    time.sleep(0.15)
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.1)
    samples = []
    fx, fy = from_pt
    tx, ty = to_pt
    for i in range(1, steps + 1):
        mx = fx + (tx - fx) * i // steps
        my = fy + (ty - fy) * i // steps
        user32.SetCursorPos(mx, my)
        time.sleep(interval)
        l, t, r, b = rect(hwnd)
        # 锚点=按下时鼠标相对窗口的偏移;窗口理想位置 = 鼠标 - 锚点
        offx = fx - rect_at_press[0]
        offy = fy - rect_at_press[1]
        ideal_x, ideal_y = mx - offx, my - offy
        samples.append((i, mx, my, l, t, l - ideal_x, t - ideal_y))
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.4)      # 等 _settle(30ms) 完成
    return samples


pid = int(sys.argv[1])
case = sys.argv[2]
hwnd = get_hwnd(pid)
l, t, r, b = rect(hwnd)
cx, cy = (l + r) // 2, (t + b) // 2
rect_at_press = (l, t)
print(f"window: ({l},{t})-({r},{b})  center=({cx},{cy})")

if case == "move":
    target = (cx + 500, cy + 300)
elif case == "dock":
    target = (cx, 1500)          # 拖向底部(工作区底 1540)
else:                             # undock: 条中心 → 屏中
    target = (cx + 400, cy - 200)

samples = drag(hwnd, (cx, cy), target)
print(f"{'frame':>5} {'mouse':>12} {'window':>12} {'lag_xy':>12}")
for i, mx, my, wl, wtx, dx, dy in samples[::5]:      # 每 5 帧抽样打印
    print(f"{i:>5} ({mx:>5},{my:>5}) ({wl:>5},{wtx:>5}) ({dx:>4},{dy:>4})")
l2, t2, r2, b2 = rect(hwnd)
print(f"final: ({l2},{t2})-({r2},{b2})")
max_lag = max(max(abs(s[5]), abs(s[6])) for s in samples)
avg_lag = sum(abs(s[5]) + abs(s[6]) for s in samples) / (2 * len(samples))
print(f"max_frame_lag={max_lag}px  avg_frame_lag={avg_lag:.1f}px")
