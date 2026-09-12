"""第 8 步验收：AI 对话窗。假客户端（不联网、不花钱），真 Tk（withdraw）。
覆盖：类型自动判定 → book_meta；生成讲解写进右栏 + 记录 + 记账；生成中切章 / 编辑 → 不写、给覆盖按钮；
覆盖按钮写到原章；关窗后 delta 丢弃、占用照样释放、能重开；失败 / 截断不写笔记；放进笔记追加；没 key 禁用。
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_harness as H

import tkinter as tk
import ai_chat
from ai_chat import AiError

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


class FakeClient:
    """stream_chat 在 manager 线程里跑：先发几段 delta，然后等 gate，再发 done。complete 返回判定 JSON。"""

    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.next_status = "finished"
        self.next_text = "这是讲解稿。第二句。"
        self.detect_text = '{"kind":"fiction","language":"zh"}'
        self.calls = []
        self.api_key = "sk-test"

    def redact(self, t):
        return t

    def complete(self, request, messages, max_tokens, json_mode=False, temperature=0.3):
        self.calls.append(("complete", request.purpose))
        return self.detect_text, {"prompt_tokens": 40, "completion_tokens": 8, "cost": 0.00001}, "stop"

    def stream_chat(self, request, messages, max_tokens, out_queue, temperature=0.7):
        self.calls.append(("stream", request.purpose, messages))
        parts = [self.next_text[:3], self.next_text[3:]]
        for p in parts:
            if request.cancel.is_set():
                break
            out_queue.put(("ai_delta", request.id, p))
            time.sleep(0.02)
        self.gate.wait(5)
        status = "cancelled" if request.cancel.is_set() else self.next_status
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0005} if status != "cancelled" else None
        text = "".join(parts) if status != "failed" else ""
        out_queue.put(("ai_done", request.id, status, text, usage, "stop" if status == "finished" else None,
                       "模拟失败" if status == "failed" else None))


def pump(app, seconds=0.4):
    end = time.time() + seconds
    while time.time() < end:
        H.current_root().update()
        time.sleep(0.02)


def wait_until(app, cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        H.current_root().update()
        if cond():
            pump(app, 0.15)     # 线程 finally 之后还有一条 ai_done / ai_finished 在队列里，让定时器把它 drain 掉
            return True
        time.sleep(0.02)
    return False


os.environ["OPENROUTER_KEY"] = "sk-test"
app, layout = H.new_app(v2_data={"schema": 2, "last_file": H.book1, "recent_files": [H.book1]})
fake = FakeClient()
app.ai_client = fake
app.ai_cfg["api_key"] = "sk-test"

# ---------- 1. 打开对话窗 → 自动判定 → book_meta + 主窗口下拉 ----------
print("1. 自动判定")
app._load_chapter(1)
app._open_ai_dialog()
dlg = app.ai_dialog
check(dlg is not None and dlg.note_id == (H.book1, "1"), "对话窗绑定 (书一, 1)")
check(wait_until(app, lambda: app.config.get_book_meta(H.book1).get("kind") == "fiction"), "判定结果写进 book_meta")
meta = app.config.get_book_meta(H.book1)
check(meta.get("kind_source") == "auto" and meta.get("language") == "zh", f"来源 auto、语言 zh：{meta}")
check(app.kind_var.get() == "小说", "主窗口下拉刷新为小说")
check(wait_until(app, lambda: app.ai_manager.running is None), "判定线程释放占用")
check(app.config.config["ai_usage"].get("requests") == 1, "判定记了账")

# ---------- 2. 生成讲解：空笔记 → 直接写入 ----------
print("2. 生成讲解写入")
dlg._generate()
check(app.ai_manager.running is not None and dlg.active_request is not None, "请求在跑")
check(wait_until(app, lambda: app.ai_manager.running is None), "请求结束")
check(app._note_text() == "这是讲解稿。第二句。", f"右栏是讲解稿：{app._note_text()!r}")
check(H.read_json(layout.bookmarks_path)["notes"][H.book1]["1"] == "这是讲解稿。第二句。", "笔记已落盘")
hist = app.chat_store.history(H.book1, "1")
check(len(hist) == 1 and hist[0]["is_summary"] and hist[0]["status"] == "finished" and hist[0]["usage"]["cost_usd"] == 0.0005, f"记录 + usage：{hist}")
check(app.chat_store.latest_summary(H.book1, "1") == "这是讲解稿。第二句。", "latest_summary")
check(abs(app.config.config["ai_usage"]["cost_usd"] - 0.00051) < 1e-9, f"记账累计：{app.config.config['ai_usage']}")
# 第二章生成时带上一章讲解
app._load_chapter(2)
dlg._on_close()
app._open_ai_dialog()
dlg = app.ai_dialog
fake.calls.clear()
dlg._generate()
wait_until(app, lambda: app.ai_manager.running is None)
msgs = [c for c in fake.calls if c[0] == "stream"][0][2]
check("<previous>" in msgs[1]["content"] and "这是讲解稿" in msgs[1]["content"], "第 2 章的 prompt 带了第 1 章的讲解稿")

# ---------- 3. 生成中切章 → 不写入，给覆盖按钮；覆盖写到原章 ----------
print("3. 生成中切章")
app._load_chapter(1)
dlg._on_close()
app._open_ai_dialog()
dlg = app.ai_dialog
H.yes_answers["default"] = True     # 右栏非空 → 问 → 答是
fake.gate.clear()
fake.next_text = "新稿子。"
dlg._generate()
pump(app, 0.2)
app._load_chapter(3)                # 生成期间切到第 3 章
fake.gate.set()
check(wait_until(app, lambda: app.ai_manager.running is None), "请求结束")
check(app.displayed_note == (H.book1, "3") and app._note_text() == "", "第 3 章右栏没被写")
check(app.config.get_note(H.book1, "1") == "这是讲解稿。第二句。", "第 1 章笔记没被自动覆盖")
check(dlg.pending_summary == "新稿子。", "稿子留在窗口里")
dlg._overwrite_pending()
check(app.config.get_note(H.book1, "1") == "新稿子。", "覆盖写到第 1 章（不是右栏显示的第 3 章）")
check(app._note_text() == "", "右栏（第 3 章）仍然为空")

# ---------- 4. 生成中编辑 → 不写入 ----------
print("4. 生成中编辑")
app._load_chapter(1)
dlg._on_close()
app._open_ai_dialog()
dlg = app.ai_dialog
fake.gate.clear()
fake.next_text = "又一稿。"
dlg._generate()
pump(app, 0.2)
app.sandbox_area.insert(tk.END, "手动加的")
H.current_root().update()
fake.gate.set()
wait_until(app, lambda: app.ai_manager.running is None)
check(app._note_text().endswith("手动加的") and dlg.pending_summary == "又一稿。", "编辑过 → 不覆盖，稿子挂起")

# ---------- 5. 关窗：delta 丢弃、占用释放、能重开 ----------
print("5. 关窗")
fake.gate.clear()
dlg.input.insert("1.0", "问一个问题")
dlg._send()
pump(app, 0.1)
dlg._on_close()
check(app.ai_dialog is None, "窗口已关")
check(app.ai_manager.running is not None, "后台线程还在（等 gate）")
app._open_ai_dialog()
dlg2 = app.ai_dialog
check(dlg2.send_btn.cget("state") == "disabled", "重开的窗口在旧请求结束前禁用发送")
fake.gate.set()
check(wait_until(app, lambda: app.ai_manager.running is None), "旧请求结束、占用释放")
pump(app, 0.2)
check(dlg2.send_btn.cget("state") == "normal", "释放后发送可用")
check(app.config.config["ai_usage"]["requests"] >= 5, "关窗后的 done 也记了账")

# ---------- 6. 追问 + 放进笔记 ----------
print("6. 追问")
fake.next_text = "回答内容。"
dlg2.input.insert("1.0", "李义山是谁")
dlg2._send()
wait_until(app, lambda: app.ai_manager.running is None)
pump(app, 0.2)
hist = app.chat_store.history(H.book1, "1")
check(hist[-2]["role"] == "user" and hist[-2]["content"] == "李义山是谁" and hist[-1]["content"] == "回答内容。", "记录了问答")
before = app._note_text()
dlg2._put_into_note("回答内容。")
check(app._note_text() == before.rstrip() + "\n\n回答内容。", "放进笔记 = 追加")

# ---------- 7. 失败 / 截断 不写笔记 ----------
print("7. 失败 / 截断")
note_before = app._note_text()
fake.next_status = "failed"
dlg2._generate()
wait_until(app, lambda: app.ai_manager.running is None)
pump(app, 0.2)
check(app._note_text() == note_before, "失败不写")
check("失败" in dlg2.text.get("1.0", tk.END), "失败显示在对话区")
fake.next_status = "truncated"
fake.next_text = "截断的稿子"
dlg2._generate()
wait_until(app, lambda: app.ai_manager.running is None)
pump(app, 0.2)
check(app._note_text() == note_before and "截断" in dlg2.text.get("1.0", tk.END), "截断不写、有提示")
fake.next_status = "finished"

# ---------- 8. 没 key → 禁用 ----------
print("8. 没 key")
dlg2._on_close()
app.ai_cfg["api_key"] = ""
app._open_ai_dialog()
dlg3 = app.ai_dialog
check(dlg3.gen_btn.cget("state") == "disabled" and "OPENROUTER_KEY" in dlg3.state_var.get(), "没 key：按钮禁用并说明")
dlg3._on_close()

H.cleanup()
print()
if FAILS:
    print(f"FAILED {len(FAILS)}")
    sys.exit(1)
print("ALL OK")
