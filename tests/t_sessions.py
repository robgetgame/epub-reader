"""第 4+5 步验收：播放 session 取消（A.3）、下载器去重 / 并发上限 / 重试（A.4 / B2）、
失败上报（B7）、SAPI 初始化失败不死等（B8）、设置每句重读。

假 pygame、假 SAPI、假 edge-tts（子类覆盖 _download）。不联网、不出声。
"""

import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ---- 假 pygame ----
fake_pygame = types.ModuleType("pygame")
mixer = types.SimpleNamespace()
music_state = {"playing": False, "loaded": [], "polls_left": 0}


def m_init():
    if music_state.get("init_fail"):
        raise RuntimeError("没有音频设备")


def m_load(path):
    music_state["loaded"].append(path)


def m_play():
    music_state["playing"] = True
    music_state["polls_left"] = 2


def m_busy():
    if music_state["polls_left"] > 0:
        music_state["polls_left"] -= 1
        return True
    music_state["playing"] = False
    return False


def m_stop():
    music_state["playing"] = False
    music_state["polls_left"] = 0


mixer.init = m_init
mixer.music = types.SimpleNamespace(load=m_load, play=m_play, get_busy=m_busy, stop=m_stop, unload=lambda: None,
                                    set_volume=lambda v: music_state.__setitem__("volume", v))
fake_pygame.mixer = mixer
sys.modules["pygame"] = fake_pygame

# ---- 假 COM / SAPI ----
sapi_log = {"spoken": [], "purges": 0, "fail_init": False}


class FakeVoice:
    def __init__(self, vid):
        self.Id = vid

    def GetDescription(self):
        return "Voice " + self.Id


class FakeVoices:
    def __init__(self):
        self._v = [FakeVoice("sapi-a"), FakeVoice("sapi-b")]
        self.Count = 2

    def Item(self, i):
        return self._v[i]


class FakeSpeaker:
    def __init__(self):
        self.Rate = 0
        self.Voice = None
        self._polls = 0

    def GetVoices(self):
        return FakeVoices()

    def Speak(self, text, flags):
        if flags == 2 and text == "":
            sapi_log["purges"] += 1
            self._polls = 0
            return
        sapi_log["spoken"].append((text, getattr(self.Voice, "Id", None), self.Rate))
        self._polls = 3

    def WaitUntilDone(self, ms):
        if self._polls > 0:
            self._polls -= 1
            time.sleep(0.01)
            return False
        return True


fake_pythoncom = types.ModuleType("pythoncom")


def co_init():
    if sapi_log["fail_init"]:
        raise RuntimeError("CoInitialize 炸了")


fake_pythoncom.CoInitialize = co_init
fake_pythoncom.CoUninitialize = lambda: None
sys.modules["pythoncom"] = fake_pythoncom
fake_win32 = types.ModuleType("win32com")
fake_client = types.ModuleType("win32com.client")
fake_client.Dispatch = lambda name: FakeSpeaker()
fake_win32.client = fake_client
sys.modules["win32com"] = fake_win32
sys.modules["win32com.client"] = fake_client

import tts_thread
from tts_thread import TTSWorker, EdgeTTSDownloader, NEURAL_VOICES

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


class FakeDownloader(EdgeTTSDownloader):
    """_download 不联网：按设定延迟 / 失败次数写一个假 mp3。记录并发。"""

    def __init__(self, cache_dir, **kw):
        super().__init__(cache_dir, **kw)
        self.delay = 0.02
        self.fail_first = {}          # path -> 还要失败几次
        self.calls = {}               # path -> 次数
        self.block = None             # threading.Event：设了就卡住直到 set
        self.running = 0
        self.max_running = 0
        self.stat_lock = threading.Lock()

    async def _download(self, job, tmp):
        with self.stat_lock:
            self.calls[job.path] = self.calls.get(job.path, 0) + 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            if self.block is not None:
                while not self.block.is_set():
                    time.sleep(0.01)
            time.sleep(self.delay)
            if self.fail_first.get(job.path, 0) > 0:
                self.fail_first[job.path] -= 1
                raise RuntimeError("假失败")
            with open(tmp, "wb") as f:
                f.write(b"MP3" + job.text.encode("utf-8"))
        finally:
            with self.stat_lock:
                self.running -= 1


def make_worker(cache_dir, **kw):
    ev = queue.Queue()
    dl = FakeDownloader(cache_dir, **kw)
    w = TTSWorker(ev, cache_dir=cache_dir, downloader=dl)
    return w, ev, dl


