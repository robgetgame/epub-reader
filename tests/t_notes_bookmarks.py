"""第 3 步验收：笔记身份（B1）、书签偏移恢复（B4）、播放不改文本（A.2）、v1 迁移（D18）、
保存失败不切章（A.1）、快捷键在笔记区不生效（B3）。

真 Tk（窗口 withdraw，不显示）、假 TTSWorker、临时目录里生成的小 EPUB。不碰真实数据。
"""

import json
import os
import shutil
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

root_dir = tempfile.mkdtemp(prefix="epubreader-notes-")
os.environ["LOCALAPPDATA"] = os.path.join(root_dir, "LocalAppData")
os.environ["USERPROFILE"] = os.path.join(root_dir, "Profile")

# ---- 假 tts_thread：不碰 COM、不碰 pygame ----
fake_tts = types.ModuleType("tts_thread")
fake_tts.NEURAL_VOICES = ["zh-CN-YunyangNeural"]


class FakeTTS:
    """第 4 步接口：events 队列、play 返回 session id、set_settings。"""

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
import customtkinter as ctk

from ebooklib import epub

import paths
import main_window
from main_window import MainWindow, parse_note_text
from epub_parser import EpubParser
from config_manager import ConfigManager

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


def read_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


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

# 弹窗一律吞掉（记录下来），不然测试会卡在 messagebox
popups = []
for name in ("showinfo", "showwarning", "showerror"):
    setattr(messagebox, name, lambda title, msg, _n=name, **kw: popups.append((_n, title, msg)))
messagebox.askyesno = lambda title, msg, **kw: popups.append(("askyesno", title, msg)) or False

tk_root = ctk.CTk()
tk_root.withdraw()


def new_app(v1_data=None, v2_data=None):
    layout = paths.resolve_layout()
    os.makedirs(layout.data, exist_ok=True)
    p = layout.bookmarks_path
    for f in os.listdir(layout.data):
        fp = os.path.join(layout.data, f)
        if os.path.isdir(fp):
            shutil.rmtree(fp, ignore_errors=True)
        else:
            os.remove(fp)
    with open(os.path.join(layout.data, paths.MIGRATION_MARKER), "w") as f:
        f.write("{}")
    if v1_data is not None or v2_data is not None:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(v1_data if v1_data is not None else v2_data, f, ensure_ascii=False)
    global tk_root
    if new_app.prev is not None:
        new_app.prev._closed = True      # 停掉上一个实例的事件定时器
        tk_root.destroy()                 # CustomTkinter 控件不能在旧根上重建，换一个根
        tk_root = ctk.CTk()
        tk_root.withdraw()
    app = MainWindow(tk_root, layout)
    new_app.prev = app
    tk_root.update()
    return app, layout


new_app.prev = None

# ---------- 1. 解析器：章 = 目录条目 ----------
print("1. 解析器")
pr = EpubParser()
check(pr.load_epub(book1), "打开生成的 epub")
check([c.title for c in pr.chapters] == [f"第{i}章 标题{i}" for i in range(1, 5)], f"标题来自目录、nav 文档不算章：{[c.title for c in pr.chapters]}")
text, ss = pr.get_chapter_sentences(0)
check(len(ss) >= 12 and all(text[s.start:s.end] == s.text for s in ss), f"句子偏移与原文一致（{len(ss)} 句）")

# ---------- 2. 首次开书：第 0 章已有笔记不被清空（B1-A） ----------
print("2. 首开第 0 章")
v2 = {"schema": 2, "last_file": book1, "recent_files": [book1],
      "notes": {book1: {"0": "第零章原有笔记"}}, "bookmarks": {book1: {"chapter": 0, "offset": 0}}}
app, layout = new_app(v2_data=v2)
check(app.displayed_note == (book1, "0"), f"显示的是 (书一, 0)：{app.displayed_note}")
check(app._note_text() == "第零章原有笔记", "右栏是原有笔记")
check(read_json(layout.bookmarks_path)["notes"][book1]["0"] == "第零章原有笔记", "磁盘上没被清空")

