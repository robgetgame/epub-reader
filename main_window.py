"""主窗口。第 3 步（2026-09-12）重写了状态模型，界面还是旧的 ttk，第 6.5 步换 CustomTkinter。

三个身份（设计文档 §2）：
- 笔记身份 displayed_note = (book_key, chapter_key)：右栏现在显示的是谁的笔记。
  保存只保存到它，切换只经过 _show_note()，修改只经过 _mutate_note() / 用户敲键盘。
  没开书时 book_key = "__scratch__"。这一条解决 B1 的三种串笔记。
- 播放 session：第 4 步做。现在还是共享 stop_event 的旧 TTSWorker。
- AI 请求身份：第 8 步做。

书签存 (章序号, 字符偏移)，恢复时高亮并回写（B4）。
正文和笔记都**按原文渲染**，句子用偏移打 tag，不再重排成一句一段（A.2）。
"""

import os
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import paths
from config_manager import ConfigManager, SCRATCH_BOOK
from epub_parser import EpubParser, sentence_index_at
from mp3_exporter import run_export_background
from tts_thread import TTSWorker, NEURAL_VOICES

# 笔记里的多音色标签 {yunxi}…{/yunxi}。README 和 AGENTS.md 里公开的六个。
TAG_TO_VOICE = {
    'yunxi': 'zh-CN-YunxiNeural',
    'xiaoxiao': 'zh-CN-XiaoxiaoNeural',
    'yunyang': 'zh-CN-YunyangNeural',
    'guy': 'en-US-GuyNeural',
    'aria': 'en-US-AriaNeural',
    'jenny': 'en-US-JennyNeural',
}
_TAG_RE = re.compile(r'\{(/?)([a-zA-Z]+)\}')


def parse_note_text(raw_text, split):
    """把笔记文本切成带偏移、带音色的句子列表。标签本身不进列表（不朗读、不高亮）。
    voice_id 为 None 表示用全局音色。split 是 EpubParser.split_into_sentences。"""
    out = []
    active = None
    pos = 0

    def take(seg_start, seg_end):
        for s in split(raw_text[seg_start:seg_end]):
            out.append({"text": s.text, "voice_id": active,
                        "start": seg_start + s.start, "end": seg_start + s.end})

    for m in _TAG_RE.finditer(raw_text):
        take(pos, m.start())
        closing, name = m.group(1), m.group(2).lower()
        if closing:
            active = None
        elif name in TAG_TO_VOICE:
            active = TAG_TO_VOICE[name]
        # 不认识的标签：不发音、不改音色，跟旧版一致
        pos = m.end()
    take(pos, len(raw_text))
    return out


