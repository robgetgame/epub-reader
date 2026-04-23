import threading
import queue
import win32com.client
import pythoncom
import time
import os
import hashlib
import asyncio
import edge_tts
import pygame

# Initialize pygame mixer
pygame.mixer.init()

TEMP_AUDIO_DIR = "temp_audio"
if not os.path.exists(TEMP_AUDIO_DIR):
    os.makedirs(TEMP_AUDIO_DIR)

MAX_CACHE_MB = 500

NEURAL_VOICES = [
    'zh-CN-YunyangNeural',
    'en-US-JennyNeural',
    'zh-CN-XiaoxiaoNeural',
    'zh-CN-YunxiNeural',
    'en-US-AriaNeural',
    'en-US-GuyNeural'
]

class EdgeTTSDownloader:
    def __init__(self):
        pass
        
    def manage_cache(self):
        try:
            files = [os.path.join(TEMP_AUDIO_DIR, f) for f in os.listdir(TEMP_AUDIO_DIR) if f.endswith('.mp3')]
            total_size = sum(os.path.getsize(f) for f in files)
            
            if total_size > MAX_CACHE_MB * 1024 * 1024:
                files.sort(key=os.path.getmtime)
                target_size = 400 * 1024 * 1024
                while total_size > target_size and files:
                    oldest_file = files.pop(0)
                    try:
                        size = os.path.getsize(oldest_file)
                        os.remove(oldest_file)
                        total_size -= size
                    except Exception:
                        pass
        except Exception:
            pass

    async def _download_sentence(self, text, voice, rate_str, out_path):
        # We write to a temporary .tmp file first, so os.path.exists isn't triggered prematurely
        tmp_path = out_path + ".tmp"
        err_path = out_path + ".error"
        if not os.path.exists(out_path):
            try:
                communicate = edge_tts.Communicate(text, voice, rate=rate_str)
                await communicate.save(tmp_path)
                if os.path.getsize(tmp_path) > 0:
                    os.rename(tmp_path, out_path)
                else:
                    os.remove(tmp_path)
            except Exception as e:
                # Create error marker file
                try:
                    with open(err_path, 'w') as f:
                        f.write(str(e))
                except:
                    pass
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except:
                        pass
                
    def trigger_download(self, text, voice, rate_str, out_path):
        if os.path.exists(out_path):
            return
            
        def _job():
            try:
                # Use asyncio.run inside a fresh thread. On Windows, this correctly sets up the ProactorEventLoop.
                asyncio.run(self._download_sentence(text, voice, rate_str, out_path))
            except Exception as e:
                pass
        threading.Thread(target=_job, daemon=True).start()

