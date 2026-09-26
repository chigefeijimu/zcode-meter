"""驱动 mini_tk_test 窗口:合成 120Hz 拖动,然后读事件日志。"""
import ctypes
import time

user32 = ctypes.windll.user32
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x2, 0x4

# mini 窗口在 (833,600)-(1083,750),中心 (958,675) —— 直接用同逻辑坐标
start = (958, 675)
target = (1158, 775)

user32.SetCursorPos(*start)
time.sleep(0.2)
user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
time.sleep(0.1)
for i in range(1, 31):
    mx = start[0] + (target[0] - start[0]) * i // 30
    my = start[1] + (target[1] - start[1]) * i // 30
    user32.SetCursorPos(mx, my)
    time.sleep(0.008)
user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
time.sleep(0.5)
print("drag injected")