# ---------- 3. A→B→A 不串；编辑后切章保存到正确的章 ----------
print("3. A→B→A")
app.sandbox_area.insert(tk.END, "，加了一句")
tk_root.update()
check(app.note_dirty, "编辑后 dirty")
rev_before = app.note_revisions.get((book1, "0"))
check(rev_before and rev_before >= 1, f"revision 递增：{rev_before}")
check(app._load_chapter(1), "切到第 1 章")
check(app.displayed_note == (book1, "1") and app._note_text() == "", "第 1 章笔记为空")
d = read_json(layout.bookmarks_path)
check(d["notes"][book1]["0"] == "第零章原有笔记，加了一句", "第 0 章笔记存对了")
check("1" not in d["notes"][book1], "第 1 章没有被写入空串")
app.sandbox_area.insert(tk.END, "第一章笔记")
tk_root.update()
app._load_chapter(0)
check(app._note_text() == "第零章原有笔记，加了一句", "切回第 0 章内容正确")
check(read_json(layout.bookmarks_path)["notes"][book1]["1"] == "第一章笔记", "第 1 章笔记存到第 1 章")

# ---------- 4. 换书：旧书笔记不进新书（B1-C） ----------
print("4. 换书")
app.sandbox_area.insert(tk.END, "（换书前又改）")
tk_root.update()
app._load_epub(book2)
check(app.book_key == book2 and app.displayed_note == (book2, "0"), "新书第 0 章")
d = read_json(layout.bookmarks_path)
check(d["notes"][book1]["0"].endswith("（换书前又改）"), "旧书第 0 章笔记存到旧书")
check(book2 not in d["notes"], "新书没有任何笔记被写入")
check(app._note_text() == "", "右栏为空")

# ---------- 5. 书签：偏移恢复 + 回写（B4） ----------
print("5. 书签")
app._load_chapter(2)
target = app.current_sentences[5]
app.config.set_bookmark(book2, 2, target.start + 1)   # 落在第 5 句中间
app._load_epub(book2)
check(app.current_chapter_idx == 2 and app.current_sentence_idx == 5, f"恢复到第 2 章第 5 句：{app.current_chapter_idx}/{app.current_sentence_idx}")
check(read_json(layout.bookmarks_path)["bookmarks"][book2] == {"chapter": 2, "offset": target.start}, "书签回写为该句起点")
check("highlight" in app.text_area.tag_names(f"sentence_5.first"), "恢复位置有高亮")
check(app.text_area.get("1.0", "end-1c") == app.chapter_text, "正文按原样渲染，没有重排")

# ---------- 6. 播放笔记不改文本、不改 revision（A.2） ----------
print("6. 播放笔记不改文本")
note = "{xiaoxiao}第一句。{/xiaoxiao}\n\n第二句！  第三句？"
app._mutate_note(note, source="test")
tk_root.update()
rev = app.note_revisions[app.displayed_note]
app._start_play_sandbox()
tk_root.update()
check(app._note_text() == note, "文本一字未动")
check(app.note_revisions[app.displayed_note] == rev, "revision 没变")
check(app.is_playing_sandbox and app.tts.plays, "开始播放")
sent = app.tts.plays[-1][2]
check([s["text"] for s in sent] == ["第一句。", "第二句！", "第三句？"], f"句子：{[s['text'] for s in sent]}")
check(sent[0]["voice_id"] == "zh-CN-XiaoxiaoNeural" and sent[1]["voice_id"] is None, "音色标签解析")
check(app.sandbox_area.get("sentence_1.first", "sentence_1.last") == "第二句！", "标签打在原文正确位置")
app.events.put(("highlight", app.active_session, 1)); app._drain_events()
check("highlight" in app.sandbox_area.tag_names("sentence_1.first"), "高亮第 1 句")
# 播放中编辑 → 立即停
app.sandbox_area.insert(tk.END, "x")
tk_root.update()
check(not app.is_playing_sandbox, "播放中编辑 → 停止")
check(app.note_revisions[app.displayed_note] == rev + 1, "编辑后 revision +1")
app._mutate_note(note, source="test")