def drain(ev, timeout=5.0, until=None):
    """收消息直到 until(msgs) 为真或超时。"""
    msgs = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msgs.append(ev.get(timeout=0.05))
        except queue.Empty:
            pass
        if until is not None and until(msgs):
            break
    return msgs


def dones(msgs):
    return [m for m in msgs if m[0] == "done"]


def wait_ready(ev):
    return drain(ev, until=lambda ms: any(m[0] == "ready" for m in ms))


NEURAL = NEURAL_VOICES[0]
tmp_root = tempfile.mkdtemp(prefix="epubreader-sess-")

# ---------- 1. 正常播完：highlight 顺序、done finished、无 skipped ----------
print("1. 正常播完")
w, ev, dl = make_worker(os.path.join(tmp_root, "c1"))
wait_ready(ev)
w.set_settings(NEURAL, 200)
sid = w.play(["一。", "二。", "三。"], 0)
msgs = drain(ev, until=lambda ms: dones(ms))
hl = [m[2] for m in msgs if m[0] == "highlight" and m[1] == sid]
check(hl == [0, 1, 2], f"高亮顺序：{hl}")
d = dones(msgs)[0]
check(d[1] == sid and d[2] == "finished" and d[3] == [], f"done finished 无跳句：{d}")
check(len(music_state["loaded"]) == 3, "播了 3 个文件")
check(all(v == 1 for v in dl.calls.values()) and len(dl.calls) == 3, f"每句只下载一次（含预取去重）：{dl.calls}")
w.quit()

# ---------- 2. 交接窗口：worker 已出队、未 mark_current 时 Stop ----------
print("2. 交接窗口")
w, ev, dl = make_worker(os.path.join(tmp_root, "c2"))
wait_ready(ev)
w.set_settings("sapi-a", 200)
barrier_in = threading.Event()
barrier_out = threading.Event()


def hook(session):
    barrier_in.set()
    barrier_out.wait(5)


w.on_dequeue = hook
sapi_log["spoken"].clear()
sid = w.play(["不该出声。"], 0)
check(barrier_in.wait(3), "worker 停在出队之后")
w.stop()
barrier_out.set()
msgs = drain(ev, until=lambda ms: dones(ms))
d = dones(msgs)[0]
check(d[1] == sid and d[2] == "cancelled", f"done cancelled：{d}")
check(sapi_log["spoken"] == [], "没有任何 Speak")
check(not [m for m in msgs if m[0] == "highlight"], "没有 highlight")
w.on_dequeue = None

# ---------- 3. Stop 在 PLAY 出队之前（worker 正忙） ----------
print("3. Stop 早于出队")
busy_gate = threading.Event()


def slow_hook(session):
    if session.sentences and session.sentences[0] == "慢":
        busy_gate.wait(5)


w.on_dequeue = slow_hook
sapi_log["spoken"].clear()
sid_a = w.play(["慢"], 0)
time.sleep(0.1)
sid_b = w.play(["B 不该出声。"], 0)      # play() 本身取消 A
w.stop()                                   # 取消 B（还在队里）
busy_gate.set()
msgs = drain(ev, until=lambda ms: len(dones(ms)) >= 2)
st = {m[1]: m[2] for m in dones(msgs)}
check(st.get(sid_a) == "cancelled" and st.get(sid_b) == "cancelled", f"A、B 都 cancelled：{st}")
check(sapi_log["spoken"] == [], "B 没出声")
w.on_dequeue = None

# ---------- 4. 快速 Play→Stop→Play：只有第三个出声 ----------
print("4. Play→Stop→Play")
sapi_log["spoken"].clear()
s1 = w.play(["第一。"], 0)
w.stop()
s2 = w.play(["第二。"], 0)
w.stop()
s3 = w.play(["第三。"], 0)
msgs = drain(ev, until=lambda ms: any(m[0] == "done" and m[1] == s3 for m in ms))
spoken = [t for t, _, _ in sapi_log["spoken"]]
check(spoken == ["第三。"], f"只有第三个 session 出声：{spoken}")
st = {m[1]: m[2] for m in dones(msgs)}
check(st.get(s3) == "finished" and st.get(s1) == "cancelled" and st.get(s2) == "cancelled", f"终态：{st}")
check(len(dones(msgs)) == 3, "每个 session 恰好一个 done")
w.quit()