class TTSWorker(threading.Thread):
    def __init__(self, highlight_callback, chapter_done_callback):
        super().__init__(daemon=True)
        self.cmd_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.highlight_callback = highlight_callback
        self.chapter_done_callback = chapter_done_callback
        
        self.ready_event = threading.Event()
        self.voices = []
        self.speaker = None
        self.downloader = EdgeTTSDownloader()
        
        self.start()
        
    def run(self):
        # Initialize COM for this thread specifically
        pythoncom.CoInitialize()
        
        self.speaker = win32com.client.Dispatch("SAPI.SpVoice")
        
        voices_obj = self.speaker.GetVoices()
        for i in range(voices_obj.Count):
            voice = voices_obj.Item(i)
            self.voices.append((voice.Id, voice.GetDescription()))
            
        self.ready_event.set()
        
        while True:
            try:
                cmd = self.cmd_queue.get()
                if cmd[0] == 'QUIT':
                    break
                elif cmd[0] == 'PLAY':
                    _, voice_id, rate, sentences, start_idx = cmd
                    self._handle_play(voice_id, rate, sentences, start_idx)
                elif cmd[0] == 'SET_RATE':
                    # Active modification of SAPI rate. Neural rate takes effect on next playback.
                    rate = cmd[1]
                    mapped_rate = int((rate - 200) / 15)
                    mapped_rate = max(-10, min(10, mapped_rate))
                    if self.speaker:
                        self.speaker.Rate = mapped_rate
            except Exception as e:
                print(f"TTS Thread error: {e}")
                
        pythoncom.CoUninitialize()

    def _get_cache_path(self, text, voice, rate_str):
        # generate a unique filename
        hash_str = hashlib.md5(f"{text}_{voice}_{rate_str}".encode('utf-8')).hexdigest()
        return os.path.join(TEMP_AUDIO_DIR, f"{hash_str}.mp3")
        
    def _handle_play(self, fallback_voice_id, rate, sentences, start_idx):
        self.stop_event.clear()
        
        # Rates
        pct = int((rate - 200) / 2)
        rate_str = f"+{pct}%" if pct >= 0 else f"{pct}%"
        
        sapi_rate = max(-10, min(10, int((rate - 200) / 15)))
        if self.speaker:
            self.speaker.Rate = sapi_rate
            
        def set_sapi_voice(vid):
            if not self.speaker: return
            try:
                voices_obj = self.speaker.GetVoices()
                for i in range(voices_obj.Count):
                    if voices_obj.Item(i).Id == vid:
                        self.speaker.Voice = voices_obj.Item(i)
                        break
            except Exception: pass
            
        self.downloader.manage_cache()
        played_to_end = True
        
        for i in range(start_idx, len(sentences)):
            if self.stop_event.is_set():
                played_to_end = False
                break
                
            sentence_obj = sentences[i]
            if type(sentence_obj) is dict:
                txt = sentence_obj['text'].strip()
                override = sentence_obj.get('voice_id')
                if override == 'SKIP':
                    if self.highlight_callback:
                        self.highlight_callback(i)
                    continue
                current_voice = override if override else fallback_voice_id
            else:
                txt = sentence_obj.strip()
                current_voice = fallback_voice_id
                
            if not txt: continue
            
            if self.highlight_callback:
                self.highlight_callback(i)
                
            if current_voice in NEURAL_VOICES:
                # Pre-trigger cache for upcoming Neural sentences
                for offset in range(1, 4):
                    if i + offset < len(sentences):
                        nxt_obj = sentences[i + offset]
                        nxt_txt = nxt_obj['text'].strip() if type(nxt_obj) is dict else nxt_obj.strip()
                        nxt_v = (nxt_obj.get('voice_id') or fallback_voice_id) if type(nxt_obj) is dict else fallback_voice_id
                        if nxt_txt and nxt_v in NEURAL_VOICES and nxt_v != 'SKIP':
                            nxt_path = self._get_cache_path(nxt_txt, nxt_v, rate_str)
                            self.downloader.trigger_download(nxt_txt, nxt_v, rate_str, nxt_path)
                            
                out_path = self._get_cache_path(txt, current_voice, rate_str)
                self.downloader.trigger_download(txt, current_voice, rate_str, out_path)
                
                wait_time = 0
                err_path = out_path + ".error"
                while not os.path.exists(out_path):
                    if self.stop_event.is_set() or os.path.exists(err_path):
                        played_to_end = False
                        break
                    time.sleep(0.05)
                    wait_time += 0.05
                    if wait_time > 15: break
                    
                if self.stop_event.is_set() or not os.path.exists(out_path):
                    continue
                    
                try:
                    pygame.mixer.music.load(out_path)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy():
                        if self.stop_event.is_set():
                            pygame.mixer.music.stop()
                            played_to_end = False
                            break
                        time.sleep(0.05)
                    try:
                        pygame.mixer.music.unload()
                    except: pass
                except Exception:
                    pass
            else:
                # SAPI5 Processing
                set_sapi_voice(current_voice)
                try:
                    self.speaker.Speak(txt, 1)
                    while not self.speaker.WaitUntilDone(10):
                        if self.stop_event.is_set():
                            self.speaker.Speak("", 2)
                            played_to_end = False
                            break
                except Exception:
                    pass
                    
        if played_to_end and not self.stop_event.is_set():
            if self.chapter_done_callback:
                self.chapter_done_callback()
            
    def play(self, voice_id, rate, sentences, start_idx=0):
        self.stop_event.set()
        
        while not self.cmd_queue.empty():
            try:
                self.cmd_queue.get_nowait()
            except queue.Empty:
                break
                
        self.cmd_queue.put(('PLAY', voice_id, rate, sentences, start_idx))
        
    def stop(self):
        self.stop_event.set()
        if self.speaker:
            try:
                self.speaker.Speak("", 2)
            except Exception:
                pass
        try:
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
        except Exception:
            pass
            
    def set_rate(self, rate):
        self.cmd_queue.put(('SET_RATE', rate))
        
    def get_voices(self):
        self.ready_event.wait()
        all_voices = []
        for nv in NEURAL_VOICES:
            all_voices.append((nv, nv + " (High Quality Online Neural)"))
            
        for v in self.voices:
            all_voices.append(v)
            
        return all_voices
    
    def quit(self):
        self.stop_event.set()
        self.cmd_queue.put(('QUIT',))
