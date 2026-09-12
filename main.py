"""入口。--portable：数据、缓存、导出全部放在程序所在目录（旧行为）。"""

import sys
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

import paths
from main_window import MainWindow


def main():
    portable = "--portable" in sys.argv[1:]
    layout = paths.resolve_layout(portable)

    # 单实例检查放在建 CTk 根窗口之前。CTk 在 Windows 上自己管理首次显示：
    # 建好后先 withdraw、mainloop 时再 deiconify；如果我们在那之前手动 withdraw 过，它就不再自动显示
    # —— 2026-09-12 打包版窗口「一闪就没了」就是这个原因。所以提示用一个临时的普通 Tk 窗口。
    lock = paths.InstanceLock(layout.lock_path)
    if not lock.acquire():
        tmp = tk.Tk()
        tmp.withdraw()
        messagebox.showinfo("EPUB Reader", "已经有一个 EPUB Reader 在运行。")
        tmp.destroy()
        return

    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    app = MainWindow(root, layout)
    try:
        root.mainloop()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
