"""第 7 步验收：ai_chat 传输层。假 HTTP（monkeypatch opener），不联网、不花钱。
覆盖：SSE 注释行 / UTF-8 跨块 / usage 尾帧 / 缺 [DONE] / 流内 error / length 截断 / 空输出 / 取消 /
零碎字节无换行的总时限 / HTTP 错误脱敏 / base_url 限制 / 重定向拒绝 / 任务管理 / 判定结果校验 / prompt 构造。
"""

import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_chat
from ai_chat import (OpenRouterClient, AiRequest, AiTaskManager, AiError, parse_detect_result,
                     build_summary_messages, build_chat_messages, budget_ok, summary_target, usage_summary,
                     load_ai_config, _NoRedirect)
import urllib.error

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


class FakeResp:
    def __init__(self, chunks, per_read_delay=0.0, endless=False):
        self.chunks = list(chunks)
        self.delay = per_read_delay
        self.endless = endless
        self.closed = False

    def read(self, n):
        if self.delay:
            time.sleep(self.delay)
        if self.chunks:
            return self.chunks.pop(0)
        if self.endless:
            return b"x"           # 永远有零碎字节、永远没有换行
        return b""

    def close(self):
        self.closed = True


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body):
        super().__init__("https://openrouter.ai/api/v1/chat/completions", code, "err", {}, None)
        self._body = body

    def read(self, n=-1):
        return self._body


def client_with(resp_factory, **kw):
    kw.setdefault("provider", "openrouter")
    kw.setdefault("base_url", "https://openrouter.ai/api/v1")
    c = OpenRouterClient("deepseek/deepseek-v4.1-flash", "sk-or-SECRET123", **kw)
    c._opener = type("O", (), {"open": staticmethod(lambda req, timeout=None: resp_factory(req))})()
    return c


def sse(*events):
    """把事件（dict / "[DONE]" / 原始 bytes）编码成 SSE 字节。"""
    import json
    out = b""
    for e in events:
        if isinstance(e, bytes):
            out += e
        elif e == "[DONE]":
            out += b"data: [DONE]\n\n"
        else:
            out += b"data: " + json.dumps(e, ensure_ascii=False).encode("utf-8") + b"\n\n"
    return out


def delta(text, finish=None):
    return {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}


def run_stream(client, chunks, cancel_after=None, **kw):
    q = queue.Queue()
    req = AiRequest(purpose="chat", note_id=("b", "1"), note_revision=0, model="m")
    if cancel_after is not None:
        threading.Timer(cancel_after, req.cancel.set).start()
    t0 = time.monotonic()
    client.stream_chat(req, [{"role": "user", "content": "hi"}], 100, q)
    elapsed = time.monotonic() - t0
    msgs = []
    while True:
        try:
            msgs.append(q.get_nowait())
        except queue.Empty:
            break
    done = [m for m in msgs if m[0] == "ai_done"][0]
    deltas = "".join(m[2] for m in msgs if m[0] == "ai_delta")
    return done, deltas, elapsed


# ---------- 1. 正常流：注释行、UTF-8 跨块、finish 之后的 usage 帧、[DONE] ----------
print("1. 正常流")
body = sse(b": OPENROUTER PROCESSING\n\n", delta("你好"), delta("，世"), delta("界。", "stop"),
           {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "cost": 0.000012,
                                     "prompt_tokens_details": {"cached_tokens": 4}}}, "[DONE]")
# 在一个多字节字符中间切块
cut = body.index("世".encode("utf-8")) + 1
chunks = [body[:cut], body[cut:cut + 7], body[cut + 7:]]
c = client_with(lambda req: FakeResp(chunks))
done, deltas, _ = run_stream(c, chunks)
check(done[2] == "finished" and done[3] == "你好，世界。", f"finished，文本完整：{done[2]} {done[3]!r}")
check(deltas == "你好，世界。", "delta 拼起来一致")
check(done[4] and done[4]["cost"] == 0.000012, f"usage 尾帧收到：{done[4]}")
check(usage_summary(done[4]) == (10, 3, 4, 0.000012), "usage_summary")