# ---------- 7. 笔记位置按 NoteId 存 ----------
print("7. 笔记位置")
app._start_play_sandbox()
app.events.put(("highlight", app.active_session, 2)); app._drain_events()
app._stop_play_sandbox()
check(app.config.get_note_position(*app.displayed_note) == sent[2]["start"], "停下时存位置")
app._start_play_sandbox()
check(app.tts.plays[-1][3] == 2, "再播从第 2 句开始")
app._stop_play_sandbox()

# ---------- 8. 保存失败 → 不切章、不换书、内容保留 ----------
print("8. 保存失败")
app.sandbox_area.insert(tk.END, "未保存")
tk_root.update()
real_save = app.config.store.save
app.config.store.save = lambda: (setattr(app.config.store, "last_error", "模拟失败") or False)
popups.clear()
ok = app._load_chapter(0)
check(ok is False and app.displayed_note == (book2, "2"), "切章被拒，displayed_note 不变")
check(app._note_text().endswith("未保存") and app.note_dirty, "内容和 dirty 都在")
check(popups and popups[-1][0] == "showerror", "弹了错误")
app._load_epub(book1)
check(app.book_key == book2, "换书也被拒")
app.config.store.save = real_save
check(app._load_chapter(0) is True, "恢复后能切")
check(read_json(layout.bookmarks_path)["notes"][book2]["2"].endswith("未保存"), "之前的编辑最终存进了正确的章")

# ---------- 9. 快捷键在笔记区不生效（B3） ----------
print("9. 快捷键")
# 窗口 withdraw 时 Tk 不分配焦点，直接假装焦点在哪
app.root.focus_get = lambda: app.sandbox_area
before = len(app.tts.plays)
r = app._key_toggle_play(None)
check(r is None and len(app.tts.plays) == before and not app.is_playing, "焦点在笔记区：空格不播放")
ch = app.current_chapter_idx
app._key_nav(+1)
check(app.current_chapter_idx == ch, "焦点在笔记区：方向键不翻章")
app.root.focus_get = lambda: app.text_area
app._key_nav(+1)
check(app.current_chapter_idx == ch + 1, "焦点在正文区：方向键翻章")

# ---------- 10. v1 迁移：原样保留，笔记不自动归章 ----------
print("10. v1 迁移")
v1 = {"recent_files": [book1], "last_file": book1, "voice_id": "zh-CN-YunyangNeural", "speech_rate": 271,
      "bookmarks": {book1: {"chapter_idx": 2, "sentence_idx": 7}},
      "sandbox_text": "旧草稿", "sandbox_sentence_idx": 3,
      "chapter_notes": {book1: {"0": "", "2": "旧第2文件笔记"}}}
app, layout = new_app(v1_data=v1)
d = read_json(layout.bookmarks_path)
check(d["schema"] == 2 and app.config.migrated_now, "升级到 schema 2")
check(d["migrated_v1"]["chapter_notes"] == v1["chapter_notes"] and d["migrated_v1"]["sandbox_text"] == "旧草稿"
      and d["migrated_v1"]["bookmarks"] == v1["bookmarks"], "v1 四个字段原样在 migrated_v1")
