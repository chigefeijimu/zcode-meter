"""zcode-meter 拖动自动化测试 v2 —— 本进程声明 PerMonitorV2 DPI 感知,
坐标全部物理像素,与目标窗口一致,合成输入才会被 DPI 感知窗口接受。

用法: python test_drag2.py <pid> <case>
  case: move   屏中拖到另一处(验证跟手/乱飞/迟滞)
        dock   拖到底边(验证吸附)
        undock 从条拖到屏中(验证恢复卡片)
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

user32 = ctypes.windll.user32
user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))    # 必须最先声明

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


pid = int(sys.argv[1])
case = sys.argv[2]
hwnd = get_hwnd(pid)
l, t, r, b = rect(hwnd)
cx, cy = (l + r) // 2, (t + b) // 2
print(f"window(physical): ({l},{t})-({r},{b})  center=({cx},{cy})")

if case == "move":
    target = (min(cx + 500, 2400), min(cy + 300, 1300))
elif case == "dock":
    target = (cx, 1500)            # 工作区底 1540
else:
    target = (cx - 400, max(cy - 200, 200))

anchor_off = (cx - l, cy - t)
user32.SetCursorPos(cx, cy)
time.sleep(0.2)
user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
time.sleep(0.1)

samples = []
steps, interval = 40, 0.008        # 120Hz 步进
for i in range(1, steps + 1):
    mx = cx + (target[0] - cx) * i // steps
    my = cy + (target[1] - cy) * i // steps
    user32.SetCursorPos(mx, my)
    time.sleep(interval)
    wl, wt_, wr, wb = rect(hwnd)
    ideal_x, ideal_y = mx - anchor_off[0], my - anchor_off[1]
    samples.append((i, mx, my, wl, wt_, wl - ideal_x, wt_ - ideal_y))

user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
time.sleep(0.5)                    # 等 _settle

print(f"{'frame':>5} {'mouse':>13} {'window':>13} {'lag_xy':>11}")
for i, mx, my, wl, wty, dx, dy in samples[::5]:
    print(f"{i:>5} ({mx:>5},{my:>5}) ({wl:>5},{wty:>5}) ({dx:>4},{dy:>4})")
l2, t2, r2, b2 = rect(hwnd)
print(f"final: ({l2},{t2})-({r2},{b2})")
max_lag = max(max(abs(s[5]), abs(s[6])) for s in samples)
avg_lag = sum(abs(s[5]) + abs(s[6]) for s in samples) / (2 * len(samples))
print(f"max_frame_lag={max_lag}px  avg_frame_lag={avg_lag:.1f}px")
