import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
from epub_parser import EpubParser
from tts_thread import TTSWorker, NEURAL_VOICES
from mp3_exporter import run_export_background
from config_manager import ConfigManager

class MainWindow:
    def __init__(self, root):
        self.root = root
        self.root.title("Native EPUB TTS Reader")
        self.root.geometry("1400x800")
        
        # Dark mode colors
        self.bg_color = "#1e1e1e"
        self.fg_color = "#d4d4d4"
        self.highlight_bg = "#b8860b" # Dark goldenrod
        self.highlight_fg = "#ffffff"
        
        self.root.configure(bg=self.bg_color)
        
        # Setup modern dark style
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background=self.bg_color)
        style.configure('TLabel', background=self.bg_color, foreground=self.fg_color)
        style.configure('TButton', background="#333333", foreground=self.fg_color, borderwidth=1)
        style.map('TButton', background=[('active', '#555555')])
        
        self.config = ConfigManager()
        self.parser = EpubParser()
        
        # EPUB State
        self.current_sentences = []
        self.current_chapter_idx = 0
        self.current_sentence_idx = 0
        self.is_playing = False
        self.loaded_file = None
        
        # Sandbox State
        self.sandbox_sentences = []
        self.sandbox_sentence_idx = 0
        self.is_playing_sandbox = False
        
        # Start TTS Worker
        self.tts = TTSWorker(
            highlight_callback=lambda idx: self.root.after(0, self._highlight_sentence, idx),
            chapter_done_callback=lambda: self.root.after(0, self._auto_next_chapter)
        )
        
        self._build_ui()
        self._setup_keybinds()
        self._load_voices()
        
        # Resume operations
        self._reload_last_session()
        self._reload_sandbox()

    def _build_ui(self):
        menubar = tk.Menu(self.root, bg=self.bg_color, fg=self.fg_color)
        file_menu = tk.Menu(menubar, tearoff=0, bg=self.bg_color, fg=self.fg_color)
        file_menu.add_command(label="Open EPUB...", command=self._open_file_dialog)
        
        self.recent_menu = tk.Menu(file_menu, tearoff=0, bg=self.bg_color, fg=self.fg_color)
        file_menu.add_cascade(label="Recent Files", menu=self.recent_menu)
        self._update_recent_menu()
        
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)
        self.root.config(menu=menubar)
        
        # Main Layout: 3-Pane PanedWindow
        paned = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg=self.bg_color, bd=0, sashwidth=4)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # -------------- LEFT PANE: TOC --------------
        left_frame = ttk.Frame(paned)
        paned.add(left_frame, minsize=200)
        ttk.Label(left_frame, text="Contents", font=("Arial", 12, "bold")).pack(anchor=tk.W, pady=5)
        
        self.toc_listbox = tk.Listbox(left_frame, bg="#2d2d2d", fg=self.fg_color, 
                                      selectbackground=self.highlight_bg, selectforeground=self.highlight_fg,
                                      borderwidth=0, highlightthickness=0,
                                      font=("Arial", 10), exportselection=False)
        self.toc_listbox.pack(fill=tk.BOTH, expand=True)
        self.toc_listbox.bind("<<ListboxSelect>>", self._on_toc_select)
        
        # -------------- MIDDLE PANE: EPUB Reader --------------
        mid_frame = ttk.Frame(paned)
        paned.add(mid_frame, minsize=400)
        
        ttk.Label(mid_frame, text="EPUB Reader", font=("Arial", 12, "bold")).pack(anchor=tk.W, pady=5)
        self.text_area = tk.Text(mid_frame, bg=self.bg_color, fg=self.fg_color, 
                                 font=("Microsoft YaHei", 12), wrap=tk.WORD, 
                                 padx=20, pady=20, borderwidth=0, highlightthickness=0)
        self.text_area.pack(fill=tk.BOTH, expand=True)
        self.text_area.tag_configure("highlight", background=self.highlight_bg, foreground=self.highlight_fg)
        
        # Prevent manual typing but allow highlight and copy
        self.text_area.bind("<Key>", lambda e: "break")
        
        epub_controls = ttk.Frame(mid_frame)
        epub_controls.pack(fill=tk.X, pady=10)
        
        ttk.Button(epub_controls, text="<< Prev", command=self._prev_chapter).pack(side=tk.LEFT, padx=2)
        self.play_btn = ttk.Button(epub_controls, text="Play", command=self._toggle_play)
        self.play_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(epub_controls, text="Next >>", command=self._next_chapter).pack(side=tk.LEFT, padx=2)
        
        export_btn = ttk.Button(epub_controls, text="Export MP3", command=self._export_mp3_ui)
        export_btn.pack(side=tk.LEFT, padx=15)
        
        # -------------- RIGHT PANE: Chapter Note --------------
        right_frame = ttk.Frame(paned)
        paned.add(right_frame, minsize=350)
        
        title_frame = ttk.Frame(right_frame)
        title_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(title_frame, text="Chapter Note", font=("Arial", 12, "bold")).pack(side=tk.LEFT)
        ttk.Button(title_frame, text="AI Prompt Guide", command=self._show_script_guide).pack(side=tk.RIGHT, padx=2)
        
        # This one is editable so users can paste text freely
        self.sandbox_area = tk.Text(right_frame, bg="#252526", fg=self.fg_color, 
                                 font=("Microsoft YaHei", 12), wrap=tk.WORD, 
                                 padx=20, pady=20, borderwidth=0, highlightthickness=0)
        self.sandbox_area.pack(fill=tk.BOTH, expand=True)
        self.sandbox_area.tag_configure("highlight", background=self.highlight_bg, foreground=self.highlight_fg)
        
        sandbox_controls = ttk.Frame(right_frame)
        sandbox_controls.pack(fill=tk.X, pady=10)
        
        self.sandbox_play_btn = ttk.Button(sandbox_controls, text="Play Note", command=self._toggle_sandbox_play)
        self.sandbox_play_btn.pack(side=tk.LEFT, padx=2)
        
        ttk.Button(sandbox_controls, text="Save", command=self._save_chapter_note).pack(side=tk.LEFT, padx=2)
        
        ttk.Button(sandbox_controls, text="Clear", command=self._clear_sandbox).pack(side=tk.RIGHT, padx=2)
        
        # -------------- GLOBAL SETTINGS (Bottom) --------------
        settings_frame = ttk.Frame(self.root)
        settings_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(settings_frame, text="Voice:").pack(side=tk.LEFT, padx=5)
        self.voice_var = tk.StringVar()
        self.voice_combo = ttk.Combobox(settings_frame, textvariable=self.voice_var, state="readonly", width=30)
        self.voice_combo.pack(side=tk.LEFT, padx=5)
        self.voice_combo.bind("<<ComboboxSelected>>", self._on_settings_change)
        
        ttk.Label(settings_frame, text="Speed:").pack(side=tk.LEFT, padx=5)
        self.speed_var = tk.IntVar(value=self.config.config.get("speech_rate", 200))
        self.speed_slider = ttk.Scale(settings_frame, from_=50, to=400, variable=self.speed_var, 
                                      command=lambda v: self._on_settings_change(None))
        self.speed_slider.pack(side=tk.LEFT, padx=5)
        
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_keybinds(self):
        self.root.bind("<space>", lambda e: self._toggle_play())
        self.root.bind("<Left>", lambda e: self._prev_chapter())
        self.root.bind("<Right>", lambda e: self._next_chapter())

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

    def _on_settings_change(self, event):
        voice_name = self.voice_var.get()
        voice_id = self.voice_map.get(voice_name)
        rate = int(float(self.speed_var.get()))
        self.config.set_voice_and_rate(voice_id, rate)
        self.tts.set_rate(rate)

    def _open_file_dialog(self):
        file_path = filedialog.askopenfilename(filetypes=[("EPUB Files", "*.epub")])
        if file_path:
            self._load_epub(file_path)

    def _reload_last_session(self):
        last_file = self.config.config.get("last_file")
        if last_file and os.path.exists(last_file):
            self._load_epub(last_file)

    def _reload_sandbox(self):
        text, idx = self.config.load_sandbox()
        if text:
            self.sandbox_area.delete("1.0", tk.END)
            self.sandbox_area.insert("1.0", text)
            self.sandbox_sentence_idx = idx

    def _load_epub(self, file_path):
        self._stop_play()
        if self.parser.load_epub(file_path):
            self.loaded_file = file_path
            self.config.add_recent_file(file_path)
            self._update_recent_menu()
            
            self.toc_listbox.delete(0, tk.END)
            for ch in self.parser.get_chapter_list():
                self.toc_listbox.insert(tk.END, ch['title'])
                
            bookmark = self.config.get_bookmark(file_path)
            chap_idx = bookmark.get("chapter_idx", 0)
            target_sentence = bookmark.get("sentence_idx", 0)
            
            if chap_idx >= len(self.parser.chapters):
                chap_idx = 0
                target_sentence = 0
            
            self._load_chapter(chap_idx)
            self.current_sentence_idx = target_sentence
            self._see_line_centered(self.text_area, f"sentence_{target_sentence}")
        else:
            messagebox.showerror("Error", "Failed to load EPUB file.")

    def _load_chapter(self, index):
        self._stop_play()
        self._stop_play_sandbox()
        if not self.loaded_file: return
        
        # Auto-save current note before jumping unless it's the first load
        if hasattr(self, 'current_chapter_idx') and self.current_chapter_idx < len(self.parser.chapters):
            self._save_chapter_note()
            
        if index < 0 or index >= len(self.parser.chapters): return
        
        self.current_chapter_idx = index
        self.current_sentence_idx = 0
        self.current_sentences = self.parser.get_chapter_text(index)
        
        # Load up chapter notes
        note_text = self.config.load_chapter_note(self.loaded_file, index)
        self.sandbox_area.delete("1.0", tk.END)
        if note_text:
            self.sandbox_area.insert("1.0", note_text)
        
        # Determine Book Name for potential exporting later
        self.current_book_name = os.path.basename(self.loaded_file).replace(".epub", "")
        
        self.toc_listbox.selection_clear(0, tk.END)
        self.toc_listbox.selection_set(index)
        self.toc_listbox.see(index)
        
        self.text_area.config(state=tk.NORMAL)
        self.text_area.delete("1.0", tk.END)
        
        for i, sentence in enumerate(self.current_sentences):
            start_index = self.text_area.index(tk.INSERT)
            self.text_area.insert(tk.END, sentence)
            end_index = self.text_area.index(tk.INSERT)
            self.text_area.insert(tk.END, "\n\n")
            
            self.text_area.tag_add(f"sentence_{i}", start_index, end_index)
            
        self.text_area.config(state=tk.DISABLED)
        
        self.config.set_bookmark(self.loaded_file, self.current_chapter_idx, 0)

    def _on_toc_select(self, event):
        selection = self.toc_listbox.curselection()
        if selection:
            idx = selection[0]
            if idx != self.current_chapter_idx:
                self._load_chapter(idx)

    def _prev_chapter(self):
        if self.current_chapter_idx > 0:
            self._load_chapter(self.current_chapter_idx - 1)

    def _next_chapter(self):
        if self.current_chapter_idx < len(self.parser.chapters) - 1:
            self._load_chapter(self.current_chapter_idx + 1)

    # ---------- EPUB Playback Controls ----------
    def _toggle_play(self):
        if self.is_playing_sandbox:
            self._stop_play_sandbox()
            
        if not self.loaded_file: return
        if not self.current_sentences: return
        
        if self.is_playing:
            self._stop_play()
        else:
            self._start_play()

    def _start_play(self):
        # Override start position if user highlighted a specific sentence
        try:
            ranges = self.text_area.tag_ranges("sel")
            if ranges:
                for tag in self.text_area.tag_names(ranges[0]):
                    if tag.startswith("sentence_"):
                        self.current_sentence_idx = int(tag.split("_")[1])
                        break
                self.text_area.tag_remove("sel", "1.0", tk.END)
        except tk.TclError:
            pass

        if self.current_sentence_idx >= len(self.current_sentences):
            self.current_sentence_idx = 0
            
        self.is_playing = True
        self.is_playing_sandbox = False
        self.play_btn.config(text="Pause")
        
        voice_name = self.voice_var.get()
        voice_id = self.voice_map.get(voice_name)
        rate = int(float(self.speed_var.get()))
        
        self.tts.play(voice_id, rate, self.current_sentences, self.current_sentence_idx)

    def _stop_play(self):
        if self.is_playing:
            self.is_playing = False
            self.play_btn.config(text="Play")
            self.tts.stop()
            if self.loaded_file:
                self.config.set_bookmark(self.loaded_file, self.current_chapter_idx, self.current_sentence_idx)

    # ---------- Sandbox Playback Controls ----------
    def _toggle_sandbox_play(self):
        if self.is_playing:
            self._stop_play()
            
        if self.is_playing_sandbox:
            self._stop_play_sandbox()
        else:
            self._start_play_sandbox()
            
    def _start_play_sandbox(self):
        # Read the raw text
        raw_text = self.sandbox_area.get("1.0", tk.END).strip()
        if not raw_text:
            return
            
        # Check if user highlighted a specific sentence
        selected_idx = None
        try:
            ranges = self.sandbox_area.tag_ranges("sel")
            if ranges:
                for tag in self.sandbox_area.tag_names(ranges[0]):
                    if tag.startswith("sentence_"):
                        selected_idx = int(tag.split("_")[1])
                        break
                self.sandbox_area.tag_remove("sel", "1.0", tk.END)
        except tk.TclError:
            pass
            
        # Parse it into sentences with Multi-Voice support
        self.sandbox_sentences = self._parse_sandbox_text(raw_text)
        
        # Reset the sandbox text area to embed highlight tags
        # We temporarily disable edits to render the tags cleanly
        self.sandbox_area.delete("1.0", tk.END)
        for i, sentence_obj in enumerate(self.sandbox_sentences):
            text_str = sentence_obj['text']
            
            start_index = self.sandbox_area.index(tk.INSERT)
            self.sandbox_area.insert(tk.END, text_str)
            end_index = self.sandbox_area.index(tk.INSERT)
            
            if sentence_obj['voice_id'] != 'SKIP':
                self.sandbox_area.insert(tk.END, "\n\n")
            else:
                self.sandbox_area.insert(tk.END, "\n")
                
            self.sandbox_area.tag_add(f"sentence_{i}", start_index, end_index)
            
        if selected_idx is not None:
            self.sandbox_sentence_idx = selected_idx
            
        if self.sandbox_sentence_idx >= len(self.sandbox_sentences):
            self.sandbox_sentence_idx = 0
            
        self.is_playing_sandbox = True
        self.sandbox_play_btn.config(text="Pause Sandbox")
        
        voice_name = self.voice_var.get()
        voice_id = self.voice_map.get(voice_name)
        rate = int(float(self.speed_var.get()))
        
        self.tts.play(voice_id, rate, self.sandbox_sentences, self.sandbox_sentence_idx)

    def _stop_play_sandbox(self):
        if self.is_playing_sandbox:
            self.is_playing_sandbox = False
            self.sandbox_play_btn.config(text="Play Sandbox")
            self.tts.stop()
            raw_text = self.sandbox_area.get("1.0", tk.END).strip()
            self.config.save_sandbox(raw_text, self.sandbox_sentence_idx)

    def _save_chapter_note(self):
        if not self.loaded_file: return
        raw_text = self.sandbox_area.get("1.0", tk.END).strip()
        self.config.save_chapter_note(self.loaded_file, self.current_chapter_idx, raw_text)

    def _parse_sandbox_text(self, raw_text):
        import re
        TAG_TO_VOICE = {
            'yunxi': 'zh-CN-YunxiNeural',
            'xiaoxiao': 'zh-CN-XiaoxiaoNeural',
            'yunyang': 'zh-CN-YunyangNeural',
            'guy': 'en-US-GuyNeural',
            'aria': 'en-US-AriaNeural',
            'jenny': 'en-US-JennyNeural'
        }
        parsed = []
        active_override = None
        tokens = re.split(r'(\{(?:/?)[a-zA-Z]+\})', raw_text)
        for token in tokens:
            if not token: continue
            if re.match(r'^\{/([a-zA-Z]+)\}$', token.strip()):
                active_override = None
                parsed.append({'text': token, 'voice_id': 'SKIP'})
                continue
            match_open = re.match(r'^\{([a-zA-Z]+)\}$', token.strip())
            if match_open:
                tag_name = match_open.group(1).lower()
                if tag_name in TAG_TO_VOICE: active_override = TAG_TO_VOICE[tag_name]
                parsed.append({'text': token, 'voice_id': 'SKIP'})
                continue
            if not token.strip():
                parsed.append({'text': token, 'voice_id': 'SKIP'})
                continue
            sub_sentences = self.parser.split_into_sentences(token)
            for s in sub_sentences: parsed.append({'text': s, 'voice_id': active_override})
        return parsed

    def _export_mp3_ui(self):
        if not self.loaded_file or self.current_chapter_idx is None:
            messagebox.showerror("Error", "No chapter loaded!")
            return
            
        voice_display = self.voice_combo.get()
        voice_id = self.voice_map.get(voice_display)
        
        if voice_id not in NEURAL_VOICES:
            messagebox.showerror("Error", "MP3 Export exclusively supports high-quality Online Native Voices.\n\nPlease select one of the top Narrator or Neural Voices to enable exports.")
            return
            
        raw_notes = self.sandbox_area.get("1.0", tk.END)
        notes_parsed = self._parse_sandbox_text(raw_notes) if raw_notes.strip() else []
        epub_s = self.current_sentences
        
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
            chap_name = self.parser.chapters[self.current_chapter_idx]['title']
        except Exception:
            chap_name = f"Chapter {self.current_chapter_idx + 1}"
        
        def on_status_update(status_text):
            self.root.after(0, lambda: lbl.config(text=status_text))
            
        def on_complete(success):
            if success:
                self.root.after(0, lambda: [lbl.config(text="Export completed! Check Audio/ folder.", foreground="lightgreen"), self.root.after(3000, lambda: [win.grab_release(), win.destroy()])])
            else:
                self.root.after(0, lambda: [lbl.config(text="Export failed.", foreground="red"), self.root.after(3000, lambda: [win.grab_release(), win.destroy()])])
                
        run_export_background(
            book_name=getattr(self, 'current_book_name', "Unknown Book"),
            chapter_name=chap_name, 
            epub_sentences=epub_s, 
            notes_objects=notes_parsed, 
            fallback_voice_id=voice_id, 
            status_callback=on_status_update, 
            done_callback=on_complete
        )

    def _clear_sandbox(self):
        self._stop_play_sandbox()
        self.sandbox_area.delete("1.0", tk.END)
        self.sandbox_sentence_idx = 0
        self.sandbox_sentences = []
        if self.loaded_file:
            self.config.save_chapter_note(self.loaded_file, self.current_chapter_idx, "")
            
    def _show_script_guide(self):
        guide_win = tk.Toplevel(self.root)
        guide_win.title("Multi-Voice AI Prompt Guide")
        guide_win.geometry("550x450")
        guide_win.configure(bg="#1e1e1e")
        guide_win.transient(self.root)
        
        lbl = ttk.Label(guide_win, text="How to Create Audiobook Scripts", font=("Arial", 12, "bold"))
        lbl.pack(pady=10)
        
        info = ("You can assign specific characters to different voices by wrapping their "
                "dialogue in special tags. The default narrator voice (selected at the bottom) "
                "will read any normal text natively.\n\n"
                "Supported Tags:\n"
                "{yunxi} ... {/yunxi} (Male, Chinese)\n"
                "{xiaoxiao} ... {/xiaoxiao} (Female, Chinese)\n"
                "{yunyang} ... {/yunyang} (Male Narrator, Chinese)\n"
                "{guy} ... {/guy} (Male, English)\n"
                "{aria} ... {/aria} (Female, English)\n"
                "{jenny} ... {/jenny} (Female Narrator, English)\n\n"
                "Sample AI Prompt to generate your script (Copy this!):")
        
        ttk.Label(guide_win, text=info, wraplength=510, justify=tk.LEFT).pack(padx=20, fill=tk.X)
        
        prompt_text = (
            "以后都用这样的 format。小说家是年轻女性，每次她说话必须用 {xiaoxiao} 内容 {/xiaoxiao}，"
            "老黄说话是必须用 {yunxi} 内容 {/yunxi} 的格式。其它内容是这个谈话节目的host，用host的口吻说（不需要加标签）。"
            "host永远先快速总结这一章内容，然后提问。"
        )
        
        text_area = tk.Text(guide_win, height=6, bg="#252526", fg=self.fg_color, 
                            font=("Microsoft YaHei", 10), wrap=tk.WORD, borderwidth=1)
        text_area.insert("1.0", prompt_text)
        text_area.configure(state=tk.DISABLED) # Read-only but gracefully copyable
        text_area.pack(padx=20, pady=10, fill=tk.BOTH, expand=True)
        
        ttk.Button(guide_win, text="Close", command=guide_win.destroy).pack(pady=10)

    # ---------- Global Callbacks ----------
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

    def _highlight_sentence(self, index):
        if self.is_playing_sandbox:
            self.sandbox_sentence_idx = index
            self.sandbox_area.tag_remove("highlight", "1.0", tk.END)
            if index < len(self.sandbox_sentences):
                tag_name = f"sentence_{index}"
                self.sandbox_area.tag_add("highlight", f"{tag_name}.first", f"{tag_name}.last")
                self._see_line_centered(self.sandbox_area, tag_name)
        else:
            self.current_sentence_idx = index
            self.text_area.tag_remove("highlight", "1.0", tk.END)
            if index < len(self.current_sentences):
                tag_name = f"sentence_{index}"
                self.text_area.tag_add("highlight", f"{tag_name}.first", f"{tag_name}.last")
                self._see_line_centered(self.text_area, tag_name)
                
                # Auto-save EPUB bookmark
                if self.loaded_file:
                    self.config.set_bookmark(self.loaded_file, self.current_chapter_idx, self.current_sentence_idx)

    def _auto_next_chapter(self):
        if self.is_playing:
            self.is_playing = False
            self.play_btn.config(text="Play")
            if self.current_chapter_idx < len(self.parser.chapters) - 1:
                self._load_chapter(self.current_chapter_idx + 1)
                self.root.after(500, self._start_play)
        elif self.is_playing_sandbox:
            # Sandbox is finished reading
            self._stop_play_sandbox()

    def _on_close(self):
        self._stop_play()
        self._stop_play_sandbox()
        self._save_chapter_note()
        self.tts.quit()
        self.root.destroy()
