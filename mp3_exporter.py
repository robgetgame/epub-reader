"""把一章导出成 mp3（神经音色）。第 6 步（2026-09-12）改动，对应 B9 / D20 / A.7：

- 文件名带三位章序号：`021 第021章 标题.mp3`、`021 Notes.01.mp3` —— 同名章节、书名重复的 `<title>`
  不再互相覆盖；书目录名加书文件的短 hash，同名书也不撞
- 笔记片段各自一个文件，不再把独立的 mp3 流用 "ab" 拼在章节文件后面（时长元数据是错的）
- 带语速：跟播放时听到的一致
- 同名文件已存在 → 自动加序号（D20），不询问不覆盖
- 写临时文件后 os.replace，导出到一半崩了不留半个文件
- 任一片段失败 → 结果是「部分失败」并列出，不报「完成」（B7）
- 事件循环策略只在本线程的 asyncio.Runner 里设，不再改进程全局
"""

import asyncio
import hashlib
import os
import re
import threading

CHUNK_CHARS = 2500       # 一次送 edge-tts 的字数上限；超了就分段
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def safe_name(name, fallback):
    """只留字母数字空格和常见标点；Windows 保留设备名加前缀；空的用 fallback。"""
    cleaned = "".join(c for c in name if c.isalnum() or c in " -_，。！？!（）()[]【】、·").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:80].rstrip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in _RESERVED:
        cleaned = "_" + cleaned
    return cleaned


def unique_path(path):
    """已存在就加 (2)、(3)…（D20）。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while True:
        candidate = f"{base} ({n}){ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def chunk_sentences(sentences, limit=CHUNK_CHARS):
    chunks, current, length = [], [], 0
    for s in sentences:
        st = s.strip()
        if not st:
            continue
        if length + len(st) > limit and current:
            chunks.append(current)
            current, length = [st], len(st)
        else:
            current.append(st)
            length += len(st)
    if current:
        chunks.append(current)
    return chunks


def rate_string(rate):
    pct = int((rate - 200) / 2)
    return f"+{pct}%" if pct >= 0 else f"{pct}%"


def run_export_background(book_name, book_path, chapter_index, chapter_name, epub_sentences, notes_objects,
                          fallback_voice_id, rate, status_callback, done_callback, export_dir="Audio"):
    """done_callback(success: bool, summary: str)。summary 给 UI 显示；失败时列出没成功的片段。"""

    def worker():
        failures = []
        written = []
        try:
            short = hashlib.md5(os.path.abspath(book_path).encode("utf-8")).hexdigest()[:6] if book_path else "000000"
            book_dir = os.path.join(export_dir, f"{safe_name(book_name, 'Book')} [{short}]")
            os.makedirs(book_dir, exist_ok=True)
            prefix = f"{chapter_index + 1:03d} "
            chapter_stem = prefix + safe_name(chapter_name, f"Chapter {chapter_index + 1}")
            rate_str = rate_string(rate)

            async def render(text, voice):
                import edge_tts
                communicate = edge_tts.Communicate(text, voice, rate=rate_str)
                audio = b""
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio += chunk["data"]
                if not audio:
                    raise RuntimeError("edge-tts 没有返回音频")
                return audio

            def write_atomic(path, data):
                path = unique_path(path)
                tmp = path + ".part"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
                written.append(os.path.basename(path))

            async def build():
                chunks = chunk_sentences(epub_sentences)
                multi = len(chunks) > 1
                for i, chunk in enumerate(chunks):
                    status_callback(f"正文 {i + 1} / {len(chunks)}…")
                    try:
                        audio = await render(" ".join(chunk), fallback_voice_id)
                        name = f"{chapter_stem}.{i + 1:02d}.mp3" if multi else f"{chapter_stem}.mp3"
                        write_atomic(os.path.join(book_dir, name), audio)
                    except Exception as e:
                        failures.append(f"正文第 {i + 1} 段：{e}")

                spoken_notes = [n for n in notes_objects
                                if n.get("text", "").strip() and n.get("spoken", True) and n.get("voice_id") != "SKIP"]
                # 笔记按音色连续合并成段，同一音色的相邻句子放一个文件里，文件数不至于爆炸
                groups = []
                for n in spoken_notes:
                    voice = n.get("voice_id") or fallback_voice_id
                    if groups and groups[-1][0] == voice and sum(len(t) for t in groups[-1][1]) < CHUNK_CHARS:
                        groups[-1][1].append(n["text"].strip())
                    else:
                        groups.append((voice, [n["text"].strip()]))
                for i, (voice, texts) in enumerate(groups):
                    status_callback(f"笔记 {i + 1} / {len(groups)}…")
                    try:
                        audio = await render(" ".join(texts), voice)
                        write_atomic(os.path.join(book_dir, f"{prefix}Notes.{i + 1:02d}.mp3"), audio)
                    except Exception as e:
                        failures.append(f"笔记第 {i + 1} 段：{e}")

            # 事件循环只在这个线程里；不碰进程全局的 policy（旧版会影响到下载线程）
            with asyncio.Runner() as runner:
                runner.run(build())

            if failures:
                summary = f"部分失败：写了 {len(written)} 个文件，{len(failures)} 段失败：\n" + "\n".join(failures)
                done_callback(False, summary)
            elif not written:
                done_callback(False, "没有任何内容可导出")
            else:
                done_callback(True, f"导出完成，{len(written)} 个文件：\n" + "\n".join(written))
        except Exception as e:
            done_callback(False, f"导出出错：{e}")

    threading.Thread(target=worker, daemon=True, name="mp3-export").start()
