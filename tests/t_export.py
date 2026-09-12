"""第 6 步验收：mp3 导出的文件名 / 序号 / 原子写 / 部分失败上报。edge_tts 用假模块，不联网。"""

import os
import shutil
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 假 edge_tts：stream() 按文本长度吐字节；含「坏」的文本不吐（模拟无音频）
fake = types.ModuleType("edge_tts")


class Communicate:
    def __init__(self, text, voice, rate="+0%"):
        self.text, self.voice, self.rate = text, voice, rate

    async def stream(self):
        if "坏" in self.text:
            return
        yield {"type": "audio", "data": f"[{self.voice}|{self.rate}]{self.text}".encode("utf-8")}


fake.Communicate = Communicate
sys.modules["edge_tts"] = fake

import mp3_exporter
from mp3_exporter import run_export_background, safe_name, unique_path, chunk_sentences

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


def run(**kw):
    done = threading.Event()
    result = {}
    statuses = []

    def on_done(ok, summary):
        result["ok"], result["summary"] = ok, summary
        done.set()

    run_export_background(status_callback=statuses.append, done_callback=on_done, **kw)
    done.wait(10)
    return result, statuses


tmp = tempfile.mkdtemp(prefix="epubreader-export-")

print("1. 工具函数")
check(safe_name("第027章 最是能杀人！", "x") == "第027章 最是能杀人！", "中文保留")
check(safe_name("a/b\\c:d*e?", "x") == "abcde", "路径字符去掉")
check(safe_name("   ", "Fallback") == "Fallback", "空名用 fallback")
check(safe_name("CON", "x") == "_CON", "保留设备名加前缀")
check(chunk_sentences(["a" * 2000, "b" * 1000, "c"]) == [["a" * 2000], ["b" * 1000, "c"]], "分段")
p = os.path.join(tmp, "x.mp3")
open(p, "wb").close()
check(unique_path(p).endswith("x (2).mp3"), "同名加序号")

print("2. 正常导出：章序号前缀、笔记分文件、带语速")
r, st = run(book_name="雪中", book_path=os.path.join(tmp, "book.epub"), chapter_index=20, chapter_name="第021章 标题",
            epub_sentences=["第一句。", "第二句。"],
            notes_objects=[{"text": "旁白。", "voice_id": None, "spoken": True},
                           {"text": "女声。", "voice_id": "zh-CN-XiaoxiaoNeural", "spoken": True},
                           {"text": "……", "voice_id": None, "spoken": False}],
            fallback_voice_id="zh-CN-YunyangNeural", rate=271, export_dir=os.path.join(tmp, "out"))
check(r.get("ok") is True, f"成功：{r.get('summary')}")
book_dirs = os.listdir(os.path.join(tmp, "out"))
check(len(book_dirs) == 1 and book_dirs[0].startswith("雪中 ["), f"书目录带 hash：{book_dirs}")
files = sorted(os.listdir(os.path.join(tmp, "out", book_dirs[0])))
check(files == ["021 Notes.01.mp3", "021 Notes.02.mp3", "021 第021章 标题.mp3"], f"文件名：{files}")
data = open(os.path.join(tmp, "out", book_dirs[0], "021 第021章 标题.mp3"), "rb").read().decode("utf-8")
check(data.startswith("[zh-CN-YunyangNeural|+35%]"), f"带音色和语速：{data[:40]}")
n2 = open(os.path.join(tmp, "out", book_dirs[0], "021 Notes.02.mp3"), "rb").read().decode("utf-8")
check(n2.startswith("[zh-CN-XiaoxiaoNeural"), "笔记第二段用了标签音色")
check(not any(f.endswith(".part") for f in files), "没有 .part 残留")

print("3. 再导一次：不覆盖，自动加序号")
r, st = run(book_name="雪中", book_path=os.path.join(tmp, "book.epub"), chapter_index=20, chapter_name="第021章 标题",
            epub_sentences=["第一句。"], notes_objects=[], fallback_voice_id="zh-CN-YunyangNeural", rate=200,
            export_dir=os.path.join(tmp, "out"))
files = sorted(os.listdir(os.path.join(tmp, "out", book_dirs[0])))
check("021 第021章 标题 (2).mp3" in files, f"加序号：{files}")

print("4. 部分失败：报出来，不说完成")
r, st = run(book_name="雪中", book_path=os.path.join(tmp, "book.epub"), chapter_index=1, chapter_name="第二章",
            epub_sentences=["好的。"], notes_objects=[{"text": "坏的。", "voice_id": None, "spoken": True}],
            fallback_voice_id="zh-CN-YunyangNeural", rate=200, export_dir=os.path.join(tmp, "out"))
check(r.get("ok") is False and "部分失败" in r["summary"] and "笔记第 1 段" in r["summary"], f"部分失败：{r['summary']}")
files = sorted(os.listdir(os.path.join(tmp, "out", book_dirs[0])))
check("002 第二章.mp3" in files and not any(f.startswith("002 Notes") for f in files), "成功的写了、失败的没写")

print("5. 同名章不同序号不撞")
r, st = run(book_name="雪中", book_path=os.path.join(tmp, "book.epub"), chapter_index=2, chapter_name="第二章",
            epub_sentences=["x。"], notes_objects=[], fallback_voice_id="zh-CN-YunyangNeural", rate=200,
            export_dir=os.path.join(tmp, "out"))
files = sorted(os.listdir(os.path.join(tmp, "out", book_dirs[0])))
check("003 第二章.mp3" in files and "002 第二章.mp3" in files, f"序号区分：{files}")

shutil.rmtree(tmp, ignore_errors=True)
print()
if FAILS:
    print(f"FAILED {len(FAILS)}")
    sys.exit(1)
print("ALL OK")
