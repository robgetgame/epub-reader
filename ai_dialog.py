"""AI 章节讲解对话窗 + 对话记录存储。设计文档 §2.3 / §5.3 / §5.4 / A.2 / A.5，第 8 步（2026-09-12）。

请求身份（§2.3）：每个请求带 note_id + 发出时的笔记 revision + 正文快照。完成时只有
「右栏还显示这一章 且 revision 没变」才写右栏；否则稿子留在窗口里，给一个写明目标章的
「覆盖」按钮，点的时候再比一次 revision。「放进笔记」同样只认请求的 note_id。

生命周期（A.5）：关窗只是不再收 delta；后台线程跑到 finally 才释放 AiTaskManager 的占用，
ai_done 永远先到主窗口记账，再决定要不要更新对话窗。
"""

import os
import time
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

import ai_chat
from ai_chat import AiRequest, AiError
from json_store import JsonStore

BG = "#1b1b1b"
FG = "#d4d4d4"
FG_DIM = "#8a8a8a"
ACCENT = "#7a5c1e"
ACCENT_HOVER = "#8f6d25"

SUMMARY_MAX_TOKENS_CAP = 8192
CHAT_MAX_TOKENS = 2048
DETECT_MAX_TOKENS = 120


# ====================================================================
# ai_chat.json
# ====================================================================

def _chat_defaults():
    return {"schema": 1, "books": {}}


def _chat_validate(data):
    if not isinstance(data, dict):
        return ["顶层不是对象"]
    books = data.get("books", {})
    if not isinstance(books, dict):
        return ["books 不是对象"]
    for book, chapters in books.items():
        if not isinstance(chapters, dict):
            return [f"books[{os.path.basename(str(book))}] 不是对象"]
        for ck, turns in chapters.items():
            if not isinstance(turns, list) or not all(isinstance(t, dict) and isinstance(t.get("content", ""), str) for t in turns):
                return [f"{os.path.basename(str(book))}/{ck} 的记录形状不对"]
    return []


class ChatStore:
    """对话记录按 (书, 章) 存；每轮完成时写一次盘，不按 delta 写。逐请求的 usage 也在这里 ——
    bookmarks.json 的 ai_usage 只是可重建的汇总（A.5）。"""

    def __init__(self, path, read_only=False):
        self.store = JsonStore(path, _chat_defaults, _chat_validate)
        self.store.read_only = read_only
        if read_only:
            self.store.load_readonly()
        else:
            self.store.load()
        if not isinstance(self.store.data.get("books"), dict):
            self.store.data["books"] = {}

    @property
    def load_error(self):
        return self.store.load_error

    def history(self, book_key, chapter_key):
        return list(self.store.data["books"].get(book_key, {}).get(str(chapter_key), []))

    def append(self, book_key, chapter_key, entry):
        turns = self.store.data["books"].setdefault(book_key, {}).setdefault(str(chapter_key), [])
        turns.append(entry)
        return self.store.save()

    def latest_summary(self, book_key, chapter_key):
        for t in reversed(self.history(book_key, chapter_key)):
            if t.get("is_summary") and t.get("status") == "finished" and t.get("content"):
                return t["content"]
        return None

    def usage_totals(self):
        """从所有记录重算累计（A.5：汇总可重建）。"""
        p = c = cached = 0
        cost = 0.0
        unknown = 0
        for chapters in self.store.data["books"].values():
            for turns in chapters.values():
                for t in turns:
                    u = t.get("usage")
                    if not u:
                        if t.get("role") == "assistant":
                            unknown += 1
                        continue
                    p += u.get("prompt_tokens") or 0
                    c += u.get("completion_tokens") or 0
                    cached += u.get("cached_tokens") or 0
                    if u.get("cost_usd") is None:
                        unknown += 1
                    else:
                        cost += u["cost_usd"]
        return {"prompt_tokens": p, "completion_tokens": c, "cached_tokens": cached, "cost_usd": cost,
                "unknown_cost_requests": unknown}


def _usage_entry(usage):
    p, c, cached, cost = ai_chat.usage_summary(usage)
    if p is None and c is None and cost is None:
        return None
    return {"prompt_tokens": p, "completion_tokens": c, "cached_tokens": cached, "cost_usd": cost}


# ====================================================================
# 对话窗
# ====================================================================

