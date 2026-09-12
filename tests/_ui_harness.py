"""UI 测试共用的脚手架：假 tts_thread、生成的小 EPUB、吞掉 messagebox、按需新建根窗口。
被 t_notes_bookmarks.py / t_ai_dialog.py import。**必须在 import main_window 之前 import 它。**"""

import json
import os
import shutil
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

root_dir = tempfile.mkdtemp(prefix="epubreader-ui-")
os.environ["LOCALAPPDATA"] = os.path.join(root_dir, "LocalAppData")
os.environ["USERPROFILE"] = os.path.join(root_dir, "Profile")

# ---- 假 tts_thread ----
fake_tts = types.ModuleType("tts_thread")
fake_tts.NEURAL_VOICES = ["zh-CN-YunyangNeural"]


class FakeTTS:
    def __init__(self, events, cache_dir=None, downloader=None):
        self.events = events
        self.plays = []
        self.stops = 0
        self.next_id = 1
        self.settings = None
        events.put(("ready", [("sapi-1", "SAPI One")]))

    def set_settings(self, voice_id, rate, volume=None):
        self.settings = (voice_id, rate, volume)

    def play(self, sentences, start_idx=0, target="epub", end_idx=None):
        sid = self.next_id
        self.next_id += 1
        self.plays.append((sid, target, sentences, start_idx))
        return sid

    def stop(self):
        self.stops += 1

    def quit(self):
        pass


fake_tts.TTSWorker = FakeTTS
sys.modules["tts_thread"] = fake_tts

import tkinter as tk
from tkinter import messagebox
from ebooklib import epub

popups = []
for _name in ("showinfo", "showwarning", "showerror"):
    setattr(messagebox, _name, lambda title, msg, _n=_name, **kw: popups.append((_n, title, msg)))
yes_answers = {"default": False}
messagebox.askyesno = lambda title, msg, **kw: (popups.append(("askyesno", title, msg)), yes_answers["default"])[1]


def make_epub(path, title, n_chapters):
    book = epub.EpubBook()
    book.set_identifier(title)
    book.set_title(title)
    book.set_language("zh")
    chapters = []
    for i in range(n_chapters):
        c = epub.EpubHtml(title=f"第{i + 1}章 标题{i + 1}", file_name=f"ch{i + 1}.xhtml", lang="zh")
        paras = "".join(f"<p>第{i + 1}章第{j + 1}段。这是一句话！还有一句？</p>" for j in range(4))
        c.content = f"<html><body><h1>第{i + 1}章 标题{i + 1}</h1>{paras}</body></html>"
        book.add_item(c)
        chapters.append(c)
    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + chapters
    epub.write_epub(path, book)


book1 = os.path.join(root_dir, "书一.epub")
book2 = os.path.join(root_dir, "书二.epub")
make_epub(book1, "书一", 4)
make_epub(book2, "书二", 3)

import customtkinter as ctk

import paths
from main_window import MainWindow

_state = {"root": None, "app": None}


def read_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def new_app(v1_data=None, v2_data=None):
    layout = paths.resolve_layout()
    os.makedirs(layout.data, exist_ok=True)
    for f in os.listdir(layout.data):
        fp = os.path.join(layout.data, f)
        if os.path.isdir(fp):
            shutil.rmtree(fp, ignore_errors=True)
        else:
            os.remove(fp)
    with open(os.path.join(layout.data, paths.MIGRATION_MARKER), "w") as f:
        f.write("{}")
    if v1_data is not None or v2_data is not None:
        with open(layout.bookmarks_path, "w", encoding="utf-8") as f:
            json.dump(v1_data if v1_data is not None else v2_data, f, ensure_ascii=False)
    if _state["app"] is not None:
        _state["app"]._closed = True          # 停掉上一个实例的事件定时器
        _state["root"].destroy()               # CustomTkinter 控件不能在旧根上重建
    _state["root"] = ctk.CTk()     # CTkToplevel 的 DPI 检查要求根是 CTk
    _state["root"].withdraw()
    app = MainWindow(_state["root"], layout)
    _state["root"].update()
    _state["app"] = app
    return app, layout


def current_root():
    return _state["root"]


def cleanup():
    try:
        if _state["root"] is not None:
            _state["root"].destroy()
    except Exception:
        pass
    shutil.rmtree(root_dir, ignore_errors=True)
