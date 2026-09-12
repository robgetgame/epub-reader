"""主窗口。第 3 步（2026-09-12）重写了状态模型，第 6.5 步换成 CustomTkinter 界面（D21–D26）。

界面层的原则：正文区和笔记区仍然是 tk.Text（放在 CTkFrame 里配 CTkScrollbar）——
CustomTkinter 的 CTkTextbox 底下也是 tk.Text，但句子 tag、偏移定位、<<Modified>> 这些全在
tk.Text 的 API 上，直接用它最稳。其余控件（按钮、下拉、滑块、顶栏、底栏）是 CustomTkinter。

三个身份（设计文档 §2）：
- 笔记身份 displayed_note = (book_key, chapter_key)：右栏现在显示的是谁的笔记。
  保存只保存到它，切换只经过 _show_note()，修改只经过 _mutate_note() / 用户敲键盘。
  没开书时 book_key = "__scratch__"。这一条解决 B1 的三种串笔记。
- 播放 session：每次 play() 一个 id，worker 的消息都带 id，只认 active_session 的（§2.2 / A.3）。
  done 是终态，按钮靠它复位；跳过的句子立即进状态栏，也累计进 skipped_log。
  章末自动播放带 (book_key, chapter, nav_token) 三重身份，切过章就作废。
- AI 请求身份：第 8 步做。

书签存 (章序号, 字符偏移)，恢复时高亮并回写（B4）。
正文和笔记都**按原文渲染**，句子用偏移打 tag，不再重排成一句一段（A.2）。
"""

import os
import queue
import re
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox

import customtkinter as ctk

import ai_chat
import paths
from ai_dialog import ChapterChatDialog, ChatStore
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

# 字体候选（D22）。Tk 在 Windows 上报的是英文名；显示用中文。启动时用 tkfont.families() 过滤掉没装的。
FONT_CHOICES = [
    ("微软雅黑", "Microsoft YaHei"),
    ("楷体", "KaiTi"),
    ("宋体", "SimSun"),
    ("等线", "DengXian"),
    ("Segoe UI", "Segoe UI"),
    ("Georgia", "Georgia"),
    ("Cambria", "Cambria"),
]
KIND_LABELS = {"fiction": "小说", "nonfiction": "非虚构"}
KIND_UNSET = "未判定"

# 配色：CustomTkinter dark-blue 主题的底色 + 正文用的深灰；高亮金色沿用旧版
BG = "#1b1b1b"
BG_PANEL = "#202020"
BG_TEXT = "#1e1e1e"
BG_NOTE = "#1f1f22"
FG = "#d4d4d4"
FG_DIM = "#8a8a8a"
HL_BG = "#b8860b"
HL_FG = "#ffffff"
ACCENT = "#7a5c1e"
ACCENT_HOVER = "#8f6d25"


