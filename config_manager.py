"""bookmarks.json 的读写，schema 2。

schema 2（第 3 步，2026-09-12）跟 v1 的差别：
- 书签存 (chapter, offset)：chapter 是目录条目序号，offset 是章内字符偏移。
  v1 存的是 (spine 文件序号, 句序号)，断句算法一换句序号就没意义了。
- 笔记按 (book_key, chapter_key) 存在 notes 里；没开书时 book_key = "__scratch__"。
  v1 的全局 sandbox_text 取消 —— 它和 chapter_notes 互相覆盖是 B1 的根因。
- note_positions：笔记的朗读位置，按 (book_key, chapter_key) 存字符偏移。
- v1 的 bookmarks / chapter_notes / sandbox_text 整个搬进 migrated_v1 **原样保留**，不自动换算
  （D18：一本书、十几条笔记，为它写通用换算器不值；使用者从「旧笔记」菜单手动复制）。
"""

import os
import time

from json_store import JsonStore

CONFIG_FILE = "bookmarks.json"
SCHEMA = 2
SCRATCH_BOOK = "__scratch__"


def _defaults():
    return {
        "schema": SCHEMA,
        "recent_files": [],
        "last_file": None,
        "voice_id": None,
        "speech_rate": 200,
        "volume": 100,          # D24
        "font_family": "Microsoft YaHei",   # D22
        "font_size": 16,
        "bookmarks": {},        # book_key -> {"chapter": int, "offset": int}
        "notes": {},            # book_key -> {chapter_key: text}
        "note_positions": {},   # book_key -> {chapter_key: offset}
        "book_meta": {},        # book_key -> {"kind": ..., "language": ..., "kind_source": ...}（第 8 步用）
        "ai_usage": {},         # 第 8 步用
        "migrated_v1": None,    # v1 数据原样；None 表示这个文件不是从 v1 来的
    }


def _is_nonneg_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_str_dict_of_str(d):
    return isinstance(d, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in d.items())


def validate(data):
    """只查形状。未知顶层键放过。既接受 v1 也接受 v2 —— v1 文件读进来后由 ConfigManager 迁移。"""
    problems = []
    if not isinstance(data, dict):
        return ["顶层不是对象"]

    rf = data.get("recent_files", [])
    if not isinstance(rf, list) or not all(isinstance(p, str) for p in rf):
        problems.append("recent_files 不是字符串列表")
    lf = data.get("last_file")
    if lf is not None and not isinstance(lf, str):
        problems.append("last_file 不是字符串")
    vid = data.get("voice_id")
    if vid is not None and not isinstance(vid, str):
        problems.append("voice_id 不是字符串")
    rate = data.get("speech_rate", 200)
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        problems.append("speech_rate 不是数字")
    vol = data.get("volume", 100)
    if not isinstance(vol, (int, float)) or isinstance(vol, bool) or not 0 <= vol <= 100:
        problems.append("volume 不在 0–100")
    if not isinstance(data.get("font_family", ""), str):
        problems.append("font_family 不是字符串")
    fs = data.get("font_size", 16)
    if not isinstance(fs, (int, float)) or isinstance(fs, bool) or not 8 <= fs <= 72:
        problems.append("font_size 不在 8–72")

    schema = data.get("schema")
    if schema is None:
        problems += _validate_v1(data)
    elif schema == SCHEMA:
        problems += _validate_v2(data)
    else:
        problems.append(f"不认识的 schema {schema!r}")
    return problems


def _validate_v1(data):
    problems = []
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
    if not isinstance(data.get("sandbox_text", ""), str):
        problems.append("sandbox_text 不是字符串")
    if not _is_nonneg_int(data.get("sandbox_sentence_idx", 0)):
        problems.append("sandbox_sentence_idx 不是非负整数")
    notes = data.get("chapter_notes", {})
    if not isinstance(notes, dict) or not all(_is_str_dict_of_str(v) for v in notes.values()):
        problems.append("chapter_notes 形状不对")
    return problems


def _validate_v2(data):
    problems = []
    bm = data.get("bookmarks", {})
    if not isinstance(bm, dict):
        problems.append("bookmarks 不是对象")
    else:
        for path, entry in bm.items():
            if (not isinstance(entry, dict)
                    or not _is_nonneg_int(entry.get("chapter", 0))
                    or not _is_nonneg_int(entry.get("offset", 0))):
                problems.append(f"bookmarks[{os.path.basename(str(path))}] 形状不对")
                break
    notes = data.get("notes", {})
    if not isinstance(notes, dict) or not all(_is_str_dict_of_str(v) for v in notes.values()):
        problems.append("notes 形状不对")
    pos = data.get("note_positions", {})
    if not isinstance(pos, dict) or not all(
            isinstance(v, dict) and all(isinstance(k, str) and _is_nonneg_int(o) for k, o in v.items())
            for v in pos.values()):
        problems.append("note_positions 形状不对")
    for key in ("book_meta", "ai_usage"):
        if not isinstance(data.get(key, {}), dict):
            problems.append(f"{key} 不是对象")
    mv1 = data.get("migrated_v1")
    if mv1 is not None and not isinstance(mv1, dict):
        problems.append("migrated_v1 不是对象")
    return problems


