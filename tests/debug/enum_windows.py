"""枚举 zcode-meter 进程的全部顶层窗口与状态,看 histRoot 是否存在及在何处。"""
import ctypes
import ctypes.wintypes as wt
import sys

user32 = ctypes.windll.user32
TARGET_PID = int(sys.argv[1])
found = []

@ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
def cb(hwnd, _):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == TARGET_PID:
        rc = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        vis = bool(user32.IsWindowVisible(hwnd))
        iconic = bool(user32.IsIconic(hwnd))
        txt = ctypes.create_unicode_buffer(128)
        user32.GetWindowTextW(hwnd, txt, 128)
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        found.append((hwnd, txt.value, cls.value, vis, iconic,
                      (rc.left, rc.top, rc.right, rc.bottom)))
    return True

user32.EnumWindows(cb, 0)
print(f"pid {TARGET_PID}: {len(found)} top-level windows")
for hwnd, txt, cls, vis, iconic, rect in found:
    print(f"  hwnd={hwnd} class={cls!r:<22} title={txt!r:<30} visible={vis} minimized={iconic} rect={rect}")
