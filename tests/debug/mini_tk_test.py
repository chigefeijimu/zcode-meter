"""最小验证:合成鼠标事件能否驱动 Tk 的 B1-Motion。窗口显示事件计数。"""
import ctypes
import tkinter as tk

root = tk.Tk()
root.geometry("250x150+833+600")
root.overrideredirect(True)
root.attributes("-topmost", True)
root.configure(bg="navy")

log = []
n = {"press": 0, "motion": 0, "release": 0}
info = tk.Label(root, text="waiting...", fg="white", bg="navy", font=("Consolas", 10))
info.pack(expand=True)

with open(r"D:\codeSoft\zcode\.zcode\workspace\default\zcode-meter\mini_tk_events.log", "w") as LOG:

    def on_press(e):
        n["press"] += 1
        log.append(("press", e.x_root, e.y_root))

    def on_motion(e):
        n["motion"] += 1
        log.append(("motion", e.x_root, e.y_root))
        root.geometry(f"+{e.x_root - 125}+{e.y_root - 75}")

    def on_release(e):
        n["release"] += 1
        log.append(("release", e.x_root, e.y_root))
        LOG.write("\n".join(str(x) for x in log[-10:]) + f"\nTOTAL press={n['press']} motion={n['motion']} release={n['release']}\n")
        LOG.flush()

    for w in [root] + list(root.winfo_children()):
        w.bind("<Button-1>", on_press)
        w.bind("<B1-Motion>", on_motion)
        w.bind("<ButtonRelease-1>", on_release)

    root.after(12000, root.destroy)   # 12 秒后自动退出
    root.mainloop()