check(d["notes"] == {}, "不自动换算：新 notes 为空")
check(d["bookmarks"] == {book1: {"chapter": 0, "offset": 0}}, f"旧书签不换算，开书后从第 0 章重新记：{d['bookmarks']}")
check(d["speech_rate"] == 271 and d["voice_id"] == "zh-CN-YunyangNeural" and d["last_file"] == book1, "音色语速最近文件保留")
v1_copies = [f for f in os.listdir(layout.data) if f.startswith("bookmarks.v1-")]
check(len(v1_copies) == 1 and read_json(os.path.join(layout.data, v1_copies[0])) == v1, f"v1 原件另存：{v1_copies}")
check(app.book_key == book1 and app.current_chapter_idx == 0, "旧书重新打开、从第 0 章开始（书签手动重定位）")
check(any("升级" in m for _, _, m in popups), "提示了升级")
# 旧笔记对话框能打开、追加到当前章走 _mutate_note
app._show_legacy_notes()
tk_root.update()
check(app.config.legacy()["chapter_notes"][book1]["2"] == "旧第2文件笔记", "legacy() 可读")

# ---------- 11. 没开书：草稿身份 ----------
print("11. 草稿")
app, layout = new_app(v2_data={"schema": 2})
check(app.displayed_note == ("__scratch__", "0"), "没开书时显示草稿")
app.sandbox_area.insert(tk.END, "草稿内容")
tk_root.update()
app._load_epub(book1)
check(read_json(layout.bookmarks_path)["notes"]["__scratch__"]["0"] == "草稿内容", "草稿存到 __scratch__")
check(app._note_text() == "", "开书后右栏是该章笔记（空）")

# ---------- 12. parse_note_text 边界 ----------
print("12. parse_note_text")
r = parse_note_text("{foo}不认识的标签。{/foo}{yunxi}男声。{/yunxi}尾巴", EpubParser.split_into_sentences)
check([x["text"] for x in r] == ["不认识的标签。", "男声。", "尾巴"] and r[0]["voice_id"] is None
      and r[1]["voice_id"] == "zh-CN-YunxiNeural" and r[2]["voice_id"] is None, "不认识的标签不改音色、不出现在句子里")
check(parse_note_text("", EpubParser.split_into_sentences) == [], "空文本")

# ---------- 13. 选区播放（D13）：只读选区、读完停、不翻页、下次从选区末尾继续 ----------
print("13. 选区播放")
app._load_epub(book1)
app._load_chapter(1)
ss = app.current_sentences
# 选区从第 2 句中间到第 3 句中间
app.text_area.config(state=tk.NORMAL)
app.text_area.tag_add("sel", f"1.0 + {ss[2].start + 2} chars", f"1.0 + {ss[3].start + 2} chars")
app.text_area.config(state=tk.DISABLED)
app._start_play()
sid, target, sent, start = app.tts.plays[-1]
check(start == 2 and app.selection_end == 3, f"从第 2 句读到第 3 句：start={start} end={app.selection_end}")
app.events.put(("done", sid, "finished", [])); app._drain_events()
check(not app.is_playing and app.current_sentence_idx == 4, f"读完停在选区之后（第 4 句）：{app.current_sentence_idx}")
check(app._auto_after_id is None, "没有排自动翻页")
check(read_json(layout.bookmarks_path)["bookmarks"][book1]["offset"] == ss[4].start, "书签在第 4 句起点")
app._start_play()
check(app.tts.plays[-1][3] == 4 and app.selection_end is None, "再按播放：从第 4 句读到章末（无选区）")
app.events.put(("done", app.tts.plays[-1][0], "finished", [])); app._drain_events()
check(app._auto_after_id is not None, "整章读完才排自动翻页")
app._cancel_auto_next()
# 选区落在句子之间的空白 / 选区只在一句内
app.text_area.tag_add("sel", f"1.0 + {ss[1].start} chars", f"1.0 + {ss[1].end} chars")
check(app._selected_range(app.text_area, ss) == (1, 1), "整句选区 → (1, 1)")
app.text_area.tag_add("sel", f"1.0 + {ss[0].end} chars", f"1.0 + {ss[1].start} chars")
check(app._selected_range(app.text_area, ss) is None, "只选到句间空白 → None")

tk_root.destroy()
shutil.rmtree(root_dir, ignore_errors=True)
print()
if FAILS:
    print(f"FAILED {len(FAILS)}")
    sys.exit(1)
print("ALL OK")