class ChapterChatDialog(ctk.CTkToplevel):
    def __init__(self, app, note_id, chapter_title, book_title, chapter_text):
        super().__init__(app.root)
        self.app = app
        self.note_id = note_id                  # 这个窗口永远只属于这一章
        self.book_key, self.chapter_key = note_id
        self.chapter_text = chapter_text        # 快照
        self.active_request = None              # 这个窗口还在收内容的请求
        self.pending_summary = None             # 生成完但没写进右栏的稿子（笔记被改过 / 切走了）
        self.pending_revision = None
        self._stream_tag = None
        self._closed = False

        self.title(f"AI 讲解 · {book_title} · {chapter_title}")
        self.geometry("820x640")
        self.minsize(600, 420)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # 顶栏
        top = ctk.CTkFrame(self, fg_color="#202020", corner_radius=0)
        top.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(top, text=f"{book_title}", font=ctk.CTkFont(size=13, weight="bold")).pack(side="left", padx=(14, 4), pady=8)
        ctk.CTkLabel(top, text=f"· {chapter_title}", text_color=FG_DIM).pack(side="left", padx=4)
        self.kind_label_var = tk.StringVar(value="")
        ctk.CTkLabel(top, textvariable=self.kind_label_var, text_color=FG_DIM).pack(side="right", padx=14)
        self._refresh_kind_label()

        # 对话区
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.grid(row=1, column=0, sticky="nsew", padx=(10, 4), pady=6)
        self.text = tk.Text(wrap, bg="#1e1e20", fg=FG, wrap=tk.WORD, padx=14, pady=12, borderwidth=0,
                            highlightthickness=0, font=(app.font_label_to_family.get(app.font_var.get(), "Microsoft YaHei"), 12),
                            cursor="arrow", spacing3=4)
        sb = ctk.CTkScrollbar(wrap, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("user", foreground="#9cc9d8", lmargin1=40, lmargin2=40)
        self.text.tag_configure("assistant", foreground=FG)
        self.text.tag_configure("system", foreground=FG_DIM)
        self.text.tag_configure("error", foreground="#e0a040")
        self.text.tag_configure("role", foreground=FG_DIM, font=("Microsoft YaHei", 9))
        self.text.tag_configure("action", foreground="#7aa2d8", underline=True)
        self.text.bind("<Key>", lambda e: "break")
        self.text.bind("<Button-1>", self._on_text_click)

        # 输入区
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 6))
        bottom.grid_columnconfigure(0, weight=1)
        self.input = ctk.CTkTextbox(bottom, height=64, wrap="word")
        self.input.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.input.bind("<Control-Return>", lambda e: (self._send(), "break")[1])
        btns = ctk.CTkFrame(bottom, fg_color="transparent")
        btns.grid(row=0, column=1, sticky="ns")
        self.send_btn = ctk.CTkButton(btns, text="发送 (Ctrl+Enter)", width=130, command=self._send)
        self.send_btn.pack(fill="x", pady=(0, 4))
        self.gen_btn = ctk.CTkButton(btns, text="生成讲解", width=130, fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._generate)
        self.gen_btn.pack(fill="x")

        # 状态栏
        foot = ctk.CTkFrame(self, fg_color="#202020", corner_radius=0)
        foot.grid(row=3, column=0, sticky="ew")
        self.usage_var = tk.StringVar(value="")
        ctk.CTkLabel(foot, textvariable=self.usage_var, text_color=FG_DIM).pack(side="left", padx=14, pady=4)
        self.state_var = tk.StringVar(value="空闲")
        ctk.CTkLabel(foot, textvariable=self.state_var, text_color=FG_DIM).pack(side="right", padx=14, pady=4)

        self._render_history()
        self._refresh_usage()
        self._maybe_detect_kind()
        self._refresh_buttons()

    # ---------- 显示 ----------

    def _append(self, text, tag, role_label=None, action=None):
        self.text.configure(state=tk.NORMAL)
        if role_label:
            self.text.insert(tk.END, role_label + "\n", ("role",))
        start = self.text.index(tk.END + "-1c")
        self.text.insert(tk.END, text, (tag,))
        end = self.text.index(tk.END + "-1c")
        if action:
            self.text.insert(tk.END, "\n")
            self.text.insert(tk.END, action[0], ("action", f"act_{action[1]}"))
        self.text.insert(tk.END, "\n\n")
        self.text.configure(state=tk.DISABLED)
        self.text.see(tk.END)
        return start, end

    def _render_history(self):
        hist = self.app.chat_store.history(self.book_key, self.chapter_key)
        if not hist:
            self._append("这一章还没有讲解稿。点「生成讲解」写一份进右栏笔记，或者直接提问。", "system")
            return
        for i, t in enumerate(hist):
            if t.get("role") == "user":
                self._append(t["content"], "user", "你")
            elif t.get("role") == "assistant":
                label = "讲解稿" if t.get("is_summary") else "AI"
                if t.get("status") not in (None, "finished"):
                    label += f"（{t.get('status')}）"
                self._append(t["content"], "assistant", label, action=("放进笔记", f"h{i}"))
        self._actions = {f"h{i}": t["content"] for i, t in enumerate(hist) if t.get("role") == "assistant"}

    def _on_text_click(self, event):
        idx = self.text.index(f"@{event.x},{event.y}")
        for tag in self.text.tag_names(idx):
            if tag.startswith("act_"):
                key = tag[4:]
                if key == "overwrite":
                    self._overwrite_pending()
                else:
                    content = getattr(self, "_actions", {}).get(key)
                    if content:
                        self._put_into_note(content)
                return "break"
        return None

    def _refresh_kind_label(self):
        meta = self.app.config.get_book_meta(self.book_key)
        kind = meta.get("kind")
        label = {"fiction": "小说", "nonfiction": "非虚构"}.get(kind, "未判定")
        src = {"auto": "自动", "manual": "手选"}.get(meta.get("kind_source"), "")
        self.kind_label_var.set(f"类型：{label}{'（' + src + '）' if src and kind else ''}   模型：{self.app.ai_cfg['model']}")

    def _refresh_usage(self):
        t = self.app.chat_store.usage_totals()
        s = f"累计 {t['prompt_tokens'] + t['completion_tokens']:,} tokens · ${t['cost_usd']:.4f}"
        if t["unknown_cost_requests"]:
            s += f" · {t['unknown_cost_requests']} 次费用未知"
        self.usage_var.set(s)

    def _refresh_buttons(self):
        busy = self.app.ai_manager.running is not None
        kind = self.app.config.get_book_meta(self.book_key).get("kind")
        no_key = not self.app.ai_cfg["api_key"]
        self.send_btn.configure(state="disabled" if (busy or no_key) else "normal")
        self.gen_btn.configure(state="disabled" if (busy or no_key or kind is None) else "normal")
        if no_key:
            self.state_var.set("OPENROUTER_KEY 没有设置（设置后需重启程序）")
        elif busy:
            self.state_var.set("上一个请求还在结束…" if self.active_request is None else "进行中…")
        elif kind is None:
            self.state_var.set("先在主窗口选择书的类型")
        else:
            self.state_var.set("空闲")

    # ---------- 类型判定 ----------

    def _maybe_detect_kind(self):
        if self.app.config.get_book_meta(self.book_key).get("kind"):
            return
        if not self.app.ai_cfg["api_key"]:
            self._append("OPENROUTER_KEY 没有设置，无法判定类型和生成讲解。请在主窗口手选类型，或设置 key 后重启。", "error")
            return
        titles = [c.title for c in self.app.parser.chapters]
        samples = []
        for i, c in enumerate(self.app.parser.chapters):
            if len(samples) >= 2:
                break
            t = self.app.parser.get_chapter_text(i)
            if len(t.strip()) > 200:
                samples.append(t)
        req = AiRequest(purpose="detect", note_id=None, note_revision=0, model=self.app.ai_cfg["model"])
        client = self.app.ai_client

        def fn(request):
            try:
                text, usage, _ = client.complete(request, ai_chat.build_detect_messages(titles, samples),
                                                 DETECT_MAX_TOKENS, json_mode=True)
                result = ai_chat.parse_detect_result(text)
                self.app.events.put(("ai_detect", request.id, self.book_key, result, None, usage))
            except AiError as e:
                self.app.events.put(("ai_detect", request.id, self.book_key, None, str(e), None))

        if self.app.ai_manager.start(req, fn):
            self.active_request = req
            self.state_var.set("正在判定书的类型…")
            self._append("正在判定这本书是小说还是非虚构…", "system")
        self._refresh_buttons()

    def on_detect(self, rid, result, error, usage):
        if self.active_request is not None and self.active_request.id == rid:
            self.active_request = None
        if result:
            self._append(f"判定：{'小说' if result['kind'] == 'fiction' else '非虚构'}（语言 {result['language']}）。"
                         "判错了可以在主窗口的类型下拉里改。", "system")
        else:
            self._append(f"无法判定类型{'：' + error if error else '（模型输出不合法）'}。请在主窗口选择类型。", "error")
        self._refresh_kind_label()
        self._refresh_buttons()

    # ---------- 生成讲解 ----------

    def _generate(self):
        if self.app.ai_manager.running is not None:
            return
        kind = self.app.config.get_book_meta(self.book_key).get("kind")
        if kind is None:
            return
        if self.app.displayed_note == self.note_id and self.app._note_text().strip():
            if not messagebox.askyesno("生成讲解", "右栏笔记不是空的。生成完成后会用讲解稿替换它，继续？", parent=self):
                return
        elif self.app.displayed_note != self.note_id and self.app.config.get_note(*self.note_id).strip():
            if not messagebox.askyesno("生成讲解", "这一章已有笔记。生成完成后会替换它，继续？", parent=self):
                return
        if not self.chapter_text.strip():
            self._append("这一章没有正文，不生成。", "error")
            return

        language = self.app.config.get_book_meta(self.book_key).get("language")
        prev = self._previous_summary()
        target = ai_chat.summary_target(self.chapter_text, language)
        messages = ai_chat.build_summary_messages(kind, language, self.chapter_text, prev, target)
        # 下限 2048：目标字数 × 3 对短章太小；上限 8192（A.5：截断要显式处理，不靠猜）
        max_tokens = max(2048, min(target * 3, SUMMARY_MAX_TOKENS_CAP))
        ok, est, limit = ai_chat.budget_ok(self.app.ai_cfg["model"], messages, max_tokens)
        if not ok:
            self._append(f"这一章太长：估算 {est:,} tokens + 输出 {max_tokens:,} 超过模型上限 {limit:,}。", "error")
            return
        req = AiRequest(purpose="summary", note_id=self.note_id,
                        note_revision=self.app.note_revisions.get(self.note_id, 0),
                        model=self.app.ai_cfg["model"], kind=kind, chapter_text=self.chapter_text)
        self._start_stream(req, messages, max_tokens, "讲解稿")

    def _previous_summary(self):
        try:
            prev_idx = int(self.chapter_key) - 1
        except ValueError:
            return None
        if prev_idx < 0:
            return None
        return self.app.chat_store.latest_summary(self.book_key, str(prev_idx))

    # ---------- 追问 ----------

    def _send(self):
        if self.app.ai_manager.running is not None:
            return
        q = self.input.get("1.0", "end-1c").strip()
        if not q:
            return
        summary = self.app.chat_store.latest_summary(self.book_key, self.chapter_key)
        history = self.app.chat_store.history(self.book_key, self.chapter_key)
        messages = ai_chat.build_chat_messages(self.chapter_text, summary, history, q, self.app.ai_cfg["history_turns"])
        ok, est, limit = ai_chat.budget_ok(self.app.ai_cfg["model"], messages, CHAT_MAX_TOKENS)
        if not ok:
            self._append(f"上下文太长：估算 {est:,} tokens 超过模型上限 {limit:,}。", "error")
            return
        self.input.delete("1.0", tk.END)
        self._append(q, "user", "你")
        self.app.chat_store.append(self.book_key, self.chapter_key,
                                   {"role": "user", "content": q, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
        req = AiRequest(purpose="chat", note_id=self.note_id,
                        note_revision=self.app.note_revisions.get(self.note_id, 0),
                        model=self.app.ai_cfg["model"], chapter_text=self.chapter_text)
        self._start_stream(req, messages, CHAT_MAX_TOKENS, "AI")

    # ---------- 流 ----------

    def _start_stream(self, req, messages, max_tokens, label):
        client = self.app.ai_client
        events = self.app.events

        def fn(request):
            client.stream_chat(request, messages, max_tokens, events)

        if not self.app.ai_manager.start(req, fn):
            self._refresh_buttons()
            return
        self.active_request = req
        self.text.configure(state=tk.NORMAL)
        self.text.insert(tk.END, label + "\n", ("role",))
        self._stream_start = self.text.index(tk.END + "-1c")
        self.text.configure(state=tk.DISABLED)
        self._stream_buf = []
        self.state_var.set("生成中…" if req.purpose == "summary" else "回答中…")
        self._refresh_buttons()

    def on_delta(self, rid, text):
        if self._closed or self.active_request is None or self.active_request.id != rid:
            return
        self._stream_buf.append(text)
        self.text.configure(state=tk.NORMAL)
        self.text.insert(tk.END, text, ("assistant",))
        self.text.configure(state=tk.DISABLED)
        self.text.see(tk.END)

    def on_done(self, rid, status, text, usage, finish, error):
        """主窗口已经记过账才转到这里。"""
        req = self.active_request
        if self._closed or req is None or req.id != rid:
            return
        self.active_request = None
        entry = {"role": "assistant", "content": text, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "request_id": rid, "status": status, "is_summary": req.purpose == "summary",
                 "usage": _usage_entry(usage), "model": req.model}
        self.app.chat_store.append(self.book_key, self.chapter_key, entry)
        self._refresh_usage()

        if status == "failed":
            self._finish_stream_block(f"\n[失败：{error}]", "error")
        elif status == "cancelled":
            self._finish_stream_block("\n[已取消]", "system")
        elif status == "truncated":
            self._finish_stream_block("\n[输出被截断（到达长度上限），没有写入笔记]", "error", action=("放进笔记", f"r{rid}"), content=text)
        else:
            if req.purpose == "summary":
                self._deliver_summary(req, text)
            else:
                self._finish_stream_block("", "assistant", action=("放进笔记", f"r{rid}"), content=text)
        self._refresh_buttons()

    def on_finished(self, rid):
        self._refresh_buttons()

    def _finish_stream_block(self, tail, tag, action=None, content=None):
        self.text.configure(state=tk.NORMAL)
        if tail:
            self.text.insert(tk.END, tail, (tag,))
        if action:
            self.text.insert(tk.END, "\n")
            self.text.insert(tk.END, action[0], ("action", f"act_{action[1]}"))
            self._actions = getattr(self, "_actions", {})
            self._actions[action[1]] = content
        self.text.insert(tk.END, "\n\n")
        self.text.configure(state=tk.DISABLED)
        self.text.see(tk.END)

    # ---------- 写进笔记（§2.3） ----------

    def _deliver_summary(self, req, text):
        app = self.app
        current_rev = app.note_revisions.get(self.note_id, 0)
        if app.displayed_note == self.note_id and current_rev == req.note_revision:
            app._mutate_note(text, source="ai_replace")
            self._finish_stream_block(f"\n[已写入右栏笔记，{len(text)} 字]", "system")
            return
        # 笔记在生成期间被改过，或者右栏已经切到别的章：留在这里，让使用者再决定一次
        self.pending_summary = text
        self.pending_revision = current_rev
        why = "右栏已切到别的章" if app.displayed_note != self.note_id else "笔记在生成期间被修改过"
        self._finish_stream_block(f"\n[{why}，没有自动写入]", "error",
                                  action=(f"覆盖第 {int(self.chapter_key) + 1} 章（目录序号）的笔记", "overwrite"))

    def _overwrite_pending(self):
        if self.pending_summary is None:
            return
        app = self.app
        book, chapter = self.note_id
        if app.displayed_note == self.note_id:
            if app.note_revisions.get(self.note_id, 0) != self.pending_revision:
                if not messagebox.askyesno("覆盖笔记", "笔记又被改过了。仍然用讲解稿覆盖？", parent=self):
                    return
            app._mutate_note(self.pending_summary, source="ai_replace")
        else:
            if not messagebox.askyesno("覆盖笔记", f"要覆盖的是「{os.path.basename(book)}」目录第 {int(chapter) + 1} 项的笔记，"
                                                  "不是右栏现在显示的那章。继续？", parent=self):
                return
            app.config.set_note(book, chapter, self.pending_summary)
        self._append("[已写入笔记]", "system")
        self.pending_summary = None

    def _put_into_note(self, content):
        app = self.app
        book, chapter = self.note_id
        if app.displayed_note == self.note_id:
            current = app._note_text()
            app._mutate_note((current.rstrip() + "\n\n" + content) if current.strip() else content, source="ai_append")
        else:
            existing = app.config.get_note(book, chapter)
            app.config.set_note(book, chapter, (existing.rstrip() + "\n\n" + content) if existing.strip() else content)
        self._append(f"[已追加到目录第 {int(chapter) + 1} 项的笔记]", "system")

    # ---------- 关闭 ----------

    def _on_close(self):
        self._closed = True
        if self.active_request is not None:
            self.active_request.cancel.set()
            self.active_request = None
        self.app.ai_dialog = None
        self.destroy()