def parse_note_text(raw_text, split):
    """把笔记文本切成带偏移、带音色的句子列表。标签本身不进列表（不朗读、不高亮）。
    voice_id 为 None 表示用全局音色。split 是 EpubParser.split_into_sentences。"""
    out = []
    active = None
    pos = 0

    def take(seg_start, seg_end):
        for s in split(raw_text[seg_start:seg_end]):
            out.append({"text": s.text, "voice_id": active, "spoken": s.spoken,
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
        self.root.title("EPUB Reader")
        self.root.geometry("1400x820")
        self.root.minsize(1000, 600)

        # 外观模式在 main.py 建根窗口前设；这里不再调 set_appearance_mode ——
        # 它会遍历 CustomTkinter 登记过的所有控件，测试里换根窗口时会碰到已销毁的
        self.bg_color = BG
        self.fg_color = FG
        self.highlight_bg = HL_BG
        self.highlight_fg = HL_FG
        self.root.configure(bg=BG)

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

        # 播放 session（§2.2）
        self.active_session = None      # 当前认的 session id；None = 没在播
        self.active_target = None       # "epub" | "note"
        self.nav_token = 0              # 每次切章 +1；延迟自动播放要对上号
        self._auto_after_id = None
        self.skipped_log = []           # (章序号, 句序号, 原因)，状态栏可点开
        self.selection_end = None       # 选区播放的末句序号；None = 整章

        self.events = queue.Queue()
        self.tts = TTSWorker(self.events, cache_dir=self.layout.cache)

        # AI（第 8 步）：配置来自 config.json + 环境变量；client 建不起来（base_url 非法）也不挡程序
        self.ai_cfg = ai_chat.load_ai_config(paths.load_config(self.layout.portable)[0])
        self.ai_client = None
        self.ai_error = None
        try:
            self.ai_client = ai_chat.OpenRouterClient(self.ai_cfg["model"], self.ai_cfg["api_key"], self.ai_cfg["base_url"],
                                                      self.ai_cfg["allow_custom_base_url"])
        except ai_chat.AiError as e:
            self.ai_error = str(e)
        self.ai_manager = ai_chat.AiTaskManager(self.events)
        self.chat_store = ChatStore(self.layout.ai_chat_path, read_only=not self.layout.data_ok)
        self.ai_dialog = None
        self._billed_requests = set()
        self.sapi_voices = []
        self.voices_ready = False

        # 字体（D22）：只列装了的
        installed = set()
        try:
            installed = set(tkfont.families())
        except tk.TclError:
            pass
        self.font_choices = [(label, fam) for label, fam in FONT_CHOICES if fam in installed] or [FONT_CHOICES[0]]
        self.font_label_to_family = dict(self.font_choices)
        self.font_family_to_label = {fam: label for label, fam in self.font_choices}

        self._build_ui()
        self._setup_keybinds()
        self._load_voices()
        self.root.after(50, self._drain_events)

        # 数据目录不可用 / 迁移发生了什么 / 数据文件读的时候出过事 —— 全部说出来，不静默
        if self.layout.data_problem:
            messagebox.showwarning("数据目录", self.layout.data_problem)
        if self.config.load_error:
            messagebox.showwarning("数据文件", self.config.load_error)
        if self.chat_store.load_error:
            messagebox.showwarning("对话记录", self.chat_store.load_error)
        if self.ai_error:
            self._set_status("AI 不可用：" + self.ai_error)
        if self.startup_notes:
            messagebox.showinfo("数据迁移", chr(10).join(self.startup_notes))

        # 没开书时右栏显示草稿；开了书 _load_chapter 会切到该章笔记
        self._show_note((SCRATCH_BOOK, "0"))
        self._reload_last_session()

    # ---------- UI ----------

    def _build_ui(self):
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)

        # ===== 顶栏 =====
        top = ctk.CTkFrame(root, fg_color=BG_PANEL, corner_radius=0, height=44)
        top.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(top, text="打开…", width=70, command=self._open_file_dialog).pack(side="left", padx=(12, 4), pady=8)
        self.recent_btn = ctk.CTkButton(top, text="最近 ▾", width=70, fg_color="#2c2c2c", hover_color="#3a3a3a",
                                        command=self._post_recent_menu)
        self.recent_btn.pack(side="left", padx=4, pady=8)
        self.recent_menu = tk.Menu(root, tearoff=0, bg="#2a2a2a", fg=FG, activebackground="#3a3a3a", activeforeground="#fff", bd=0)
        self._update_recent_menu()
        self.book_title_var = tk.StringVar(value="没有打开的书")
        ctk.CTkLabel(top, textvariable=self.book_title_var, font=ctk.CTkFont(size=14, weight="bold")).pack(side="left", padx=(16, 4))
        self.chapter_title_var = tk.StringVar(value="")
        ctk.CTkLabel(top, textvariable=self.chapter_title_var, text_color=FG_DIM).pack(side="left", padx=4)
        for text, cmd in (("导出目录", lambda: paths.open_in_explorer(self.layout.export)),
                          ("数据目录", lambda: paths.open_in_explorer(self.layout.data)),
                          ("旧笔记", self._show_legacy_notes)):
            ctk.CTkButton(top, text=text, width=76, fg_color="#2c2c2c", hover_color="#3a3a3a", command=cmd).pack(side="right", padx=4, pady=8)

        # ===== 三栏 =====
        paned = tk.PanedWindow(root, orient=tk.HORIZONTAL, bg=BG, bd=0, sashwidth=5, sashrelief="flat")
        paned.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)

        # --- 左：目录 ---
        left = ctk.CTkFrame(paned, fg_color="#181818", corner_radius=0)
        paned.add(left, minsize=200, width=250, stretch="never")
        self.toc_filter_var = tk.StringVar()
        self.toc_filter_var.trace_add("write", lambda *a: self._apply_toc_filter())
        self.toc_search = ctk.CTkEntry(left, placeholder_text="搜章名…", textvariable=self.toc_filter_var)
        self.toc_search.pack(fill="x", padx=10, pady=(10, 6))
        toc_wrap = ctk.CTkFrame(left, fg_color="transparent")
        toc_wrap.pack(fill="both", expand=True, padx=(10, 4), pady=(0, 10))
        self.toc_listbox = tk.Listbox(toc_wrap, bg="#181818", fg="#bbbbbb", selectbackground="#2f2f2f", selectforeground="#ffffff",
                                      borderwidth=0, highlightthickness=0, activestyle="none",
                                      font=("Microsoft YaHei", 11), exportselection=False)
        toc_sb = ctk.CTkScrollbar(toc_wrap, command=self.toc_listbox.yview)
        self.toc_listbox.configure(yscrollcommand=toc_sb.set)
        toc_sb.pack(side="right", fill="y")
        self.toc_listbox.pack(side="left", fill="both", expand=True)
        self.toc_listbox.bind("<<ListboxSelect>>", self._on_toc_select)
        self._toc_visible = []      # 过滤后列表里每一行对应的章序号

        # --- 中：正文 + 播放条 ---
        mid = ctk.CTkFrame(paned, fg_color=BG, corner_radius=0)
        paned.add(mid, minsize=400, stretch="always")   # 富余空间全给正文，不给最后一栏
        mid.grid_rowconfigure(0, weight=1)
        mid.grid_columnconfigure(0, weight=1)
        text_wrap = ctk.CTkFrame(mid, fg_color="transparent")
        text_wrap.grid(row=0, column=0, sticky="nsew")
        self.text_area = tk.Text(text_wrap, bg=BG_TEXT, fg=FG, wrap=tk.WORD, padx=36, pady=24,
                                 borderwidth=0, highlightthickness=0, spacing3=10, insertbackground=FG,
                                 selectbackground="#3b6ea5", selectforeground="#fff")
        text_sb = ctk.CTkScrollbar(text_wrap, command=self.text_area.yview)
        self.text_area.configure(yscrollcommand=text_sb.set)
        text_sb.pack(side="right", fill="y", pady=8)
        self.text_area.pack(side="left", fill="both", expand=True)
        self.text_area.tag_configure("highlight", background=HL_BG, foreground=HL_FG)
        self.text_area.config(state=tk.DISABLED)   # 只读靠 state，不拦 <Key>（Ctrl+C 要能用）

        transport = ctk.CTkFrame(mid, fg_color=BG_PANEL, corner_radius=0, height=46)
        transport.grid(row=1, column=0, sticky="ew")
        ctk.CTkButton(transport, text="◀ 上一章", width=90, fg_color="#2c2c2c", hover_color="#3a3a3a", command=self._prev_chapter).pack(side="left", padx=(20, 4), pady=8)
        self.play_btn = ctk.CTkButton(transport, text="▶ 播放", width=110, font=ctk.CTkFont(size=13, weight="bold"), command=self._toggle_play)
        self.play_btn.pack(side="left", padx=4, pady=8)
        ctk.CTkButton(transport, text="下一章 ▶", width=90, fg_color="#2c2c2c", hover_color="#3a3a3a", command=self._next_chapter).pack(side="left", padx=4, pady=8)
        ctk.CTkButton(transport, text="导出 mp3", width=90, fg_color="#2c2c2c", hover_color="#3a3a3a", command=self._export_mp3_ui).pack(side="right", padx=20, pady=8)

        # --- 右：笔记 ---
        right = ctk.CTkFrame(paned, fg_color=BG_NOTE, corner_radius=0)
        paned.add(right, minsize=320, width=400, stretch="never")
        right.grid_rowconfigure(2, weight=1)
        right.grid_columnconfigure(0, weight=1)
        hdr = ctk.CTkFrame(right, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        self.note_title_var = tk.StringVar(value="草稿")
        ctk.CTkLabel(hdr, textvariable=self.note_title_var, font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        self.note_state_var = tk.StringVar(value="")
        ctk.CTkLabel(hdr, textvariable=self.note_state_var, text_color=FG_DIM).pack(side="left", padx=6)
        self.ai_btn = ctk.CTkButton(hdr, text="AI 讲解…", width=90, fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._open_ai_dialog)
        self.ai_btn.pack(side="right")
        # 第二行：书的类型（D25）。一本书选一次；AI 讲解按它选结构。单独一行，右栏窄的时候不会挤成一个箭头
        hdr2 = ctk.CTkFrame(right, fg_color="transparent")
        hdr2.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))
        ctk.CTkLabel(hdr2, text="这本书的类型", text_color=FG_DIM).pack(side="left", padx=(0, 8))
        self.kind_var = tk.StringVar(value=KIND_UNSET)
        self.kind_menu = ctk.CTkOptionMenu(hdr2, variable=self.kind_var, values=[KIND_UNSET] + list(KIND_LABELS.values()),
                                           width=110, command=self._on_kind_change, fg_color="#2c2c2c", button_color="#3a3a3a")
        self.kind_menu.pack(side="left")

        note_wrap = ctk.CTkFrame(right, fg_color="transparent")
        note_wrap.grid(row=2, column=0, sticky="nsew", padx=(8, 4))
        self.sandbox_area = tk.Text(note_wrap, bg="#252528", fg=FG, wrap=tk.WORD, padx=16, pady=16,
                                    borderwidth=0, highlightthickness=0, undo=True, insertbackground=FG,
                                    selectbackground="#3b6ea5", selectforeground="#fff")
        note_sb = ctk.CTkScrollbar(note_wrap, command=self.sandbox_area.yview)
        self.sandbox_area.configure(yscrollcommand=note_sb.set)
        note_sb.pack(side="right", fill="y")
        self.sandbox_area.pack(side="left", fill="both", expand=True)
        self.sandbox_area.tag_configure("highlight", background=HL_BG, foreground=HL_FG)
        self.sandbox_area.bind("<<Modified>>", self._on_note_modified)
        self.sandbox_area.bind("<FocusOut>", lambda e: self._save_displayed_note())

        foot = ctk.CTkFrame(right, fg_color="transparent")
        foot.grid(row=3, column=0, sticky="ew", padx=12, pady=8)
        self.sandbox_play_btn = ctk.CTkButton(foot, text="▶ 播放笔记", width=100, fg_color="#2c2c2c", hover_color="#3a3a3a", command=self._toggle_sandbox_play)
        self.sandbox_play_btn.pack(side="left")
        ctk.CTkButton(foot, text="清空", width=60, fg_color="#2c2c2c", hover_color="#3a3a3a", command=self._clear_sandbox).pack(side="right")

        # ===== 底栏：设置 + 状态 =====
        bottom = ctk.CTkFrame(root, fg_color=BG_PANEL, corner_radius=0, height=40)
        bottom.grid(row=2, column=0, sticky="ew")

        def lab(text):
            ctk.CTkLabel(bottom, text=text, text_color=FG_DIM).pack(side="left", padx=(12, 4))

        lab("音色")
        self.voice_var = tk.StringVar()
        self.voice_menu = ctk.CTkOptionMenu(bottom, variable=self.voice_var, values=[""], width=230,
                                            command=lambda v: self._on_settings_change(), fg_color="#2c2c2c", button_color="#3a3a3a")
        self.voice_menu.pack(side="left", pady=6)

        lab("语速")
        self.speed_var = tk.IntVar(value=int(self.config.config.get("speech_rate", 200)))
        self.speed_slider = ctk.CTkSlider(bottom, from_=50, to=400, width=120, variable=self.speed_var,
                                          command=lambda v: self.speed_value_var.set(str(int(float(v)))))
        self.speed_slider.pack(side="left", pady=6)
        self.speed_slider.bind("<ButtonRelease-1>", self._on_settings_change)   # 松手才存盘
        self.speed_value_var = tk.StringVar(value=str(self.speed_var.get()))
        ctk.CTkLabel(bottom, textvariable=self.speed_value_var, width=32).pack(side="left")

        lab("音量")
        self.volume_var = tk.IntVar(value=int(self.config.config.get("volume", 100)))
        self.volume_slider = ctk.CTkSlider(bottom, from_=0, to=100, width=100, variable=self.volume_var,
                                           command=self._on_volume_drag)
        self.volume_slider.pack(side="left", pady=6)
        self.volume_slider.bind("<ButtonRelease-1>", self._on_settings_change)
        self.volume_value_var = tk.StringVar(value=str(self.volume_var.get()))
        ctk.CTkLabel(bottom, textvariable=self.volume_value_var, width=32).pack(side="left")

        lab("字体")
        saved_family = self.config.config.get("font_family") or FONT_CHOICES[0][1]
        self.font_var = tk.StringVar(value=self.font_family_to_label.get(saved_family, self.font_choices[0][0]))
        self.font_menu = ctk.CTkOptionMenu(bottom, variable=self.font_var, values=[l for l, _ in self.font_choices], width=110,
                                           command=lambda v: self._on_font_change(), fg_color="#2c2c2c", button_color="#3a3a3a")
        self.font_menu.pack(side="left", pady=6)
        lab("字号")
        self.font_size_var = tk.IntVar(value=int(self.config.config.get("font_size", 16)))
        self.font_size_slider = ctk.CTkSlider(bottom, from_=12, to=28, number_of_steps=16, width=100, variable=self.font_size_var,
                                              command=lambda v: self._on_font_change(save=False))
        self.font_size_slider.pack(side="left", pady=6)
        self.font_size_slider.bind("<ButtonRelease-1>", lambda e: self._on_font_change(save=True))
        self.font_size_value_var = tk.StringVar(value=str(self.font_size_var.get()))
        ctk.CTkLabel(bottom, textvariable=self.font_size_value_var, width=28).pack(side="left")

        self.status_var = tk.StringVar(value="")
        status = ctk.CTkLabel(bottom, textvariable=self.status_var, text_color="#8fbf8f", anchor="e", cursor="hand2")
        status.pack(side="right", padx=14, fill="x", expand=True)
        status.bind("<Button-1>", lambda e: self._show_skipped_log())

        self._apply_fonts()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _post_recent_menu(self):
        try:
            x = self.recent_btn.winfo_rootx()
            y = self.recent_btn.winfo_rooty() + self.recent_btn.winfo_height()
            self.recent_menu.tk_popup(x, y)
        finally:
            self.recent_menu.grab_release()

    def _apply_toc_filter(self):
        """目录搜索：只列标题含关键字的章，选中仍映射回真实章序号。"""
        key = self.toc_filter_var.get().strip().lower()
        self.toc_listbox.delete(0, tk.END)
        self._toc_visible = []
        for ch in self.parser.get_chapter_list():
            if not key or key in ch.title.lower():
                self.toc_listbox.insert(tk.END, ch.title)
                self._toc_visible.append(ch.index)
        self._select_toc_row(self.current_chapter_idx)

    def _select_toc_row(self, chapter_idx):
        self.toc_listbox.selection_clear(0, tk.END)
        if chapter_idx in self._toc_visible:
            row = self._toc_visible.index(chapter_idx)
            self.toc_listbox.selection_set(row)
            self.toc_listbox.see(row)

    # ---------- 字体 / 音量 / 类型 ----------

    def _apply_fonts(self):
        family = self.font_label_to_family.get(self.font_var.get(), FONT_CHOICES[0][1])
        size = int(self.font_size_var.get())
        self.text_area.configure(font=(family, size))
        self.sandbox_area.configure(font=(family, max(10, size - 2)))
        self.font_size_value_var.set(str(size))

    def _on_font_change(self, save=True):
        self._apply_fonts()
        if save:
            family = self.font_label_to_family.get(self.font_var.get(), FONT_CHOICES[0][1])
            self.config.set_font(family, int(self.font_size_var.get()))

    def _on_volume_drag(self, value):
        v = int(float(value))
        self.volume_value_var.set(str(v))
        voice_id, rate = self._current_voice_and_rate()
        self.tts.set_settings(voice_id, rate, v)   # 拖的过程中就生效（pygame 立即，SAPI 下一句）

    def _kind_label(self, kind):
        return KIND_LABELS.get(kind, KIND_UNSET)

    def _refresh_kind_menu(self):
        if self.book_key is None:
            self.kind_var.set(KIND_UNSET)
            self.kind_menu.configure(state="disabled")
            return
        self.kind_menu.configure(state="normal")
        self.kind_var.set(self._kind_label(self.config.get_book_meta(self.book_key).get("kind")))

    def _on_kind_change(self, label):
        if self.book_key is None:
            return
        if self.ai_dialog is not None:
            self.root.after(0, lambda: self.ai_dialog and (self.ai_dialog._refresh_kind_label(), self.ai_dialog._refresh_buttons()))
        for kind, lab in KIND_LABELS.items():
            if lab == label:
                self.config.set_book_kind(self.book_key, kind, "manual")
                return
        # 选回「未判定」：删掉记录，AI 下次会重新判
        meta = self.config.config["book_meta"].get(self.book_key)
        if meta:
            meta.pop("kind", None)
            meta.pop("kind_source", None)
            self.config.save()

    def _open_ai_dialog(self):
        if self.book_key is None:
            messagebox.showinfo("AI 讲解", "先打开一本书。")
            return
        if self.ai_error:
            messagebox.showerror("AI 讲解", self.ai_error)
            return
        if self.ai_dialog is not None:
            try:
                self.ai_dialog.lift()
                self.ai_dialog.focus_force()
                return
            except tk.TclError:
                self.ai_dialog = None
        note_id = self._note_id_for_chapter(self.current_chapter_idx)
        title = self.parser.chapters[self.current_chapter_idx].title
        self.ai_dialog = ChapterChatDialog(self, note_id, title, self.book_title_var.get(), self.chapter_text)

    def _update_note_header(self):
        if self.displayed_note is None:
            return
        book, chapter = self.displayed_note
        # 不写「第 N 章」：目录序号和书名里的章号（封面、序言占了前几个）对不上，会误导
        self.note_title_var.set("草稿（没开书）" if book == SCRATCH_BOOK else "本章笔记")
        self.note_state_var.set("· 未保存" if self.note_dirty else "· 已保存")

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
        if w is self.sandbox_area or isinstance(w, tk.Entry):   # CTkEntry 内部就是 tk.Entry
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
        recent = self.config.config.get("recent_files", [])
        if not recent:
            self.recent_menu.add_command(label="（没有最近打开的书）", state="disabled")
        for path in recent:
            self.recent_menu.add_command(label=os.path.basename(path), command=lambda p=path: self._load_epub(p))

    def _load_voices(self):
        """神经音色立刻可选；本机语音等 worker 发 ready 再补进来（B8：启动不等 COM）。"""
        voices = [(nv, nv.replace("Neural", "") + "（在线）") for nv in NEURAL_VOICES] + list(self.sapi_voices)
        self.voice_map = {name: vid for vid, name in voices}
        self.voice_menu.configure(values=list(self.voice_map.keys()))
        saved_voice = self.config.config.get("voice_id")
        selected_name = None
        if saved_voice:
            for name, vid in self.voice_map.items():
                if vid == saved_voice:
                    selected_name = name
                    break
        if selected_name:
            self.voice_var.set(selected_name)
        elif voices and self.voice_var.get() not in self.voice_map:
            self.voice_var.set(voices[0][1])
        self._push_settings()

    def _push_settings(self):
        voice_id, rate = self._current_voice_and_rate()
        self.tts.set_settings(voice_id, rate, int(self.volume_var.get()))

    def _on_settings_change(self, event=None):
        voice_id, rate = self._current_voice_and_rate()
        volume = int(self.volume_var.get())
        self.speed_value_var.set(str(rate))
        self.volume_value_var.set(str(volume))
        self.config.set_voice_and_rate(voice_id, rate, volume)
        self.tts.set_settings(voice_id, rate, volume)   # 播放中也生效：worker 每句重读

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
        self._update_note_header()
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
            self._update_note_header()
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
        self._update_note_header()
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

        self.book_title_var.set(os.path.basename(file_path).replace(".epub", ""))
        self.toc_filter_var.set("")
        self._apply_toc_filter()
        self._refresh_kind_menu()

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

        self.nav_token += 1
        self.current_chapter_idx = index
        self.chapter_text, self.current_sentences = self.parser.get_chapter_sentences(index)
        self.current_book_name = os.path.basename(self.book_key).replace(".epub", "")

        self._select_toc_row(index)
        self.chapter_title_var.set("· " + self.parser.chapters[index].title)

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
        if not selection or selection[0] >= len(self._toc_visible):
            return
        idx = self._toc_visible[selection[0]]
        if idx != self.current_chapter_idx:
            if not self._load_chapter(idx):
                # 切换被拒（笔记没存）：把目录选中项放回去，别让界面说谎
                self._select_toc_row(self.current_chapter_idx)

    def _prev_chapter(self):
        if self.book_key is not None and self.current_chapter_idx > 0:
            self._load_chapter(self.current_chapter_idx - 1)

    def _next_chapter(self):
        if self.book_key is not None and self.current_chapter_idx < len(self.parser.chapters) - 1:
            self._load_chapter(self.current_chapter_idx + 1)

    # ---------- 正文播放 ----------

    def _selected_range(self, widget, sentences):
        """选区覆盖的句子范围 (start_idx, end_idx)，含两端；没选返回 None。
        用字符偏移算：控件里的文本就是原文（第 3 步起按原样渲染），偏移和句子偏移是同一坐标系。
        选区落在句子之间的空白上也能算出来：起点取第一个 end > 选区起点 的句子，终点取最后一个 start < 选区终点 的。"""
        if not sentences:
            return None
        try:
            ranges = widget.tag_ranges("sel")
            if not ranges:
                return None
            sel_start = widget.count("1.0", ranges[0], "chars")[0]
            sel_end = widget.count("1.0", ranges[1], "chars")[0]
            widget.tag_remove("sel", "1.0", tk.END)
        except (tk.TclError, TypeError, IndexError):
            return None
        if sel_end <= sel_start:
            return None

        def s_start(x):
            return x["start"] if isinstance(x, dict) else x.start

        def s_end(x):
            return x["end"] if isinstance(x, dict) else x.end

        start_idx = next((i for i, x in enumerate(sentences) if s_end(x) > sel_start), None)
        end_idx = next((i for i in range(len(sentences) - 1, -1, -1) if s_start(sentences[i]) < sel_end), None)
        if start_idx is None or end_idx is None or end_idx < start_idx:
            return None
        return start_idx, end_idx

    def _selected_sentence_idx(self, widget):
        """兼容旧调用：只要起点。"""
        sentences = self.current_sentences if widget is self.text_area else self.note_sentences
        r = self._selected_range(widget, sentences)
        return None if r is None else r[0]

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
        """有选区：只读选区、读完停、不翻页，下次 Play 从选区末尾继续（D13 / AGENTS.md）。
        没选区：从当前位置（书签）读到章末，读完自动翻页。"""
        end_idx = None
        sel = self._selected_range(self.text_area, self.current_sentences)
        if sel is not None:
            self.current_sentence_idx, end_idx = sel
        if self.current_sentence_idx >= len(self.current_sentences):
            self.current_sentence_idx = 0
        self._cancel_auto_next()
        self.is_playing = True
        self.is_playing_sandbox = False
        self.play_btn.configure(text="⏸ 暂停")
        self._push_settings()
        self.active_target = "epub"
        self.selection_end = end_idx
        self.active_session = self.tts.play([{"text": s.text, "spoken": s.spoken} for s in self.current_sentences],
                                            self.current_sentence_idx, target="epub", end_idx=end_idx)

    def _stop_play(self):
        self._cancel_auto_next()
        if self.is_playing:
            self.is_playing = False
            self.play_btn.configure(text="▶ 播放")
            self.active_session = None
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

        self._cancel_auto_next()
        self.is_playing_sandbox = True
        self.sandbox_play_btn.configure(text="⏸ 暂停笔记")
        self._push_settings()
        self.active_target = "note"
        self.active_session = self.tts.play(self.note_sentences, self.note_sentence_idx, target="note")

    def _render_note_tags(self):
        """只加 tag，不改文本。用 "1.0 + N chars" 定位：Tk 的 chars 按字符数，跟 Python 偏移一致
        （代理对字符在 Tk 8.6 里算两个，网文里几乎不会出现，出现了也只是高亮偏一格）。"""
        for i, s in enumerate(self.note_sentences):
            self.sandbox_area.tag_add(f"sentence_{i}", f"1.0 + {s['start']} chars", f"1.0 + {s['end']} chars")

    def _stop_play_sandbox(self):
        if self.is_playing_sandbox:
            self.is_playing_sandbox = False
            self.sandbox_play_btn.configure(text="▶ 播放笔记")
            self.active_session = None
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
        win = ctk.CTkToplevel(self.root)
        win.title("旧笔记（升级前的数据，只读）")
        win.geometry("940x620")
        win.transient(self.root)

        entries = []   # (label, text)
        info_lines = []
        if legacy:
            for book, chapters in (legacy.get("chapter_notes") or {}).items():
                name = os.path.basename(book)
                for ck in sorted(chapters, key=lambda k: int(k) if k.isdigit() else 0):
                    if chapters[ck].strip():
                        entries.append((f"{name} · 旧第 {int(ck) + 1 if ck.isdigit() else ck} 个文件", chapters[ck]))
            if (legacy.get("sandbox_text") or "").strip():
                entries.append(("未归章的草稿（旧 sandbox）", legacy["sandbox_text"]))
            info_lines.append(f"升级时间：{legacy.get('migrated_at', '?')}")
            for book, bm in (legacy.get("bookmarks") or {}).items():
                info_lines.append(f"旧书签：{os.path.basename(book)} → 第 {bm.get('chapter_idx', 0) + 1} 个文件，"
                                  f"第 {bm.get('sentence_idx', 0) + 1} 句")
        else:
            info_lines.append("这个数据文件不是从旧版升级来的，没有旧笔记。旧 exe 旁边的 bookmarks.json 可以用下面的按钮导入。")
        ctk.CTkLabel(win, text=chr(10).join(info_lines), justify="left", anchor="w", text_color=FG_DIM).pack(fill="x", padx=14, pady=(12, 6))

        body = tk.PanedWindow(win, orient=tk.HORIZONTAL, bg=BG, bd=0, sashwidth=5)
        body.pack(fill="both", expand=True, padx=14, pady=4)
        lb = tk.Listbox(body, bg="#2d2d2d", fg=FG, exportselection=False, width=40, borderwidth=0, highlightthickness=0,
                        selectbackground=HL_BG, selectforeground=HL_FG, font=("Microsoft YaHei", 10))
        body.add(lb, minsize=260)
        for label, _ in entries:
            lb.insert(tk.END, label)
        if not entries:
            lb.insert(tk.END, "（没有非空的旧笔记）")
        view = tk.Text(body, bg="#252528", fg=FG, font=("Microsoft YaHei", 11), wrap=tk.WORD,
                       padx=12, pady=12, borderwidth=0, highlightthickness=0)
        body.add(view, minsize=400)
        view.config(state=tk.DISABLED)

        def show(event=None):
            sel = lb.curselection()
            view.config(state=tk.NORMAL)
            view.delete("1.0", tk.END)
            if sel and sel[0] < len(entries):
                view.insert("1.0", entries[sel[0]][1])
            view.config(state=tk.DISABLED)

        lb.bind("<<ListboxSelect>>", show)

        def append_to_current():
            sel = lb.curselection()
            if not sel or sel[0] >= len(entries):
                return
            text = entries[sel[0]][1]
            current = self._note_text()
            self._mutate_note((current.rstrip() + chr(10) + chr(10) + text) if current.strip() else text,
                              source="legacy_copy")
            self._set_status("已追加到当前章笔记")

        def copy_clipboard():
            sel = lb.curselection()
            if not sel or sel[0] >= len(entries):
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(entries[sel[0]][1])
            self._set_status("已复制到剪贴板")

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=14, pady=10)
        ctk.CTkButton(btns, text="追加到当前章笔记", command=append_to_current).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="复制到剪贴板", fg_color="#2c2c2c", hover_color="#3a3a3a", command=copy_clipboard).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="导入旧数据文件…", fg_color="#2c2c2c", hover_color="#3a3a3a",
                      command=lambda: [win.destroy(), self._import_legacy_dialog()]).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="关闭", fg_color="#2c2c2c", hover_color="#3a3a3a", command=win.destroy).pack(side="right", padx=4)

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
            messagebox.showerror("导出", "没有打开的章节。")
            return
        voice_id, rate = self._current_voice_and_rate()
        if voice_id not in NEURAL_VOICES:
            messagebox.showerror("导出", "导出 mp3 只支持在线神经音色，请先在下面选一个 Neural 音色。")
            return
        raw_notes = self._note_text()
        notes_parsed = parse_note_text(raw_notes, EpubParser.split_into_sentences) if raw_notes.strip() else []
        epub_s = [s.text for s in self.current_sentences if s.spoken]
        if not epub_s and not notes_parsed:
            messagebox.showinfo("导出", "本章没有可导出的内容。")
            return

        win = ctk.CTkToplevel(self.root)
        win.title("导出 mp3")
        win.geometry("460x200")
        win.transient(self.root)
        lbl = ctk.CTkLabel(win, text="准备中…", wraplength=420, justify="left", anchor="w")
        lbl.pack(pady=16, padx=16, fill="x")
        ctk.CTkButton(win, text="关闭", width=80, command=win.destroy).pack(pady=6)

        try:
            chap_name = self.parser.chapters[self.current_chapter_idx].title
        except Exception:
            chap_name = f"Chapter {self.current_chapter_idx + 1}"

        def on_status_update(status_text):
            # 使用者可能已经把导出窗口关了，别往销毁的控件上写
            self.root.after(0, lambda: win.winfo_exists() and lbl.configure(text=status_text))

        def on_complete(success, summary):
            def show():
                self._set_status(summary.splitlines()[0])
                if win.winfo_exists():
                    lbl.configure(text=summary, text_color="#8fbf8f" if success else "#e0a040")
                if not success:
                    messagebox.showwarning("导出", summary)
            self.root.after(0, show)

        run_export_background(
            book_name=getattr(self, 'current_book_name', "Unknown Book"),
            book_path=self.book_key,
            chapter_index=self.current_chapter_idx,
            chapter_name=chap_name,
            epub_sentences=epub_s,
            notes_objects=notes_parsed,
            fallback_voice_id=voice_id,
            rate=rate,
            status_callback=on_status_update,
            done_callback=on_complete,
            export_dir=self.layout.export,
        )

    # ---------- 高亮 ----------

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

    # ---------- worker 事件（§2.2） ----------

    def _drain_events(self):
        if getattr(self, "_closed", False):
            return
        try:
            while True:
                msg = self.events.get_nowait()
                try:
                    self._handle_event(msg)
                except Exception as e:     # 一条消息处理炸了不能把定时器炸掉
                    print("event error:", msg[0], e)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_events)

    def _handle_event(self, msg):
        kind = msg[0]
        if kind == "ready":
            self.sapi_voices = list(msg[1])
            self.voices_ready = True
            self._load_voices()
            return
        if kind == "init_error":
            self._set_status(msg[1] + "（神经音色仍可用）")
            return
        if kind == "audio_error":
            self._set_status(msg[1])
            messagebox.showerror("音频输出", msg[1] + chr(10) + "神经音色无法播放；本机语音不受影响。")
            return
        if kind.startswith("ai_"):
            self._handle_ai_event(msg)
            return

        sid = msg[1]
        if sid != self.active_session:
            return      # 旧 session 的迟到消息：丢（B5）
        if kind == "highlight":
            idx = msg[2]
            if self.active_target == "note":
                self._highlight_note(idx)
            else:
                self._highlight_epub(idx, save=True)
        elif kind == "skipped":
            _, _, idx, reason = msg
            self.skipped_log.append((self.current_chapter_idx if self.active_target == "epub" else -1, idx, reason))
            self._set_status(f"第 {idx + 1} 句没读：{reason}（点这里看全部）")
        elif kind == "done":
            _, _, status, skipped = msg
            self._on_session_done(status, skipped)

    def _handle_ai_event(self, msg):
        """ai_done 永远先记账再转给对话窗（A.5）；对话窗关了消息就丢，占用照样在 ai_finished 释放。"""
        kind = msg[0]
        dlg = self.ai_dialog
        if kind == "ai_delta":
            if dlg is not None:
                dlg.on_delta(msg[1], msg[2])
        elif kind == "ai_done":
            _, rid, status, text, usage, finish, error = msg
            self._bill(rid, usage)
            if dlg is not None:
                dlg.on_done(rid, status, text, usage, finish, error)
        elif kind == "ai_finished":
            if dlg is not None:
                dlg.on_finished(msg[1])
        elif kind == "ai_detect":
            _, rid, book_key, result, error, usage = msg
            self._bill(rid, usage)
            if result and not self.config.get_book_meta(book_key).get("kind"):
                self.config.set_book_kind(book_key, result["kind"], "auto", result.get("language"))
                if book_key == self.book_key:
                    self._refresh_kind_menu()
            if dlg is not None:
                dlg.on_detect(rid, result, error, usage)

    def _bill(self, rid, usage):
        """累计到 bookmarks.json 的 ai_usage。按请求 id 去重；没有 usage 记为费用未知，不记 0。"""
        if rid in self._billed_requests:
            return
        self._billed_requests.add(rid)
        totals = self.config.config.setdefault("ai_usage", {})
        p, c, cached, cost = ai_chat.usage_summary(usage)
        if p is None and c is None and cost is None:
            totals["unknown_cost_requests"] = totals.get("unknown_cost_requests", 0) + 1
        else:
            totals["prompt_tokens"] = totals.get("prompt_tokens", 0) + (p or 0)
            totals["completion_tokens"] = totals.get("completion_tokens", 0) + (c or 0)
            totals["cached_tokens"] = totals.get("cached_tokens", 0) + (cached or 0)
            if cost is None:
                totals["unknown_cost_requests"] = totals.get("unknown_cost_requests", 0) + 1
            else:
                totals["cost_usd"] = totals.get("cost_usd", 0.0) + cost
        totals["requests"] = totals.get("requests", 0) + 1
        self.config.save()

    def _highlight_note(self, index):
        self.note_sentence_idx = index
        self.sandbox_area.tag_remove("highlight", "1.0", tk.END)
        if index < len(self.note_sentences):
            tag = f"sentence_{index}"
            try:
                self.sandbox_area.tag_add("highlight", f"{tag}.first", f"{tag}.last")
            except tk.TclError:
                return
            self._see_line_centered(self.sandbox_area, tag)

    def _on_session_done(self, status, skipped):
        """终态：按钮复位。只有 finished 才自动翻页；cancelled / failed 停在原地（A.3）。"""
        target = self.active_target
        self.active_session = None
        if target == "note":
            self.is_playing_sandbox = False
            self.sandbox_play_btn.configure(text="▶ 播放笔记")
            if self.displayed_note and self.note_sentences:
                idx = min(self.note_sentence_idx, len(self.note_sentences) - 1)
                self.config.set_note_position(*self.displayed_note, self.note_sentences[idx]["start"])
            return
        self.is_playing = False
        self.play_btn.configure(text="▶ 播放")
        selection_end = getattr(self, "selection_end", None)
        self.selection_end = None
        if status == "failed":
            self._save_epub_bookmark()
            self._set_status("播放中断：" + (skipped[-1][1] if skipped else "未知错误"))
            return
        if status == "finished" and selection_end is not None:
            # 选区读完：光标放到选区之后那句，不翻页
            self.current_sentence_idx = min(selection_end + 1, len(self.current_sentences) - 1)
            self._highlight_epub(self.current_sentence_idx, save=True)
            self._set_status("选区读完" + (f"，跳过了 {len(skipped)} 句（点这里看）" if skipped else ""))
            return
        self._save_epub_bookmark()
        if status == "finished":
            if skipped:
                self._set_status(f"本章读完，跳过了 {len(skipped)} 句（点这里看）")
            if self.current_chapter_idx < len(self.parser.chapters) - 1:
                self._schedule_auto_next()

    def _schedule_auto_next(self):
        """章末自动翻页。带三重身份：切了章、换了书、又手动播了别的，都作废。"""
        self._cancel_auto_next()
        ident = (self.book_key, self.current_chapter_idx + 1, self.nav_token)
        self._auto_after_id = self.root.after(500, self._auto_next, ident)

    def _cancel_auto_next(self):
        if self._auto_after_id is not None:
            try:
                self.root.after_cancel(self._auto_after_id)
            except tk.TclError:
                pass
            self._auto_after_id = None

    def _auto_next(self, ident):
        self._auto_after_id = None
        book_key, next_chapter, token = ident
        if book_key != self.book_key or token != self.nav_token or self.active_session is not None:
            return
        if self._load_chapter(next_chapter):
            self._start_play()

    def _show_skipped_log(self):
        if not self.skipped_log:
            return
        lines = [f"第 {ch + 1} 章 第 {idx + 1} 句：{reason}" if ch >= 0 else f"笔记 第 {idx + 1} 句：{reason}"
                 for ch, idx, reason in self.skipped_log[-50:]]
        messagebox.showinfo("没读的句子", chr(10).join(lines))

    def _on_close(self):
        self._stop_play()
        self._stop_play_sandbox()
        if not self._save_displayed_note():
            if not messagebox.askyesno("笔记没保存",
                                       f"{self.config.last_error}\n\n仍然退出？未保存的笔记会丢。"):
                return
        self._cancel_auto_next()
        self._closed = True
        self.ai_manager.cancel_running()
        self.tts.quit()
        self.root.destroy()
