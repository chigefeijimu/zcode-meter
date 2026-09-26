"""找到 zcode-meter 进程的窗口并打印位置,若完全在虚拟屏幕外则拉回主屏。"""
import ctypes
import ctypes.wintypes as wt
import sys

user32 = ctypes.windll.user32
TARGET_PID = int(sys.argv[1])

found = []

@ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
def on_window(hwnd, lparam):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == TARGET_PID and user32.IsWindowVisible(hwnd):
        rc = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        found.append((hwnd, rc.left, rc.top, rc.right, rc.bottom))
    return True

user32.EnumWindows(on_window, 0)

class MASK(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]

vs = MASK()
user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
vs.left = user32.GetSystemMetrics(76)
vs.top = user32.GetSystemMetrics(77)
vs.right = vs.left + user32.GetSystemMetrics(78)
vs.bottom = vs.top + user32.GetSystemMetrics(79)
print(f"virtual screen: ({vs.left},{vs.top})-({vs.right},{vs.bottom})")

if not found:
    print("no visible window for pid", TARGET_PID)
    sys.exit(1)

for hwnd, l, t, r, b in found:
    outside = (r <= vs.left or b <= vs.top or l >= vs.right or t >= vs.bottom)
    print(f"window {hwnd}: ({l},{t})-({r},{b})  fully_outside={outside}")
    if outside:
        user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 200, 200, 0, 0, 0x1 | 0x4 | 0x10)
        print("  -> moved back to (200,200)")
