"""第 2 步验收：paths.resolve_layout / migrate_legacy / InstanceLock / 只读模式。

全部在临时目录里跑：LOCALAPPDATA 被指到临时目录，旧数据来源用假文件。不碰真实数据。
"""

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


root = tempfile.mkdtemp(prefix="epubreader-paths-")
os.environ["LOCALAPPDATA"] = os.path.join(root, "LocalAppData")
os.environ["USERPROFILE"] = os.path.join(root, "Profile")

import paths
from config_manager import ConfigManager

# ---------- 1. 默认布局 ----------
print("1. 默认布局")
lay = paths.resolve_layout()
check(lay.data == os.path.join(os.environ["LOCALAPPDATA"], "EpubReader"), f"数据目录在 LOCALAPPDATA：{lay.data}")
check(lay.cache == os.path.join(lay.local, "temp_audio"), "缓存在本机目录")
check(lay.export.endswith(os.path.join("Documents", "EpubReader", "Audio")), f"导出在文档目录：{lay.export}")
check(os.path.isdir(lay.local) and os.path.isdir(lay.cache), "本机目录和缓存目录已建")
check(lay.data_ok and lay.data_problem is None, "默认数据目录可用")

# ---------- 2. config.json 指到不存在的目录 → 只读 ----------
print("2. data_dir 不可用 → 只读")
write_json(paths.config_path(), {"data_dir": os.path.join(root, "GoogleDrive", "不存在")})
lay2 = paths.resolve_layout()
check(lay2.data_ok is False and "不可用" in lay2.data_problem, f"data_ok False：{lay2.data_problem}")
check(not os.path.exists(lay2.data), "没有替使用者新建目录")
cm = ConfigManager(lay2.bookmarks_path, read_only=True)
check(cm.save() is False and "只读" in cm.last_error, f"只读模式 save 拒绝：{cm.last_error}")
check(not os.path.exists(lay2.bookmarks_path), "只读模式没写文件")
notes = paths.migrate_legacy(lay2)
check(notes and "不迁移" in notes[0], "只读模式不迁移")

# ---------- 3. data_dir 指到可用的同步目录 ----------
print("3. data_dir 可用")
sync = os.path.join(root, "GoogleDrive", "EpubReader")
os.makedirs(sync)
write_json(paths.config_path(), {"data_dir": sync, "export_dir": os.path.join(root, "Exports")})
lay3 = paths.resolve_layout()
check(lay3.data_ok and lay3.data == sync, "数据目录 = 同步目录")
check(lay3.cache == os.path.join(lay3.local, "temp_audio"), "缓存仍在本机")
check(lay3.export == os.path.join(root, "Exports"), "export_dir 生效")
os.remove(paths.config_path())

# ---------- 4. 迁移：目标空，两个来源内容不同 ----------
print("4. 迁移 · 目标空、两来源")
lay4 = paths.resolve_layout()
old_a = os.path.join(root, "old", "dist", "bookmarks.json")
old_b = os.path.join(root, "old", "bookmarks.json")
write_json(old_a, {"recent_files": ["A"], "speech_rate": 1})
write_json(old_b, {"recent_files": ["B"], "speech_rate": 2})
t = time.time()
os.utime(old_a, (t, t))          # A 最新
os.utime(old_b, (t - 100, t - 100))
a_bytes = open(old_a, "rb").read()
b_bytes = open(old_b, "rb").read()
notes = paths.migrate_legacy(lay4, sources=[("dist", old_a), ("parent", old_b)])
check(os.path.exists(lay4.bookmarks_path) and read_json(lay4.bookmarks_path)["recent_files"] == ["A"], "最新的 A 成为主文件")
migrated = [f for f in os.listdir(lay4.data) if f.startswith("migrated-parent-")]
check(len(migrated) == 1 and read_json(os.path.join(lay4.data, migrated[0]))["recent_files"] == ["B"], f"B 保留为 migrated-parent-*：{migrated}")
check(os.path.exists(os.path.join(lay4.data, paths.MIGRATION_MARKER)), "写了标记")
check(open(old_a, "rb").read() == a_bytes and open(old_b, "rb").read() == b_bytes, "原文件一字未动")
check(any("最新" in n for n in notes) and any("内容不同" in n for n in notes), f"说明文字：{notes}")
notes2 = paths.migrate_legacy(lay4, sources=[("dist", old_a), ("parent", old_b)])
check(notes2 == [] and len([f for f in os.listdir(lay4.data) if f.startswith("migrated-")]) == 1, "第二次启动：有标记，什么都不做")

