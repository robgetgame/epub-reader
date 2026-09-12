"""TTS 工作线程：播放 session、取消、下载器。设计文档 §2.2 / A.3 / A.4 / A.5，第 4+5 步（2026-09-12）。

为什么重写：旧版一个共享 stop_event，出队 PLAY 时先 clear() —— Stop 按在 PLAY 出队之前就丢了，
声音照样出来（B5）；回调不带身份，旧循环的迟到回调把新章书签写坏；下载靠「文件在不在」判重，
同一句两个线程抢一个 .tmp（B2，缓存里 90 个 .error 就是证据）；超时跳句还算「播完」（B7）；
SAPI 初始化失败 ready_event 永远不 set，主线程死等（B8）。

模型：
- 每次 play() 生成一个 PlaySession，**创建即登记**到 SessionRegistry，自带 cancel。
  stop() 取消注册表里全部未终结的 session —— 包括排在队里还没出队的、和刚出队还没标成 current 的
  （mark_current 在锁内检查 cancel，没有脱管窗口）。
- worker → UI 只走一个 queue，每条消息带 session id；UI 只认 active_session 的消息。
  done 是终态，由 _finish() 单出口发且只发一次；状态 finished / cancelled / failed。
  finished 表示「循环跑到末尾」，可能带 skipped 列表 —— 跳过的句子必须报出来。
- 下载器：ThreadPoolExecutor(4) + 待处理上限 8；每个缓存路径同时只有一个 writer；
  Job 不归属 session，用订阅者集合；预取用 "prefetch:<sid>" 伪订阅者登记。
  不再写 .error 文件；错误在内存里，清缓存顺便把旧 .error / 孤儿 .tmp 扫掉。
- 音色和语速是 worker 上的原子值，每句开始时重读 —— 播放中改设置下一句就生效。
- SAPI 对象只在 worker 线程碰；UI 的 stop() 只 set cancel，worker 的 WaitUntilDone(10) 循环自己 purge。
- pygame 延迟到第一次神经音色播放才初始化；失败报出来，不假装切到 SAPI。
"""

import asyncio
import hashlib
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

DEFAULT_CACHE_DIR = "temp_audio"
MAX_CACHE_MB = 500
CACHE_TARGET_MB = 400
DOWNLOAD_TIMEOUT_S = 60      # 单次 edge-tts 下载
WAIT_TIMEOUT_S = 20          # 播放循环等一句音频的上限
MAX_RETRIES = 2              # 同一句在一个 session 内的重试次数
PREFETCH_AHEAD = 3

NEURAL_VOICES = [
    'zh-CN-YunyangNeural',
    'en-US-JennyNeural',
    'zh-CN-XiaoxiaoNeural',
    'zh-CN-YunxiNeural',
    'en-US-AriaNeural',
    'en-US-GuyNeural',
]


# ====================================================================
# 播放 session
# ====================================================================

@dataclass
class PlaySession:
    id: int
    target: str                 # "epub" | "note"
    sentences: list             # str 或 {"text", "voice_id", ...}
    start_idx: int
    end_idx: int | None = None  # 含；None = 到末尾。第 9 步选区播放用
    cancel: threading.Event = field(default_factory=threading.Event)
    finished: bool = False      # done 只发一次