# ---------- 2. 缺 [DONE] ----------
print("2. 缺 [DONE]")
c = client_with(lambda req: FakeResp([sse(delta("半截", "stop"))]))
done, _, _ = run_stream(c, None)
check(done[2] == "failed" and "[DONE]" in done[6], f"failed：{done[6]}")

# ---------- 3. 流内 error（HTTP 200 之后） ----------
print("3. 流内 error")
c = client_with(lambda req: FakeResp([sse(delta("开头"), {"error": {"message": "rate limited sk-or-SECRET123", "code": 429}}, "[DONE]")]))
done, _, _ = run_stream(c, None)
check(done[2] == "failed" and "API 错误" in done[6] and "SECRET123" not in done[6], f"failed 且脱敏：{done[6]}")

# ---------- 4. length 截断 ----------
print("4. 截断")
c = client_with(lambda req: FakeResp([sse(delta("太长了", "length"), "[DONE]")]))
done, _, _ = run_stream(c, None)
check(done[2] == "truncated" and done[3] == "太长了", "truncated")

# ---------- 5. 空输出 ----------
print("5. 空输出")
c = client_with(lambda req: FakeResp([sse(delta("", "stop"), "[DONE]")]))
done, _, _ = run_stream(c, None)
check(done[2] == "failed" and "空" in done[6], f"空输出 failed：{done[6]}")

# ---------- 6. 取消 ----------
print("6. 取消")
slow = FakeResp([sse(delta("一")), sse(delta("二")), sse(delta("三")), sse(delta("四", "stop"), "[DONE]")], per_read_delay=0.15)
c = client_with(lambda req: slow)
done, deltas, _ = run_stream(c, None, cancel_after=0.2)
check(done[2] == "cancelled", f"cancelled：{done[2]}")
check(len(deltas) < 4, f"取消后不再投递：{deltas!r}")
check(slow.closed, "响应已关闭")

# ---------- 7. 零碎字节、没有换行 → 总时限 ----------
print("7. 总时限")
c = client_with(lambda req: FakeResp([], per_read_delay=0.02, endless=True), deadline=0.3)
done, _, elapsed = run_stream(c, None)
check(done[2] == "failed" and "总时长" in done[6], f"超时 failed：{done[6]}")
check(elapsed < 0.3 + 0.5, f"在 deadline + 一次读超时内结束：{elapsed:.2f}s")

# ---------- 8. HTTP 错误脱敏 / 无 key ----------
print("8. HTTP 错误")


def raise_401(req):
    raise FakeHTTPError(401, b'{"error":"bad key sk-or-SECRET123"}')


c = client_with(raise_401)
done, _, _ = run_stream(c, None)
check(done[2] == "failed" and "HTTP 401" in done[6] and "SECRET123" not in done[6], f"401 脱敏：{done[6]}")
c2 = OpenRouterClient("m", "", provider="openrouter", base_url="https://openrouter.ai/api/v1")
done, _, _ = run_stream(c2, None)
check(done[2] == "failed" and "OPENROUTER_KEY" in done[6], "没 key 直接失败并说明（openrouter）")
c3 = OpenRouterClient("deepseek-flash", "")
done, _, _ = run_stream(c3, None)
check(done[2] == "failed" and "DEEPSEEK_API_KEY" in done[6], "没 key 直接失败并说明（deepseek 默认）")

# ---------- 9. base_url 限制 / 重定向 ----------
print("9. base_url")
try:
    OpenRouterClient("m", "k", base_url="https://evil.example.com/v1", provider="openrouter")
    check(False, "自定义 base_url 未加 flag 应拒绝")
except AiError as e:
    check("allow_custom_base_url" in str(e), "拒绝并提示 flag")
try:
    OpenRouterClient("m", "k", base_url="http://localhost:8080/v1", allow_custom_base_url=True)
    check(False, "http 应拒绝")
except AiError:
    check(True, "http 拒绝")
OpenRouterClient("m", "k", base_url="https://localhost:8080/v1", allow_custom_base_url=True)
check(True, "https + flag 允许")
try:
    _NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere/x")
    check(False, "重定向应抛")
