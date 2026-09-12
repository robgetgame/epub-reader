"""bookmarks.json 的读写。

第 1 步（2026-09-12）：底层换成 json_store.JsonStore（原子写 + .bak + 坏文件隔离），
**公开 API 和字段一个不改** —— UI 还在用 sandbox_text 那套，schema 2 在第 3 步一起换。
"""

import os

from json_store import JsonStore

CONFIG_FILE = "bookmarks.json"


def _defaults():
    return {
        "recent_files": [],          # 文件路径列表，最近的在前
        "last_file": None,
        "bookmarks": {},             # file_path -> {"chapter_idx": int, "sentence_idx": int}
        "voice_id": None,
        "speech_rate": 200,
        "sandbox_text": "",
        "sandbox_sentence_idx": 0,
        "chapter_notes": {},         # file_path -> {"chapter_idx(str)": text}
    }


def _is_nonneg_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def validate(data):
    """只查形状，不查语义（比如章号有没有越界 —— 那要开书才知道）。
    未知的顶层键放过：schema 2 会加键，旧版本程序读到新文件不能当成坏文件。
    返回问题列表，空表示合法。"""
    problems = []
    if not isinstance(data, dict):
        return ["顶层不是对象"]

    rf = data.get("recent_files", [])
    if not isinstance(rf, list) or not all(isinstance(p, str) for p in rf):
        problems.append("recent_files 不是字符串列表")

    lf = data.get("last_file")
    if lf is not None and not isinstance(lf, str):
        problems.append("last_file 不是字符串")

    bm = data.get("bookmarks", {})
    if not isinstance(bm, dict):
        problems.append("bookmarks 不是对象")
    else:
        for path, entry in bm.items():
            if (not isinstance(entry, dict)
                    or not _is_nonneg_int(entry.get("chapter_idx", 0))
                    or not _is_nonneg_int(entry.get("sentence_idx", 0))):
                problems.append(f"bookmarks[{os.path.basename(str(path))}] 形状不对")
                break

    vid = data.get("voice_id")
    if vid is not None and not isinstance(vid, str):
        problems.append("voice_id 不是字符串")

    rate = data.get("speech_rate", 200)
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        problems.append("speech_rate 不是数字")

    if not isinstance(data.get("sandbox_text", ""), str):
        problems.append("sandbox_text 不是字符串")
    if not _is_nonneg_int(data.get("sandbox_sentence_idx", 0)):
        problems.append("sandbox_sentence_idx 不是非负整数")

    notes = data.get("chapter_notes", {})
    if not isinstance(notes, dict):
        problems.append("chapter_notes 不是对象")
    else:
        for path, chapters in notes.items():
            if not isinstance(chapters, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in chapters.items()):
                problems.append(f"chapter_notes[{os.path.basename(str(path))}] 形状不对")
                break
    return problems


class ConfigManager:
    def __init__(self, path=CONFIG_FILE, read_only=False):
        self.store = JsonStore(path, _defaults, validate)
        self.store.read_only = read_only
        if read_only:
            self.store.load_readonly()
        else:
            self.store.load()
        # 文件里缺的键补默认值（老文件没有 chapter_notes 之类）；多出来的键保留
        merged = _defaults()
        merged.update(self.store.data)
        self.store.data = merged

    # 旧代码到处直接读 self.config.config[...]，保留这个名字
    @property
    def config(self):
        return self.store.data

    @property
    def load_error(self):
        return self.store.load_error

    @property
    def last_error(self):
        return self.store.last_error

    def load(self):
        return self.store.load()

    def save(self):
        """True 成功；False 失败，原因在 last_error。第 3 步起调用方据此决定要不要继续切章/关窗。"""
        ok = self.store.save()
        if not ok:
            print(f"Error saving config: {self.store.last_error}")
        return ok

    def add_recent_file(self, file_path):
        recent = self.config["recent_files"]
        if file_path in recent:
            recent.remove(file_path)
        recent.insert(0, file_path)
        self.config["recent_files"] = recent[:10]
        self.config["last_file"] = file_path
        return self.save()

    def set_bookmark(self, file_path, chapter_idx, sentence_idx):
        self.config["bookmarks"][file_path] = {
            "chapter_idx": chapter_idx,
            "sentence_idx": sentence_idx,
        }
        return self.save()

    def get_bookmark(self, file_path):
        return self.config["bookmarks"].get(file_path, {"chapter_idx": 0, "sentence_idx": 0})

    def set_voice_and_rate(self, voice_id, rate):
        self.config["voice_id"] = voice_id
        self.config["speech_rate"] = rate
        return self.save()

    def save_sandbox(self, text, sentence_idx):
        self.config["sandbox_text"] = text
        self.config["sandbox_sentence_idx"] = sentence_idx
        return self.save()

    def load_sandbox(self):
        return self.config.get("sandbox_text", ""), self.config.get("sandbox_sentence_idx", 0)

    def save_chapter_note(self, file_path, chapter_idx, text):
        notes = self.config.setdefault("chapter_notes", {})
        notes.setdefault(file_path, {})[str(chapter_idx)] = text
        return self.save()

    def load_chapter_note(self, file_path, chapter_idx):
        return self.config.get("chapter_notes", {}).get(file_path, {}).get(str(chapter_idx), "")