# ---------- 5. 下载失败 → skipped + finished（不是 cancelled，也不静默） ----------
print("5. 下载失败上报")
w, ev, dl = make_worker(os.path.join(tmp_root, "c5"))
wait_ready(ev)
w.set_settings(NEURAL, 200)
bad = dl.cache_path("坏。", NEURAL, "+0%")
dl.fail_first[bad] = 99
tts_thread.MAX_RETRIES = 1
sid = w.play(["好。", "坏。", "又好。"], 0)
msgs = drain(ev, until=lambda ms: dones(ms))
sk = [m for m in msgs if m[0] == "skipped"]
check(len(sk) == 1 and sk[0][2] == 1 and "下载失败" in sk[0][3], f"skipped 第 1 句：{sk}")
d = dones(msgs)[0]
check(d[2] == "finished" and len(d[3]) == 1, f"finished 且带跳句列表：{d}")
check(dl.calls[bad] == 3, f"预取失败 1 次 + 正式尝试 2 次（MAX_RETRIES=1）：{dl.calls[bad]}")
tts_thread.MAX_RETRIES = 2

# ---------- 6. 第一次失败第二次成功 → 播了，不算跳 ----------
print("6. 重试成功")
music_state["loaded"].clear()
ok_path = dl.cache_path("先坏后好。", NEURAL, "+0%")
dl.fail_first[ok_path] = 1
sid = w.play(["先坏后好。"], 0)
msgs = drain(ev, until=lambda ms: dones(ms))
check(dones(msgs)[0][2] == "finished" and dones(msgs)[0][3] == [], "finished 无跳句")
check(len(music_state["loaded"]) == 1 and dl.calls[ok_path] == 2, "下载两次、播一次")

# ---------- 7. 下载超时 → skipped 超时 ----------
print("7. 下载超时")
tts_thread.WAIT_TIMEOUT_S = 0.5
dl.block = threading.Event()
sid = w.play(["卡住。"], 0)
msgs = drain(ev, timeout=8, until=lambda ms: dones(ms))
sk = [m for m in msgs if m[0] == "skipped"]
check(sk and "超时" in sk[0][3], f"超时上报：{sk}")
dl.block.set()
dl.block = None
tts_thread.WAIT_TIMEOUT_S = 20
w.quit()

# ---------- 8. 并发上限：阻塞下载 + 快速切章 20 次 ----------
print("8. 并发上限")
w, ev, dl = make_worker(os.path.join(tmp_root, "c8"), max_workers=4, max_pending=8)
wait_ready(ev)
w.set_settings(NEURAL, 200)
dl.block = threading.Event()
for n in range(20):
    w.play([f"章{n}句{k}。" for k in range(10)], 0)
    time.sleep(0.02)
    w.stop()
time.sleep(0.3)
with dl._lock:
    pending = dl._pending_count()
    running = dl.running
check(dl.max_running <= 4, f"实际并发 ≤ 4：{dl.max_running}")
check(pending <= 8, f"待处理 ≤ 8：{pending}")
dl.block.set()
dl.block = None
drain(ev, timeout=3)
check(threading.active_count() < 20, f"没有线程堆积：{threading.active_count()}")
w.quit()

# ---------- 9. 缓存清理：保护正在用的、扫掉 .error / 旧 .tmp ----------
print("9. 缓存清理")
cdir = os.path.join(tmp_root, "c9")
dl = FakeDownloader(cdir)
tts_thread.MAX_CACHE_MB = 0.001   # 1 KB
tts_thread.CACHE_TARGET_MB = 0.0005
paths = []
for i in range(6):
    p = os.path.join(cdir, f"{i:032x}.mp3")
    with open(p, "wb") as f:
        f.write(b"x" * 400)
    os.utime(p, (time.time() - 100 + i, time.time() - 100 + i))
    paths.append(p)
with open(os.path.join(cdir, "old.mp3.error"), "w") as f:
    f.write("e")
old_tmp = os.path.join(cdir, "old.mp3.abc.tmp")
with open(old_tmp, "w") as f:
    f.write("t")
os.utime(old_tmp, (time.time() - 1000, time.time() - 1000))
dl.manage_cache(protect={paths[0]})
left = sorted(os.listdir(cdir))
check(os.path.exists(paths[0]), "保护的最旧文件没被删")
check(not any(f.endswith(".error") for f in left) and "old.mp3.abc.tmp" not in left, f".error 和旧 .tmp 扫掉：{left}")
check(sum(os.path.getsize(os.path.join(cdir, f)) for f in left) <= 400 * 3, f"删到目标以下：{left}")
tts_thread.MAX_CACHE_MB = 500
tts_thread.CACHE_TARGET_MB = 400