except AiError as e:
    check("重定向" in str(e), "重定向被拒")

# ---------- 10. complete() + 判定结果校验 ----------
print("10. complete / detect")
import json as _json
ok_body = _json.dumps({"choices": [{"message": {"content": '{"kind":"fiction","language":"zh"}'}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 5, "completion_tokens": 8, "cost": 0.0}}).encode()
c = client_with(lambda req: FakeResp([ok_body]))
req = AiRequest(purpose="detect", note_id=None, note_revision=0, model="m")
text, usage, finish = c.complete(req, [{"role": "user", "content": "x"}], 50, json_mode=True)
check(parse_detect_result(text) == {"kind": "fiction", "language": "zh"}, "判定解析")
check(parse_detect_result('{"kind":"poetry","language":"zh"}') is None, "kind 不在枚举 → None")
check(parse_detect_result('{"kind":"fiction","language":"中文"}') is None, "language 不合法 → None")
check(parse_detect_result('前面有话 {"kind":"nonfiction","language":"EN"} 后面有话') == {"kind": "nonfiction", "language": "en"}, "包着文字也能捞出来、大小写归一")
check(parse_detect_result("not json") is None and parse_detect_result("") is None, "坏输入 → None")
c = client_with(lambda req: FakeResp([b'{"error":{"message":"boom"}}']))
try:
    c.complete(req, [], 10)
    check(False, "error 应抛")
except AiError as e:
    check("API 错误" in str(e), "complete 的 error 抛 AiError")

# ---------- 11. AiTaskManager ----------
print("11. AiTaskManager")
q = queue.Queue()
mgr = AiTaskManager(q)
gate = threading.Event()
r1 = AiRequest(purpose="summary", note_id=("b", "1"), note_revision=0, model="m")
r2 = AiRequest(purpose="summary", note_id=("b", "1"), note_revision=0, model="m")
check(mgr.start(r1, lambda r: gate.wait(5)), "第一个能启动")
check(mgr.start(r2, lambda r: None) is False, "第二个被拒（running 非空）")
mgr.cancel_running()
check(r1.cancel.is_set(), "cancel_running 设了 cancel")
gate.set()
time.sleep(0.2)
msgs = []
while not q.empty():
    msgs.append(q.get_nowait())
check(("ai_finished", r1.id) in msgs, "结束时发 ai_finished")
check(mgr.running is None and mgr.start(r2, lambda r: 1 / 0), "释放后能再启动")
time.sleep(0.2)
msgs = []
while not q.empty():
    msgs.append(q.get_nowait())
check(any(m[0] == "ai_done" and m[1] == r2.id and m[2] == "failed" for m in msgs) and ("ai_finished", r2.id) in msgs,
      "fn 抛异常 → ai_done failed + ai_finished")

# ---------- 12. prompt / 预算 / 配置 ----------
print("12. prompt")
msgs = build_summary_messages("fiction", "zh", "正文" * 100, "上一章讲解", summary_target("正文" * 100, "zh"))
check("<previous>" in msgs[1]["content"] and "<chapter>" in msgs[1]["content"], "带上一章")
check("300 字" in msgs[0]["content"] and "四、留一个问题" in msgs[0]["content"], "长度下限 300 + fiction 结构")
check("words" in build_summary_messages("nonfiction", "en", "word " * 5000, None, summary_target("word " * 5000, "en"))[0]["content"], "英文按 words")
check(summary_target("word " * 5000, "en") == 500 and summary_target("字" * 8000, "zh") == 800, "1/10")
hist = [{"role": "user", "content": f"q{i}"} for i in range(30)]
cm = build_chat_messages("正文", "讲解", hist, "新问题", history_turns=2)
check([m["content"] for m in cm[-5:]] == ["q26", "q27", "q28", "q29", "新问题"], f"只带最近 2 轮（4 条）：{[m['content'] for m in cm[-5:]]}")
ok, est, limit = budget_ok("unknown/model", [{"role": "user", "content": "字" * 40000}], 1000)
check(ok is False and limit == 32768, "未知模型按 32k，超预算拒绝")
ok, est, limit = budget_ok("deepseek/deepseek-v4.1-flash", [{"role": "user", "content": "字" * 40000}], 1000)
check(ok is True, "1M 上下文放得下")
os.environ["OPENROUTER_KEY"] = "  sk-test  "
os.environ["DEEPSEEK_API_KEY"] = "sk-ds"
cfg = load_ai_config({"provider": "openrouter", "model": "x/y", "history_turns": -1})
check(cfg["model"] == "x/y" and cfg["history_turns"] == 10 and cfg["api_key"] == "sk-test" and cfg["base_url"] == "https://openrouter.ai/api/v1", f"配置读取 openrouter：{cfg}")
cfg = load_ai_config({})
check(cfg["provider"] == "deepseek" and cfg["model"] == "deepseek-flash" and cfg["api_key"] == "sk-ds" and cfg["base_url"] == "https://api.deepseek.com" and cfg["key_env"] == "DEEPSEEK_API_KEY", f"默认 deepseek：{cfg}")
cfg = load_ai_config({"provider": "nonsense"})
check(cfg["provider"] == "deepseek", "未知 provider 回默认")

print("13. DeepSeek 费用估算 / 请求体")
import time as _t
peak = _t.struct_time((2026, 9, 14, 2, 0, 0, 0, 257, 0))     # 周一 02:00 UTC
off = _t.struct_time((2026, 9, 14, 12, 0, 0, 0, 257, 0))     # 周一 12:00 UTC
sat = _t.struct_time((2026, 9, 12, 2, 0, 0, 5, 255, 0))      # 周六 02:00 UTC
check(ai_chat.is_peak_utc(peak) and not ai_chat.is_peak_utc(off) and not ai_chat.is_peak_utc(sat), "高峰判定")
u = {"prompt_tokens": 1000, "completion_tokens": 100, "prompt_cache_hit_tokens": 400}
check(abs(ai_chat.estimate_cost("deepseek-flash", u, peak) - (400*0.006 + 600*0.30 + 100*1.20)/1e6) < 1e-12, "高峰估算：命中/未命中/输出分开算")
check(abs(ai_chat.estimate_cost("deepseek-flash", u, off) - (400*0.006 + 600*0.30 + 100*1.20)/2e6) < 1e-12, "非高峰半价")
check(ai_chat.estimate_cost("unknown-model", u) is None, "未知模型 → None")
check(usage_summary({"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001}, "deepseek-flash") == (10, 5, None, 0.001), "有实际 cost 优先")
p_, c_, cached_, cost_ = usage_summary(u, "deepseek-flash")
check(cached_ == 400 and cost_ is not None, "deepseek usage 用 prompt_cache_hit_tokens，费用为估算")
check(usage_summary(u)[3] is None, "不给 model 不估")
# 请求体：deepseek 用 thinking.disabled + stream_options；openrouter 用 reasoning + usage.include
captured = {}
class _R:
    def __init__(self): self.chunks=[sse(delta("x", "stop"), "[DONE]")]
    def read(self, n): return self.chunks.pop(0) if self.chunks else b""
    def close(self): pass
def opener(req, timeout=None):
    import json as _j
    captured["body"] = _j.loads(req.data.decode("utf-8")); captured["url"] = req.full_url
    return _R()
cd = OpenRouterClient("deepseek-flash", "k")
cd._opener = type("O", (), {"open": staticmethod(opener)})()
run_stream(cd, None)
b = captured["body"]
check(b.get("thinking") == {"type": "disabled"} and b.get("stream_options") == {"include_usage": True} and "reasoning" not in b and "usage" not in b,
      f"deepseek 请求体：{ {k: v for k, v in b.items() if k not in ('messages',)} }")
check(captured["url"] == "https://api.deepseek.com/chat/completions", f"deepseek 地址：{captured['url']}")
co = client_with(opener)
run_stream(co, None)
b = captured["body"]
check(b.get("reasoning") == {"enabled": False} and b.get("usage") == {"include": True} and "thinking" not in b, "openrouter 请求体不变")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}")
    sys.exit(1)
print("ALL OK")