class SessionRegistry:
    """所有未终结的 session。创建即登记；stop 取消全部；worker 出队时在锁内确认没被取消。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}
        self._next_id = 1
        self.current_id = None

    def new(self, target, sentences, start_idx, end_idx=None):
        with self._lock:
            s = PlaySession(id=self._next_id, target=target, sentences=sentences,
                            start_idx=start_idx, end_idx=end_idx)
            self._next_id += 1
            self._sessions[s.id] = s
            return s

    def cancel_all(self):
        with self._lock:
            for s in self._sessions.values():
                s.cancel.set()

    def mark_current(self, session):
        """worker 出队后调用。已取消 → False，调用方直接终结它，不播。"""
        with self._lock:
            if session.cancel.is_set():
                return False
            self.current_id = session.id
            return True

    def finish(self, session):
        """返回是否是第一次终结（只有第一次才发 done）。"""
        with self._lock:
            first = not session.finished
            session.finished = True
            self._sessions.pop(session.id, None)
            if self.current_id == session.id:
                self.current_id = None
            return first

    def active_count(self):
        with self._lock:
            return len(self._sessions)


# ====================================================================
# 下载器
# ====================================================================

@dataclass
class Job:
    path: str
    text: str
    voice: str
    rate_str: str
    done: threading.Event = field(default_factory=threading.Event)
    result: str | None = None     # "ok" | "error" | "cancelled"
    error: str = ""
    started: bool = False
    subscribers: set = field(default_factory=set)
    future: object = None


class EdgeTTSDownloader:
    def __init__(self, cache_dir=DEFAULT_CACHE_DIR, max_workers=4, max_pending=8):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._jobs = {}            # path -> Job（进行中或刚完成、还有订阅者的）
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="edge-tts")
        self.max_pending = max_pending

    def cache_path(self, text, voice, rate_str):
        h = hashlib.md5(f"{text}_{voice}_{rate_str}".encode('utf-8')).hexdigest()
        return os.path.join(self.cache_dir, f"{h}.mp3")

    def _pending_count(self):
        return sum(1 for j in self._jobs.values() if not j.started and j.result is None)

    def request(self, text, voice, rate_str, subscriber, prefetch=False):
        """要一句音频。返回 Job（可能已经 done）或 None（预取被丢弃：队列满）。
        同一路径永远只有一个 Job 在跑 —— 第二个请求者只是加进订阅者集合。"""
        path = self.cache_path(text, voice, rate_str)
        with self._lock:
            job = self._jobs.get(path)
            if job is not None and job.result != "error":
                job.subscribers.add(subscriber)
                return job
            if os.path.exists(path):
                job = Job(path=path, text=text, voice=voice, rate_str=rate_str, result="ok")
                job.done.set()
                return job
            if prefetch and self._pending_count() >= self.max_pending:
                return None
            job = Job(path=path, text=text, voice=voice, rate_str=rate_str)
            job.subscribers.add(subscriber)
            self._jobs[path] = job
            job.future = self._pool.submit(self._run, job)
            return job

    def unsubscribe(self, job, subscriber):
        with self._lock:
            job.subscribers.discard(subscriber)
            if not job.subscribers and not job.started and job.future is not None:
                if job.future.cancel():
                    job.result = "cancelled"
                    job.done.set()
                    self._jobs.pop(job.path, None)

    def release(self, subscriber):
        """一个 session 结束：把它从所有 Job 的订阅者里去掉；没人要的、还没开始的任务取消掉。"""
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            self.unsubscribe(job, subscriber)

    def _run(self, job):
        with self._lock:
            if not job.subscribers:
                job.result = "cancelled"
                self._jobs.pop(job.path, None)
                job.done.set()
                return
            job.started = True
        tmp = f"{job.path}.{uuid.uuid4().hex}.tmp"
        try:
            asyncio.run(self._download(job, tmp))
            if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                raise RuntimeError("edge-tts 没有返回音频")
            os.replace(tmp, job.path)
            job.result = "ok"
        except Exception as e:
            job.result = "error"
            job.error = f"{type(e).__name__}: {e}"[:200]
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            with self._lock:
                # 失败的 Job 从表里摘掉，下次 request 会重建（这就是重试）；成功的留着直到没人订阅
                if job.result != "ok":
                    self._jobs.pop(job.path, None)
            job.done.set()

    async def _download(self, job, tmp):
        import edge_tts
        communicate = edge_tts.Communicate(job.text, job.voice, rate=job.rate_str)
        await asyncio.wait_for(communicate.save(tmp), timeout=DOWNLOAD_TIMEOUT_S)

    def forget_done(self, job):
        with self._lock:
            if self._jobs.get(job.path) is job and job.result == "ok" and not job.subscribers:
                self._jobs.pop(job.path, None)

    def manage_cache(self, protect=()):
        """超过 500 MB 就按 mtime 删到 400 MB。保护：正在下载的、调用方点名的（当前 session 要播的）。
        顺便扫掉旧版留下的 .error 和超过 10 分钟的孤儿 .tmp。"""
        try:
            with self._lock:
                busy = set(self._jobs.keys())
            protect = set(protect) | busy
            now = time.time()
            files = []
            for name in os.listdir(self.cache_dir):
                p = os.path.join(self.cache_dir, name)
                if name.endswith(".error"):
                    _try_remove(p)
                elif name.endswith(".tmp"):
                    try:
                        if now - os.path.getmtime(p) > 600:
                            _try_remove(p)
                    except OSError:
                        pass
                elif name.endswith(".mp3"):
                    files.append(p)
            total = 0
            sized = []
            for p in files:
                try:
                    sz = os.path.getsize(p)
                    sized.append((os.path.getmtime(p), sz, p))
                    total += sz
                except OSError:
                    pass
            if total <= MAX_CACHE_MB * 1024 * 1024:
                return
            sized.sort()
            for _mtime, sz, p in sized:
                if total <= CACHE_TARGET_MB * 1024 * 1024:
                    break
                if p in protect:
                    continue
                if _try_remove(p):
                    total -= sz
        except Exception:
            pass

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


def _try_remove(p):
    try:
        os.remove(p)
        return True
    except OSError:
        return False


# ====================================================================
# worker
# ====================================================================

class TTSWorker(threading.Thread):
    """消息（全部放进 events 队列，UI 用定时器 drain）：
    ("ready", [(voice_id, name)])           本机语音枚举完成（失败时列表为空）
    ("init_error", msg)                      SAPI 初始化失败；神经音色仍可用
    ("audio_error", msg)                     pygame 初始化失败；神经音色不可用
    ("highlight", sid, idx)
    ("skipped", sid, idx, reason)            这句没读，原因
    ("done", sid, status, skipped_list)      status ∈ finished / cancelled / failed
    """

    def __init__(self, events, cache_dir=DEFAULT_CACHE_DIR, downloader=None):
        super().__init__(daemon=True, name="tts-worker")
        self.events = events
        self.cmd_queue = queue.Queue()
        self.registry = SessionRegistry()
        self.downloader = downloader or EdgeTTSDownloader(cache_dir)

        self._settings_lock = threading.Lock()
        self._voice_id = None
        self._rate = 200

        self.speaker = None
        self.voices = []
        self.init_error = None
        self._mixer_ok = None       # None 未试；True / False
        # 测试钩子：出队之后、mark_current 之前调用。A.3 的「交接窗口」测试靠它把 worker 停在那一刻
        self.on_dequeue = None
        self.start()

    # ---------- UI 线程调用 ----------

    def set_settings(self, voice_id, rate):
        with self._settings_lock:
            self._voice_id = voice_id
            self._rate = int(rate)

    def _get_settings(self):
        with self._settings_lock:
            return self._voice_id, self._rate

    def play(self, sentences, start_idx=0, target="epub", end_idx=None):
        """取消一切在跑的，登记新 session，入队。返回 session id。"""
        self.registry.cancel_all()
        self._stop_audio_now()
        session = self.registry.new(target, sentences, start_idx, end_idx)
        self.cmd_queue.put(("PLAY", session))
        return session.id

    def stop(self):
        self.registry.cancel_all()
        self._stop_audio_now()

    def _stop_audio_now(self):
        """pygame 的 stop 从任何线程调都安全（SDL 内部有锁）。SAPI 不碰 —— 那是 worker 线程的对象。"""
        if self._mixer_ok:
            try:
                import pygame
                pygame.mixer.music.stop()
            except Exception:
                pass

    def quit(self):
        self.registry.cancel_all()
        self._stop_audio_now()
        self.cmd_queue.put(("QUIT",))
        self.downloader.shutdown()

    # ---------- worker 线程 ----------

    def run(self):
        com_ok = self._init_sapi()
        try:
            while True:
                cmd = self.cmd_queue.get()
                if cmd[0] == "QUIT":
                    break
                if cmd[0] == "PLAY":
                    session = cmd[1]
                    if self.on_dequeue is not None:
                        self.on_dequeue(session)
                    if not self.registry.mark_current(session):
                        self._finish(session, "cancelled", [])
                        continue
                    self._run_session(session)
        finally:
            if com_ok:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def _init_sapi(self):
        """整体包 try：任何一步失败都要发 ready（空列表）+ init_error，主线程不能等死（B8）。"""
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
        except Exception as e:
            self.init_error = f"COM 初始化失败：{e}"
            self.events.put(("init_error", self.init_error))
            self.events.put(("ready", []))
            return False
        try:
            self.speaker = win32com.client.Dispatch("SAPI.SpVoice")
            voices_obj = self.speaker.GetVoices()
            self.voices = [(voices_obj.Item(i).Id, voices_obj.Item(i).GetDescription())
                           for i in range(voices_obj.Count)]
        except Exception as e:
            self.speaker = None
            self.voices = []
            self.init_error = f"本机语音（SAPI）不可用：{e}"
            self.events.put(("init_error", self.init_error))
        self.events.put(("ready", list(self.voices)))
        return True

    def _ensure_mixer(self):
        if self._mixer_ok is None:
            try:
                import pygame
                pygame.mixer.init()
                self._mixer_ok = True
            except Exception as e:
                self._mixer_ok = False
                self.events.put(("audio_error", f"音频输出初始化失败：{e}"))
        return self._mixer_ok

    def _finish(self, session, status, skipped):
        if self.registry.finish(session):
            self.downloader.release(session.id)
            self.downloader.release(f"prefetch:{session.id}")
            self.events.put(("done", session.id, status, list(skipped)))

    @staticmethod
    def _sentence(obj):
        if isinstance(obj, dict):
            return obj["text"].strip(), obj.get("voice_id")
        return obj.strip(), None

    @staticmethod
    def _rate_strings(rate):
        pct = int((rate - 200) / 2)
        edge = f"+{pct}%" if pct >= 0 else f"{pct}%"
        sapi = max(-10, min(10, int((rate - 200) / 15)))
        return edge, sapi

    def _run_session(self, session):
        status = "finished"
        skipped = []
        try:
            sentences = session.sentences
            last = len(sentences) - 1 if session.end_idx is None else min(session.end_idx, len(sentences) - 1)
            # 清缓存时保护本 session 要播的全部神经句子
            fallback, rate = self._get_settings()
            edge_rate, _ = self._rate_strings(rate)
            protect = set()
            for i in range(session.start_idx, last + 1):
                txt, override = self._sentence(sentences[i])
                v = override or fallback
                if txt and v in NEURAL_VOICES:
                    protect.add(self.downloader.cache_path(txt, v, edge_rate))
            self.downloader.manage_cache(protect)

            for i in range(session.start_idx, last + 1):
                if session.cancel.is_set():
                    status = "cancelled"
                    break
                txt, override = self._sentence(sentences[i])
                if not txt:
                    continue
                fallback, rate = self._get_settings()     # 每句重读：播放中改设置下一句生效
                voice = override or fallback
                edge_rate, sapi_rate = self._rate_strings(rate)
                self.events.put(("highlight", session.id, i))

                if voice in NEURAL_VOICES:
                    self._prefetch(session, sentences, i, last, fallback, edge_rate)
                    reason = self._play_neural(session, txt, voice, edge_rate)
                else:
                    reason = self._play_sapi(session, txt, voice, sapi_rate)

                if session.cancel.is_set():
                    status = "cancelled"
                    break
                if reason:
                    skipped.append((i, reason))
                    self.events.put(("skipped", session.id, i, reason))
        except Exception as e:
            status = "failed"
            skipped.append((-1, f"播放线程异常：{e}"))
        finally:
            self._finish(session, status, skipped)

    def _prefetch(self, session, sentences, i, last, fallback, edge_rate):
        for k in range(i + 1, min(i + 1 + PREFETCH_AHEAD, last + 1)):
            txt, override = self._sentence(sentences[k])
            v = override or fallback
            if txt and v in NEURAL_VOICES:
                self.downloader.request(txt, v, edge_rate, f"prefetch:{session.id}", prefetch=True)

    def _play_neural(self, session, txt, voice, edge_rate):
        """返回 None = 播了；返回字符串 = 没播的原因。取消由调用方看 session.cancel。"""
        if not self._ensure_mixer():
            return "音频输出不可用"
        job = None
        for attempt in range(MAX_RETRIES + 1):
            job = self.downloader.request(txt, voice, edge_rate, session.id)
            waited = 0.0
            while not job.done.wait(0.1):
                if session.cancel.is_set():
                    self.downloader.unsubscribe(job, session.id)
                    return None
                waited += 0.1
                if waited > WAIT_TIMEOUT_S:
                    self.downloader.unsubscribe(job, session.id)
                    return f"下载超时（{WAIT_TIMEOUT_S} 秒）"
            if job.result == "ok":
                break
            if job.result == "cancelled":
                return None
            if attempt < MAX_RETRIES:
                time.sleep(1.0)
        if job is None or job.result != "ok":
            return f"下载失败：{job.error if job else '?'}"
        path = job.path
        self.downloader.unsubscribe(job, session.id)
        self.downloader.forget_done(job)

        try:
            import pygame
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                if session.cancel.is_set():
                    pygame.mixer.music.stop()
                    break
                time.sleep(0.05)
            try:
                pygame.mixer.music.unload()
            except Exception:
                pass
            return None
        except Exception as e:
            return f"播放失败：{e}"

    def _play_sapi(self, session, txt, voice, sapi_rate):
        if self.speaker is None:
            return "本机语音不可用"
        try:
            self.speaker.Rate = sapi_rate
            voices_obj = self.speaker.GetVoices()
            for i in range(voices_obj.Count):
                if voices_obj.Item(i).Id == voice:
                    self.speaker.Voice = voices_obj.Item(i)
                    break
            self.speaker.Speak(txt, 1)          # SVSFlagsAsync
            while not self.speaker.WaitUntilDone(10):
                if session.cancel.is_set():
                    self.speaker.Speak("", 2)   # SVSFPurgeBeforeSpeak：在 worker 线程里 purge
                    break
            return None
        except Exception as e:
            return f"本机语音出错：{e}"