class MainWindow:
    def __init__(self, root, layout=None):
        self.root = root
        # layout 为 None 只在旧测试脚本里出现：退回 portable（程序目录）
        self.layout = layout or paths.resolve_layout(portable=True)
        self.root.title("Native EPUB TTS Reader")
        self.root.geometry("1400x800")

        self.bg_color = "#1e1e1e"
        self.fg_color = "#d4d4d4"
        self.highlight_bg = "#b8860b"
        self.highlight_fg = "#ffffff"
        self.root.configure(bg=self.bg_color)

        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background=self.bg_color)
        style.configure('TLabel', background=self.bg_color, foreground=self.fg_color)
        style.configure('TButton', background="#333333", foreground=self.fg_color, borderwidth=1)
        style.map('TButton', background=[('active', '#555555')])

        # 先把旧位置的数据搬进数据目录（只复制不删），再读。搬运结果要告诉使用者。
        self.startup_notes = list(self.layout.notes)
        if self.layout.config_problem:
            self.startup_notes.append(self.layout.config_problem)
        self.startup_notes += paths.migrate_legacy(self.layout)
        paths.migrate_cache_background(os.path.join(paths.app_root(), "temp_audio"), self.layout.cache)

        self.config = ConfigManager(self.layout.bookmarks_path, read_only=not self.layout.data_ok)
        if self.config.migrated_now:
            kept = (f"旧文件另存为 {os.path.basename(self.config.v1_backup)}。" if self.config.v1_backup
                    else "旧文件没能另存一份（复制失败），但内容已在新文件的 migrated_v1 里。")
            self.startup_notes.append("数据文件已升级到新格式。旧版的书签和笔记原样保留在「文件 → 旧笔记」里，"
                                      "可以从那里复制到当前章。" + kept)
        self.parser = EpubParser()

        # EPUB 状态
        self.book_key = None            # 已打开的 epub 路径；None = 没开书
        self.current_chapter_idx = 0
        self.chapter_text = ""
        self.current_sentences = []     # list[Sentence]
        self.current_sentence_idx = 0
        self.is_playing = False

        # 笔记状态（§2.1）
        self.displayed_note = None      # (book_key, chapter_key) | None
        self.note_dirty = False
        self.note_revisions = {}        # NoteId -> int
        self._suppress_modified = False
        self.note_sentences = []        # parse_note_text 的结果
        self.note_sentence_idx = 0
        self.is_playing_sandbox = False

        self.tts = TTSWorker(
            highlight_callback=lambda idx: self.root.after(0, self._highlight_sentence, idx),
            chapter_done_callback=lambda: self.root.after(0, self._auto_next_chapter),
            cache_dir=self.layout.cache,
        )

        self._build_ui()
        self._setup_keybinds()
        self._load_voices()

        # 数据目录不可用 / 迁移发生了什么 / 数据文件读的时候出过事 —— 全部说出来，不静默
        if self.layout.data_problem:
            messagebox.showwarning("数据目录", self.layout.data_problem)
        if self.config.load_error:
            messagebox.showwarning("数据文件", self.config.load_error)
        if self.startup_notes:
            messagebox.showinfo("数据迁移", chr(10).join(self.startup_notes))

        # 没开书时右栏显示草稿；开了书 _load_chapter 会切到该章笔记
        self._show_note((SCRATCH_BOOK, "0"))
        self._reload_last_session()

    # ---------- UI ----------

    def _build_ui(self):
        menubar = tk.Menu(self.root, bg=self.bg_color, fg=self.fg_color)
        file_menu = tk.Menu(menubar, tearoff=0, bg=self.bg_color, fg=self.fg_color)
        file_menu.add_command(label="Open EPUB...", command=self._open_file_dialog)
        self.recent_menu = tk.Menu(file_menu, tearoff=0, bg=self.bg_color, fg=self.fg_color)
        file_menu.add_cascade(label="Recent Files", menu=self.recent_menu)
        self._update_recent_menu()
        file_menu.add_separator()
        file_menu.add_command(label="旧笔记（升级前的数据）...", command=self._show_legacy_notes)
        file_menu.add_command(label="导入旧数据文件...", command=self._import_legacy_dialog)
        file_menu.add_command(label="打开数据目录", command=lambda: paths.open_in_explorer(self.layout.data))
        file_menu.add_command(label="打开导出目录", command=lambda: paths.open_in_explorer(self.layout.export))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)
        self.root.config(menu=menubar)

        paned = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg=self.bg_color, bd=0, sashwidth=4)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 左：目录
        left_frame = ttk.Frame(paned)
        paned.add(left_frame, minsize=200)
        ttk.Label(left_frame, text="Contents", font=("Arial", 12, "bold")).pack(anchor=tk.W, pady=5)
        self.toc_listbox = tk.Listbox(left_frame, bg="#2d2d2d", fg=self.fg_color,
                                      selectbackground=self.highlight_bg, selectforeground=self.highlight_fg,
                                      borderwidth=0, highlightthickness=0,
                                      font=("Arial", 10), exportselection=False)
        self.toc_listbox.pack(fill=tk.BOTH, expand=True)
        self.toc_listbox.bind("<<ListboxSelect>>", self._on_toc_select)

        # 中：正文
        mid_frame = ttk.Frame(paned)
        paned.add(mid_frame, minsize=400)
        ttk.Label(mid_frame, text="EPUB Reader", font=("Arial", 12, "bold")).pack(anchor=tk.W, pady=5)
        # spacing3：段落之间留空。原来是靠把每句后面塞两个换行，现在正文按原样渲染，用样式留空
        self.text_area = tk.Text(mid_frame, bg=self.bg_color, fg=self.fg_color,
                                 font=("Microsoft YaHei", 12), wrap=tk.WORD,
                                 padx=20, pady=20, borderwidth=0, highlightthickness=0, spacing3=10)
        self.text_area.pack(fill=tk.BOTH, expand=True)
        self.text_area.tag_configure("highlight", background=self.highlight_bg, foreground=self.highlight_fg)
        self.text_area.config(state=tk.DISABLED)   # 只读靠 state，不再拦 <Key>（那会把 Ctrl+C 也拦掉）

        epub_controls = ttk.Frame(mid_frame)
        epub_controls.pack(fill=tk.X, pady=10)
        ttk.Button(epub_controls, text="<< Prev", command=self._prev_chapter).pack(side=tk.LEFT, padx=2)
        self.play_btn = ttk.Button(epub_controls, text="Play", command=self._toggle_play)
        self.play_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(epub_controls, text="Next >>", command=self._next_chapter).pack(side=tk.LEFT, padx=2)
        ttk.Button(epub_controls, text="Export MP3", command=self._export_mp3_ui).pack(side=tk.LEFT, padx=15)

        # 右：本章笔记
        right_frame = ttk.Frame(paned)
        paned.add(right_frame, minsize=350)
        title_frame = ttk.Frame(right_frame)
        title_frame.pack(fill=tk.X, pady=5)
        self.note_title = ttk.Label(title_frame, text="Chapter Note", font=("Arial", 12, "bold"))
        self.note_title.pack(side=tk.LEFT)

        self.sandbox_area = tk.Text(right_frame, bg="#252526", fg=self.fg_color,
                                    font=("Microsoft YaHei", 12), wrap=tk.WORD,
                                    padx=20, pady=20, borderwidth=0, highlightthickness=0, undo=True)
        self.sandbox_area.pack(fill=tk.BOTH, expand=True)
        self.sandbox_area.tag_configure("highlight", background=self.highlight_bg, foreground=self.highlight_fg)
        self.sandbox_area.bind("<<Modified>>", self._on_note_modified)
        self.sandbox_area.bind("<FocusOut>", lambda e: self._save_displayed_note())

        sandbox_controls = ttk.Frame(right_frame)
        sandbox_controls.pack(fill=tk.X, pady=10)
        self.sandbox_play_btn = ttk.Button(sandbox_controls, text="Play Note", command=self._toggle_sandbox_play)
        self.sandbox_play_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(sandbox_controls, text="Clear", command=self._clear_sandbox).pack(side=tk.RIGHT, padx=2)

        # 底：设置 + 状态栏
        settings_frame = ttk.Frame(self.root)
        settings_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(settings_frame, text="Voice:").pack(side=tk.LEFT, padx=5)
        self.voice_var = tk.StringVar()
        self.voice_combo = ttk.Combobox(settings_frame, textvariable=self.voice_var, state="readonly", width=30)
        self.voice_combo.pack(side=tk.LEFT, padx=5)
        self.voice_combo.bind("<<ComboboxSelected>>", self._on_settings_change)
        ttk.Label(settings_frame, text="Speed:").pack(side=tk.LEFT, padx=5)
        self.speed_var = tk.IntVar(value=self.config.config.get("speech_rate", 200))
        self.speed_slider = ttk.Scale(settings_frame, from_=50, to=400, variable=self.speed_var)
        self.speed_slider.pack(side=tk.LEFT, padx=5)
        # 松手才存盘；拖动过程中每一格都写盘是 B9 里那条
        self.speed_slider.bind("<ButtonRelease-1>", self._on_settings_change)

        self.status_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.status_var, anchor=tk.W).pack(fill=tk.X, padx=10, pady=(0, 4))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_status(self, text):
        self.status_var.set(text)

    def _setup_keybinds(self):
        self.root.bind("<space>", self._key_toggle_play)
        self.root.bind("<Left>", lambda e: self._key_nav(-1))
        self.root.bind("<Right>", lambda e: self._key_nav(+1))

    def _focus_is_editable(self):
        """B3：焦点在可编辑控件（笔记区、下拉框）上时，空格和方向键是在打字，不是快捷键。"""
        w = self.root.focus_get()
        if w is None:
            return False
        if w is self.sandbox_area or isinstance(w, (tk.Entry, ttk.Entry, ttk.Combobox)):
            return True
        return isinstance(w, tk.Text) and str(w.cget("state")) == "normal"

    def _key_toggle_play(self, event):
        if self._focus_is_editable():
            return None
        self._toggle_play()
        return "break"

    def _key_nav(self, delta):
        if self._focus_is_editable():
            return None
        if delta < 0:
            self._prev_chapter()
        else:
            self._next_chapter()
        return "break"

    def _update_recent_menu(self):
        self.recent_menu.delete(0, tk.END)
        for path in self.config.config.get("recent_files", []):
            self.recent_menu.add_command(label=os.path.basename(path), command=lambda p=path: self._load_epub(p))

    def _load_voices(self):
        voices = self.tts.get_voices()
        self.voice_map = {name: vid for vid, name in voices}
        self.voice_combo['values'] = list(self.voice_map.keys())
        saved_voice = self.config.config.get("voice_id")
        selected_name = None
        if saved_voice:
            for name, vid in self.voice_map.items():
                if vid == saved_voice:
                    selected_name = name
                    break
        if selected_name:
            self.voice_combo.set(selected_name)
        elif voices:
            self.voice_combo.set(voices[0][1])

    def _on_settings_change(self, event=None):
        voice_id = self.voice_map.get(self.voice_var.get())
        rate = int(float(self.speed_var.get()))
        self.config.set_voice_and_rate(voice_id, rate)
        self.tts.set_rate(rate)

    def _current_voice_and_rate(self):
        return self.voice_map.get(self.voice_var.get()), int(float(self.speed_var.get()))

    # ---------- 渲染 ----------

    def _render_sentences(self, widget, text, sentences, editable):
        """按原文渲染，句子打 sentence_{i} 标签。文本一个字不改 —— 笔记区尤其如此（A.2）。"""
        widget.config(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        pos = 0
        for i, s in enumerate(sentences):
            start, end = (s["start"], s["end"]) if isinstance(s, dict) else (s.start, s.end)
            if start > pos:
                widget.insert(tk.END, text[pos:start])
            widget.insert(tk.END, text[start:end], (f"sentence_{i}",))
            pos = end
        if pos < len(text):
            widget.insert(tk.END, text[pos:])
        if not editable:
            widget.config(state=tk.DISABLED)

    def _clear_sentence_tags(self, widget, count):
        for i in range(count):
            widget.tag_delete(f"sentence_{i}")
        widget.tag_remove("highlight", "1.0", tk.END)

    # ---------- 笔记身份（§2.1） ----------

    def _note_id_for_chapter(self, chapter_idx):
        if self.book_key is None:
            return (SCRATCH_BOOK, "0")
        return (self.book_key, str(chapter_idx))

    def _note_text(self):
        return self.sandbox_area.get("1.0", "end-1c")

    def _on_note_modified(self, event=None):
        """Tk 的 <<Modified>> 每次内容变化触发一次，处理完必须把标志复位，否则不再触发。
        程序性写入（_show_note / _mutate_note）设了 _suppress_modified，不算用户编辑。"""
        if not self.sandbox_area.edit_modified():
            return
        self.sandbox_area.edit_modified(False)
        if self._suppress_modified:
            return
        self._note_changed(by_user=True)

    def _note_changed(self, by_user):
        if self.displayed_note is None:
            return
        self.note_dirty = True
        self.note_revisions[self.displayed_note] = self.note_revisions.get(self.displayed_note, 0) + 1
        # 文本变了，朗读位置就没意义了；播着的话立刻停（A.2）
        if self.is_playing_sandbox:
            self._stop_play_sandbox()
        self.config.set_note_position(*self.displayed_note, 0, save=False)
        self._clear_sentence_tags(self.sandbox_area, len(self.note_sentences))
        self.note_sentences = []
        self.note_sentence_idx = 0

    def _set_note_widget_text(self, text):
        self._suppress_modified = True
        try:
            self.sandbox_area.delete("1.0", tk.END)
            if text:
                self.sandbox_area.insert("1.0", text)
            self.sandbox_area.edit_reset()
            self.sandbox_area.edit_modified(False)
        finally:
            self._suppress_modified = False

    def _save_displayed_note(self):
        """把右栏存到 displayed_note。没改过就不写盘。失败返回 False，内容和 dirty 都留着。"""
        if self.displayed_note is None or not self.note_dirty:
            return True
        book, chapter = self.displayed_note
        if self.config.set_note(book, chapter, self._note_text()):
            self.note_dirty = False
            return True
        self._set_status(f"笔记没保存：{self.config.last_error}")
        return False

    def _show_note(self, note_id):
        """切换右栏显示的笔记。旧的先存；存不了就不切（A.1）。返回是否切成功。"""
        if note_id == self.displayed_note:
            return True
        if not self._save_displayed_note():
            messagebox.showerror("笔记没保存",
                                 f"{self.config.last_error}\n\n右栏内容保留在原地，没有切换。请先解决保存问题。")
            return False
        if self.is_playing_sandbox:
            self._stop_play_sandbox()
        book, chapter = note_id
        self._clear_sentence_tags(self.sandbox_area, len(self.note_sentences))
        self.note_sentences = []
        self.note_sentence_idx = 0
        self._set_note_widget_text(self.config.get_note(book, chapter))
        self.displayed_note = note_id
        self.note_dirty = False
        if book == SCRATCH_BOOK:
            self.note_title.config(text="草稿（没开书）")
        else:
            self.note_title.config(text=f"第 {int(chapter) + 1} 章笔记")
        return True

    def _mutate_note(self, new_text, source):
        """程序性修改右栏内容的唯一入口（Clear、旧笔记复制、以后的 AI 覆盖/追加）。
        走和用户编辑一样的 revision / dirty / 位置归零 / 停播。"""
        self._set_note_widget_text(new_text)
        self._note_changed(by_user=False)
        self._save_displayed_note()

    # ---------- 打开书 / 切章 ----------

    def _open_file_dialog(self):
        file_path = filedialog.askopenfilename(filetypes=[("EPUB Files", "*.epub")])
        if file_path:
            self._load_epub(file_path)

    def _reload_last_session(self):
        last_file = self.config.config.get("last_file")
        if last_file and os.path.exists(last_file):
            self._load_epub(last_file)

    def _load_epub(self, file_path):
        """候选 parser 先开；开成功、旧笔记存成功，才提交新书状态（A.1）。"""
        self._stop_play()
        self._stop_play_sandbox()
        candidate = EpubParser()
        if not candidate.load_epub(file_path):
            messagebox.showerror("Error", "Failed to load EPUB file.")
            return
        if not self._save_displayed_note():
            messagebox.showerror("笔记没保存", f"{self.config.last_error}\n\n没有打开新书。")
            return

        self.parser = candidate
        self.book_key = file_path
        self.config.add_recent_file(file_path)
        self._update_recent_menu()

        self.toc_listbox.delete(0, tk.END)
        for ch in self.parser.get_chapter_list():
            self.toc_listbox.insert(tk.END, ch.title)

        chapter, offset = self.config.get_bookmark(file_path)
        if chapter >= len(self.parser.chapters):
            chapter, offset = 0, 0
        self._load_chapter(chapter, offset)

    def _load_chapter(self, index, offset=0):
        """加载第 index 章，光标放在 offset 所在句并回写书签（B4）。"""
        if self.book_key is None or index < 0 or index >= len(self.parser.chapters):
            return False
        self._stop_play()
        self._stop_play_sandbox()
        if not self._show_note(self._note_id_for_chapter(index)):
            return False

        self.current_chapter_idx = index
        self.chapter_text, self.current_sentences = self.parser.get_chapter_sentences(index)
        self.current_book_name = os.path.basename(self.book_key).replace(".epub", "")

        self.toc_listbox.selection_clear(0, tk.END)
        self.toc_listbox.selection_set(index)
        self.toc_listbox.see(index)

        self._render_sentences(self.text_area, self.chapter_text, self.current_sentences, editable=False)

        if self.current_sentences:
            self.current_sentence_idx = min(sentence_index_at(self.current_sentences, offset),
                                            len(self.current_sentences) - 1)
            self._highlight_epub(self.current_sentence_idx, save=True)
        else:
            self.current_sentence_idx = 0
            self.config.set_bookmark(self.book_key, index, 0)
        return True

    def _on_toc_select(self, event):
        selection = self.toc_listbox.curselection()
        if selection and selection[0] != self.current_chapter_idx:
            if not self._load_chapter(selection[0]):
                # 切换被拒（笔记没存）：把目录选中项放回去，别让界面说谎
                self.toc_listbox.selection_clear(0, tk.END)
                self.toc_listbox.selection_set(self.current_chapter_idx)

    def _prev_chapter(self):
        if self.book_key is not None and self.current_chapter_idx > 0:
            self._load_chapter(self.current_chapter_idx - 1)

    def _next_chapter(self):
        if self.book_key is not None and self.current_chapter_idx < len(self.parser.chapters) - 1:
            self._load_chapter(self.current_chapter_idx + 1)

    # ---------- 正文播放 ----------

    def _selected_sentence_idx(self, widget):
        """用户选中了某句就从那句开始。返回 None 表示没选。"""
        try:
            ranges = widget.tag_ranges("sel")
            if not ranges:
                return None
            for tag in widget.tag_names(ranges[0]):
                if tag.startswith("sentence_"):
                    widget.tag_remove("sel", "1.0", tk.END)
                    return int(tag.split("_")[1])
            widget.tag_remove("sel", "1.0", tk.END)
        except (tk.TclError, ValueError):
            pass
        return None

    def _toggle_play(self):
        if self.is_playing_sandbox:
            self._stop_play_sandbox()
        if self.book_key is None or not self.current_sentences:
            return
        if self.is_playing:
            self._stop_play()
        else:
            self._start_play()

    def _start_play(self):
        sel = self._selected_sentence_idx(self.text_area)
        if sel is not None:
            self.current_sentence_idx = sel
        if self.current_sentence_idx >= len(self.current_sentences):
            self.current_sentence_idx = 0
        self.is_playing = True
        self.is_playing_sandbox = False
        self.play_btn.config(text="Pause")
        voice_id, rate = self._current_voice_and_rate()
        self.tts.play(voice_id, rate, [s.text for s in self.current_sentences], self.current_sentence_idx)

    def _stop_play(self):
        if self.is_playing:
            self.is_playing = False
            self.play_btn.config(text="Play")
            self.tts.stop()
            self._save_epub_bookmark()

    def _save_epub_bookmark(self):
        if self.book_key is not None and self.current_sentences:
            idx = min(self.current_sentence_idx, len(self.current_sentences) - 1)
            self.config.set_bookmark(self.book_key, self.current_chapter_idx, self.current_sentences[idx].start)

    # ---------- 笔记播放 ----------

    def _toggle_sandbox_play(self):
        if self.is_playing:
            self._stop_play()
        if self.is_playing_sandbox:
            self._stop_play_sandbox()
        else:
            self._start_play_sandbox()

    def _start_play_sandbox(self):
        raw_text = self._note_text()
        if not raw_text.strip():
            return
        sel = self._selected_sentence_idx(self.sandbox_area)
        # 重新解析并打标签。文本不动（A.2）。
        self._clear_sentence_tags(self.sandbox_area, len(self.note_sentences))
        self.note_sentences = parse_note_text(raw_text, EpubParser.split_into_sentences)
        if not self.note_sentences:
            return
        self._render_note_tags()

        if sel is not None:
            self.note_sentence_idx = sel
        else:
            saved = self.config.get_note_position(*self.displayed_note) if self.displayed_note else 0
            self.note_sentence_idx = sentence_index_at(self.note_sentences, saved)
        if self.note_sentence_idx >= len(self.note_sentences):
            self.note_sentence_idx = 0

        self.is_playing_sandbox = True
        self.sandbox_play_btn.config(text="Pause Note")
        voice_id, rate = self._current_voice_and_rate()
        self.tts.play(voice_id, rate, self.note_sentences, self.note_sentence_idx)

    def _render_note_tags(self):
        """只加 tag，不改文本。用 "1.0 + N chars" 定位：Tk 的 chars 按字符数，跟 Python 偏移一致
        （代理对字符在 Tk 8.6 里算两个，网文里几乎不会出现，出现了也只是高亮偏一格）。"""
        for i, s in enumerate(self.note_sentences):
            self.sandbox_area.tag_add(f"sentence_{i}", f"1.0 + {s['start']} chars", f"1.0 + {s['end']} chars")

    def _stop_play_sandbox(self):
        if self.is_playing_sandbox:
            self.is_playing_sandbox = False
            self.sandbox_play_btn.config(text="Play Note")
            self.tts.stop()
            if self.displayed_note and self.note_sentences:
                idx = min(self.note_sentence_idx, len(self.note_sentences) - 1)
                self.config.set_note_position(*self.displayed_note, self.note_sentences[idx]["start"])

    def _clear_sandbox(self):
        self._stop_play_sandbox()
        self._mutate_note("", source="clear")

    # ---------- 旧笔记 ----------

    def _show_legacy_notes(self):
        legacy = self.config.legacy()
        if not legacy:
            messagebox.showinfo("旧笔记", "这个数据文件不是从旧版升级来的，没有旧笔记。")
            return
        win = tk.Toplevel(self.root)
        win.title("旧笔记（升级前的数据，只读）")
        win.geometry("900x600")
        win.configure(bg=self.bg_color)
        win.transient(self.root)

        entries = []   # (label, text)
        for book, chapters in (legacy.get("chapter_notes") or {}).items():
            name = os.path.basename(book)
            for ck in sorted(chapters, key=lambda k: int(k) if k.isdigit() else 0):
                if chapters[ck].strip():
                    entries.append((f"{name} · 旧第 {int(ck) + 1 if ck.isdigit() else ck} 个文件", chapters[ck]))
        if (legacy.get("sandbox_text") or "").strip():
            entries.append(("未归章的草稿（旧 sandbox）", legacy["sandbox_text"]))

        info_lines = [f"升级时间：{legacy.get('migrated_at', '?')}"]
        for book, bm in (legacy.get("bookmarks") or {}).items():
            info_lines.append(f"旧书签：{os.path.basename(book)} → 第 {bm.get('chapter_idx', 0) + 1} 个文件，"
                              f"第 {bm.get('sentence_idx', 0) + 1} 句")
        ttk.Label(win, text=chr(10).join(info_lines), justify=tk.LEFT).pack(anchor=tk.W, padx=10, pady=6)

        body = tk.PanedWindow(win, orient=tk.HORIZONTAL, bg=self.bg_color, bd=0, sashwidth=4)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        lb = tk.Listbox(body, bg="#2d2d2d", fg=self.fg_color, exportselection=False, width=40,
                        selectbackground=self.highlight_bg, selectforeground=self.highlight_fg)
        body.add(lb, minsize=250)
        for label, _ in entries:
            lb.insert(tk.END, label)
        view = tk.Text(body, bg="#252526", fg=self.fg_color, font=("Microsoft YaHei", 11), wrap=tk.WORD,
                       padx=12, pady=12, borderwidth=0, highlightthickness=0)
        body.add(view, minsize=400)
        view.config(state=tk.DISABLED)

        def show(event=None):
            sel = lb.curselection()
            view.config(state=tk.NORMAL)
            view.delete("1.0", tk.END)
            if sel:
                view.insert("1.0", entries[sel[0]][1])
            view.config(state=tk.DISABLED)

        lb.bind("<<ListboxSelect>>", show)

        def append_to_current():
            sel = lb.curselection()
            if not sel:
                return
            text = entries[sel[0]][1]
            current = self._note_text()
            self._mutate_note((current.rstrip() + chr(10) + chr(10) + text) if current.strip() else text,
                              source="legacy_copy")
            self._set_status("已追加到当前章笔记")

        def copy_clipboard():
            sel = lb.curselection()
            if not sel:
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(entries[sel[0]][1])
            self._set_status("已复制到剪贴板")

        btns = ttk.Frame(win)
        btns.pack(fill=tk.X, padx=10, pady=8)
        ttk.Button(btns, text="追加到当前章笔记", command=append_to_current).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="复制到剪贴板", command=copy_clipboard).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side=tk.RIGHT, padx=2)
        if not entries:
            lb.insert(tk.END, "（旧数据里没有非空笔记）")

    def _import_legacy_dialog(self):
        """旧目录改名后自动探测不到旧 bookmarks.json，让使用者自己指。导入后要重启才生效 ——
        内存里的状态、已打开的书、播放线程全绑在旧数据上，热切换不值得冒险。"""
        src = filedialog.askopenfilename(title="选择旧的 bookmarks.json",
                                         filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not src:
            return
        if os.path.exists(self.layout.bookmarks_path):
            if not messagebox.askyesno("导入旧数据",
                                       "当前数据文件会先保留为 migrated-current-<时间>.json，"
                                       "然后用选中的文件替换它。继续？"):
                return
        try:
            notes = paths.import_data_file(self.layout, src)
        except OSError as e:
            messagebox.showerror("导入失败", str(e))
            return
        messagebox.showinfo("导入旧数据", chr(10).join(notes) + chr(10) + "现在关闭程序，请重新启动。")
        self._on_close()

    # ---------- 导出 ----------

    def _export_mp3_ui(self):
        if self.book_key is None:
            messagebox.showerror("Error", "No chapter loaded!")
            return
        voice_id, _rate = self._current_voice_and_rate()
        if voice_id not in NEURAL_VOICES:
            messagebox.showerror("Error", "MP3 Export exclusively supports high-quality Online Native Voices.\n\n"
                                          "Please select one of the top Narrator or Neural Voices to enable exports.")
            return
        raw_notes = self._note_text()
        notes_parsed = parse_note_text(raw_notes, EpubParser.split_into_sentences) if raw_notes.strip() else []
        epub_s = [s.text for s in self.current_sentences]
        if not epub_s:
            messagebox.showinfo("Export", "Chapter is entirely empty.")
            return

        win = tk.Toplevel(self.root)
        win.title("Exporting MP3")
        win.geometry("300x120")
        win.configure(bg="#1e1e1e")
        win.transient(self.root)
        win.grab_set()
        lbl = ttk.Label(win, text="Initializing Exporter Pipeline...", font=("Arial", 10))
        lbl.pack(pady=20)

        try:
            chap_name = self.parser.chapters[self.current_chapter_idx].title
        except Exception:
            chap_name = f"Chapter {self.current_chapter_idx + 1}"

        def on_status_update(status_text):
            self.root.after(0, lambda: lbl.config(text=status_text))

        def on_complete(success):
            if success:
                self.root.after(0, lambda: [lbl.config(text="导出完成（文件 → 打开导出目录）", foreground="lightgreen"),
                                            self.root.after(3000, lambda: [win.grab_release(), win.destroy()])])
            else:
                self.root.after(0, lambda: [lbl.config(text="Export failed.", foreground="red"),
                                            self.root.after(3000, lambda: [win.grab_release(), win.destroy()])])

        run_export_background(
            book_name=getattr(self, 'current_book_name', "Unknown Book"),
            chapter_name=chap_name,
            epub_sentences=epub_s,
            notes_objects=notes_parsed,
            fallback_voice_id=voice_id,
            status_callback=on_status_update,
            done_callback=on_complete,
            export_dir=self.layout.export,
        )

    # ---------- worker 回调 ----------

    def _see_line_centered(self, text_widget, tag_name):
        start_idx = f"{tag_name}.first"
        try:
            text_widget.see(start_idx)
            self.root.update_idletasks()
            bbox = text_widget.dlineinfo(start_idx)
            if bbox:
                y_offset = bbox[1]
                widget_height = text_widget.winfo_height()
                if y_offset > widget_height * 0.7:
                    text_widget.yview_scroll(5, "units")
                elif y_offset < widget_height * 0.3:
                    text_widget.yview_scroll(-5, "units")
        except tk.TclError:
            pass

    def _highlight_epub(self, index, save):
        self.current_sentence_idx = index
        self.text_area.tag_remove("highlight", "1.0", tk.END)
        if index < len(self.current_sentences):
            tag = f"sentence_{index}"
            try:
                self.text_area.tag_add("highlight", f"{tag}.first", f"{tag}.last")
            except tk.TclError:
                return
            self._see_line_centered(self.text_area, tag)
            if save:
                self._save_epub_bookmark()

    def _highlight_sentence(self, index):
        # 第 4 步之前：还是靠「现在谁在播」判断该高亮哪个区。旧 session 的迟到回调会串区（B5），第 4 步修。
        if self.is_playing_sandbox:
            self.note_sentence_idx = index
            self.sandbox_area.tag_remove("highlight", "1.0", tk.END)
            if index < len(self.note_sentences):
                tag = f"sentence_{index}"
                try:
                    self.sandbox_area.tag_add("highlight", f"{tag}.first", f"{tag}.last")
                except tk.TclError:
                    return
                self._see_line_centered(self.sandbox_area, tag)
        elif self.is_playing:
            self._highlight_epub(index, save=True)

    def _auto_next_chapter(self):
        if self.is_playing:
            self.is_playing = False
            self.play_btn.config(text="Play")
            if self.current_chapter_idx < len(self.parser.chapters) - 1:
                if self._load_chapter(self.current_chapter_idx + 1):
                    self.root.after(500, self._start_play)
        elif self.is_playing_sandbox:
            self._stop_play_sandbox()

    def _on_close(self):
        self._stop_play()
        self._stop_play_sandbox()
        if not self._save_displayed_note():
            if not messagebox.askyesno("笔记没保存",
                                       f"{self.config.last_error}\n\n仍然退出？未保存的笔记会丢。"):
                return
        self.tts.quit()
        self.root.destroy()