# ---------- 10. B8：COM 初始化失败 → ready([]) + init_error，神经音色照常 ----------
print("10. SAPI 失败不死等")
sapi_log["fail_init"] = True
w, ev, dl = make_worker(os.path.join(tmp_root, "c10"))
msgs = drain(ev, timeout=3, until=lambda ms: any(m[0] == "ready" for m in ms))
kinds = [m[0] for m in msgs]
check("ready" in kinds and "init_error" in kinds, f"发了 ready + init_error：{kinds}")
check([m for m in msgs if m[0] == "ready"][0][1] == [], "ready 列表为空")
w.set_settings(NEURAL, 200)
sid = w.play(["神经音色照常。"], 0)
msgs = drain(ev, until=lambda ms: dones(ms))
check(dones(msgs)[0][2] == "finished" and dones(msgs)[0][3] == [], "神经音色正常播")
w.set_settings("sapi-a", 200)
sid = w.play(["本机语音。"], 0)
msgs = drain(ev, until=lambda ms: dones(ms))
sk = [m for m in msgs if m[0] == "skipped"]
check(sk and "本机语音不可用" in sk[0][3], f"本机语音句子上报不可用：{sk}")
sapi_log["fail_init"] = False
w.quit()

# ---------- 11. pygame 初始化失败 → audio_error + skipped，不假装切 SAPI ----------
print("11. 音频输出失败")
music_state["init_fail"] = True
w, ev, dl = make_worker(os.path.join(tmp_root, "c11"))
wait_ready(ev)
w.set_settings(NEURAL, 200)
sid = w.play(["放不出。"], 0)
msgs = drain(ev, until=lambda ms: dones(ms))
kinds = [m[0] for m in msgs]
check("audio_error" in kinds and any(m[0] == "skipped" and "音频输出" in m[3] for m in msgs), f"audio_error + skipped：{kinds}")
check(sapi_log["spoken"] == [] or sapi_log["spoken"][-1][0] != "放不出。", "没有偷偷用 SAPI 读")
music_state["init_fail"] = False
w.quit()

# ---------- 12. 设置每句重读：播放中换音色 / 语速 ----------
print("12. 设置每句重读")
w, ev, dl = make_worker(os.path.join(tmp_root, "c12"))
wait_ready(ev)
w.set_settings("sapi-a", 200)
sapi_log["spoken"].clear()
gate = threading.Event()


def hook2(session):
    pass


sid = w.play(["第一句。", "第二句。", "第三句。"], 0)
msgs = drain(ev, until=lambda ms: any(m[0] == "highlight" and m[2] == 0 for m in ms))
w.set_settings("sapi-b", 260)
msgs += drain(ev, until=lambda ms: dones(ms))
spoken = sapi_log["spoken"]
check(spoken[0][1] == "sapi-a" and spoken[-1][1] == "sapi-b", f"后面的句子用了新音色：{[(t, v) for t, v, _ in spoken]}")
check(spoken[-1][2] == 4, f"语速 260 → SAPI rate 4：{spoken[-1][2]}")
w.quit()

# ---------- 12b. prewarm：没按播放先下好、同 tag 换批释放旧的 ----------
print("12b. prewarm")
w, ev, dl = make_worker(os.path.join(tmp_root, "c12b"))
wait_ready(ev)
w.set_settings(NEURAL, 200)
w.prewarm(["预热一。", "预热二。", "预热三。"], 0)
time.sleep(0.5)
check(all(os.path.exists(dl.cache_path(t, NEURAL, "+0%")) for t in ["预热一。", "预热二。", "预热三。"]), "三句都下好了")
dl.block = threading.Event()
w.prewarm(["新批。"] * 1, 0)
with dl._lock:
    subs = {j.path: set(j.subscribers) for j in dl._jobs.values()}
check(all("prewarm" not in v or j.endswith(dl.cache_path("新批。", NEURAL, "+0%")) for j, v in subs.items()), "换批后旧 prewarm 订阅释放")
dl.block.set(); dl.block = None
w.set_settings("sapi-a", 200)
before = dict(dl.calls)
w.prewarm(["本机不下载。"], 0)
time.sleep(0.2)
check(dl.calls == before, "本机语音不触发下载")
w.quit()

# ---------- 13. end_idx：选区播放到指定句就停 ----------
print("13. end_idx")
w, ev, dl = make_worker(os.path.join(tmp_root, "c13"))
wait_ready(ev)
w.set_settings("sapi-a", 200)
sapi_log["spoken"].clear()
sid = w.play(["0。", "1。", "2。", "3。"], 1, end_idx=2)
msgs = drain(ev, until=lambda ms: dones(ms))
check([t for t, _, _ in sapi_log["spoken"]] == ["1。", "2。"], f"只读 1..2：{sapi_log['spoken']}")
w.quit()

shutil.rmtree(tmp_root, ignore_errors=True)
print()
if FAILS:
    print(f"FAILED {len(FAILS)}")
    sys.exit(1)
print("ALL OK")