# ---------- 5. 迁移：目标已有数据，不覆盖；相同内容的来源跳过 ----------
print("5. 迁移 · 目标已有")
shutil.rmtree(lay4.data)
lay5 = paths.resolve_layout()
write_json(lay5.bookmarks_path, {"recent_files": ["TARGET"]})
target_bytes = open(lay5.bookmarks_path, "rb").read()
same = os.path.join(root, "old2", "same.json")
os.makedirs(os.path.dirname(same))
shutil.copyfile(lay5.bookmarks_path, same)
notes = paths.migrate_legacy(lay5, sources=[("dist", old_a), ("cwd", same)])
check(open(lay5.bookmarks_path, "rb").read() == target_bytes, "目标没被覆盖")
migrated = sorted(f for f in os.listdir(lay5.data) if f.startswith("migrated-"))
check(len(migrated) == 1 and migrated[0].startswith("migrated-dist-"), f"不同内容的保留、相同内容的跳过：{migrated}")

# ---------- 6. 迁移中断后再启动：补齐、不重复、不覆盖 ----------
print("6. 迁移中断")
shutil.rmtree(lay5.data)
lay6 = paths.resolve_layout()
# 模拟：上次只来得及把 A 复制成主文件，标记没写
shutil.copyfile(old_a, lay6.bookmarks_path)
notes = paths.migrate_legacy(lay6, sources=[("dist", old_a), ("parent", old_b)])
check(read_json(lay6.bookmarks_path)["recent_files"] == ["A"], "已有主文件不动")
migrated = [f for f in os.listdir(lay6.data) if f.startswith("migrated-")]
check(len(migrated) == 1 and migrated[0].startswith("migrated-parent-"), f"只补了缺的 B：{migrated}")
check(os.path.exists(os.path.join(lay6.data, paths.MIGRATION_MARKER)), "这次写了标记")

# ---------- 7. 单实例锁 ----------
print("7. 单实例锁")
lock1 = paths.InstanceLock(lay6.lock_path)
lock2 = paths.InstanceLock(lay6.lock_path)
check(lock1.acquire() is True, "第一个拿到")
check(lock2.acquire() is False, "第二个拿不到")
lock1.release()
check(lock2.acquire() is True, "释放后第二个能拿到")
lock2.release()

# ---------- 8. 真实机器上的来源探测（只读，不迁移） ----------
print("8. legacy_sources 探测")
srcs = paths.legacy_sources()
print("   探测到:", [(l, p) for l, p in srcs])
check(all(os.path.isfile(p) for _, p in srcs), "探测到的都是存在的文件")

# ---------- 9. 手动导入 ----------
print("9. 手动导入")
shutil.rmtree(lay6.data)
lay9 = paths.resolve_layout()
write_json(lay9.bookmarks_path, {"recent_files": ["CUR"]})
notes = paths.import_data_file(lay9, old_b)
check(read_json(lay9.bookmarks_path)["recent_files"] == ["B"], "导入的成为主文件")
kept = [f for f in os.listdir(lay9.data) if f.startswith("migrated-current-")]
check(len(kept) == 1 and read_json(os.path.join(lay9.data, kept[0]))["recent_files"] == ["CUR"], f"当前的保留：{kept}")
check(open(old_b, "rb").read() == b_bytes, "源文件没动")
notes = paths.import_data_file(lay9, old_b)
check("完全相同" in notes[0], "重复导入相同内容：什么都不做")

shutil.rmtree(root, ignore_errors=True)
print()
if FAILS:
    print(f"FAILED {len(FAILS)}")
    sys.exit(1)
print("ALL OK")
