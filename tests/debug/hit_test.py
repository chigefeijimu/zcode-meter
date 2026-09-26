"""诊断:鼠标位置上系统 hit-test 到的到底是哪个窗口。"""
import ctypes
import ctypes.wintypes as wt
import sys

user32 = ctypes.windll.user32

pid = int(sys.argv[1])
target_hwnd = None

@ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
def cb(hwnd, _):
    global target_hwnd
    p = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
    if p.value == pid and user32.IsWindowVisible(hwnd):
        target_hwnd = hwnd
    return True

user32.EnumWindows(cb, 0)
print(f"target hwnd: {target_hwnd}  enabled={bool(user32.IsWindowEnabled(target_hwnd))}")

rc = wt.RECT()
user32.GetWindowRect(ctypes.c_void_p(target_hwnd), ctypes.byref(rc))
print(f"target rect(logical): ({rc.left},{rc.top})-({rc.right},{rc.bottom})")

cx, cy = (rc.left + rc.right) // 2, (rc.top + rc.bottom) // 2
pt = wt.POINT(cx, cy)

# WindowFromPoint 用物理像素;本进程非 DPI aware,逻辑->物理换算
import ctypes.wintypes as w2
user32.SetProcessDPIAware()   # 让本进程也用物理像素,与 GetWindowRect 原生对齐
# SetProcessDPIAware 需在早期调用,这里重新读 rect(现在应为物理)
user32.GetWindowRect(ctypes.c_void_p(target_hwnd), ctypes.byref(rc))
print(f"target rect(physical): ({rc.left},{rc.top})-({rc.right},{rc.bottom})")
pt = wt.POINT((rc.left + rc.right) // 2, (rc.top + rc.bottom) // 2)
print(f"probe point: ({pt.x},{pt.y})")

hit = user32.WindowFromPoint(pt)
cls = ctypes.create_unicode_buffer(64)
user32.GetClassNameW(ctypes.c_void_p(hit), cls, 64)
hit_pid = wt.DWORD()
user32.GetWindowThreadProcessId(ctypes.c_void_p(hit), ctypes.byref(hit_pid))
print(f"WindowFromPoint -> hwnd={hit} class={cls.value!r} pid={hit_pid.value}")
print(f"is target: {hit == target_hwnd}")