def migrate_v1_to_v2(data):
    """v1 dict → v2 dict。纯函数，不碰文件。v1 的四个字段原样进 migrated_v1。"""
    out = _defaults()
    for k in ("recent_files", "last_file", "voice_id", "speech_rate"):
        if k in data:
            out[k] = data[k]
    out["migrated_v1"] = {
        "migrated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bookmarks": data.get("bookmarks", {}),
        "chapter_notes": data.get("chapter_notes", {}),
        "sandbox_text": data.get("sandbox_text", ""),
        "sandbox_sentence_idx": data.get("sandbox_sentence_idx", 0),
    }
    # v1 里没见过的键也带着，不丢
    for k, v in data.items():
        if k not in out and k not in ("bookmarks", "chapter_notes", "sandbox_text", "sandbox_sentence_idx"):
            out[k] = v
    return out


def _keep_v1_copy(path):
    """把 v1 文件复制成 bookmarks.v1-<时间>.json，永不轮换。复制失败返回 None（不阻止迁移，但 UI 会提）。"""
    if not os.path.exists(path):
        return None
    base, ext = os.path.splitext(path)
    dst = f"{base}.v1-{time.strftime('%Y%m%d-%H%M%S')}{ext}"
    try:
        import shutil
        shutil.copyfile(path, dst)
        return dst
    except OSError:
        return None


class ConfigManager:
    def __init__(self, path=CONFIG_FILE, read_only=False):
        self.store = JsonStore(path, _defaults, validate)
        self.store.read_only = read_only
        if read_only:
            self.store.load_readonly()
        else:
            self.store.load()

        self.migrated_now = False
        self.v1_backup = None
        data = self.store.data
        if data.get("schema") is None and not read_only:
            # v1 原件先另存一份：.bak 下次保存就会被轮换掉，靠不住
            self.v1_backup = _keep_v1_copy(path)
            self.store.data = migrate_v1_to_v2(data)
            self.migrated_now = True
            self.store.save()
        merged = _defaults()
        merged.update(self.store.data)
        self.store.data = merged

    @property
    def config(self):
        return self.store.data

    @property
    def load_error(self):
        return self.store.load_error

    @property
    def last_error(self):
        return self.store.last_error

    def save(self):
        """True 成功；False 失败，原因在 last_error。切章 / 关窗前保存失败就不能继续（A.1）。"""
        ok = self.store.save()
        if not ok:
            print(f"Error saving config: {self.store.last_error}")
        return ok

    # ---------- 最近文件 / 音色 ----------

    def add_recent_file(self, file_path):
        recent = self.config["recent_files"]
        if file_path in recent:
            recent.remove(file_path)
        recent.insert(0, file_path)
        self.config["recent_files"] = recent[:10]
        self.config["last_file"] = file_path
        return self.save()

    def set_voice_and_rate(self, voice_id, rate, volume=None):
        self.config["voice_id"] = voice_id
        self.config["speech_rate"] = rate
        if volume is not None:
            self.config["volume"] = int(volume)
        return self.save()

    def set_font(self, family, size):
        self.config["font_family"] = family
        self.config["font_size"] = int(size)
        return self.save()

    # ---------- 书的类型（D25） ----------

    def get_book_meta(self, book_key):
        return dict(self.config["book_meta"].get(book_key) or {})

    def set_book_kind(self, book_key, kind, source, language=None):
        meta = self.config["book_meta"].setdefault(book_key, {})
        meta["kind"] = kind
        meta["kind_source"] = source
        if language:
            meta["language"] = language
        return self.save()

    # ---------- 书签（章序号 + 字符偏移） ----------

    def set_bookmark(self, book_key, chapter, offset):
        self.config["bookmarks"][book_key] = {"chapter": int(chapter), "offset": int(offset)}
        return self.save()

    def get_bookmark(self, book_key):
        entry = self.config["bookmarks"].get(book_key) or {}
        return int(entry.get("chapter", 0)), int(entry.get("offset", 0))

    # ---------- 笔记 ----------

    def get_note(self, book_key, chapter_key):
        return self.config["notes"].get(book_key, {}).get(str(chapter_key), "")

    def set_note(self, book_key, chapter_key, text):
        """空文本就删掉这条，免得 notes 里堆一堆空串。"""
        notes = self.config["notes"]
        chapter_key = str(chapter_key)
        if text:
            notes.setdefault(book_key, {})[chapter_key] = text
        else:
            book = notes.get(book_key)
            if book:
                book.pop(chapter_key, None)
                if not book:
                    notes.pop(book_key, None)
        return self.save()

    def get_note_position(self, book_key, chapter_key):
        return int(self.config["note_positions"].get(book_key, {}).get(str(chapter_key), 0))

    def set_note_position(self, book_key, chapter_key, offset, save=True):
        self.config["note_positions"].setdefault(book_key, {})[str(chapter_key)] = int(offset)
        return self.save() if save else True

    # ---------- v1 旧数据（只读） ----------

    def legacy(self):
        """None 或 migrated_v1 dict。「旧笔记」菜单用。"""
        return self.config.get("migrated_v1")
