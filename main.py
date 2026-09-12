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

    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.withdraw()   # 先不显示：单实例检查失败时直接退出，不闪一下主窗口

    lock = paths.InstanceLock(layout.lock_path)
    if not lock.acquire():
        messagebox.showinfo("EPUB Reader", "已经有一个 EPUB Reader 在运行。")
        root.destroy()
        return

    root.deiconify()
    app = MainWindow(root, layout)
    try:
        root.mainloop()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
