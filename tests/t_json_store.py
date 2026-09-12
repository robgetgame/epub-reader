"""第 1 步验收：json_store.JsonStore + config_manager.validate。

全部在临时目录里跑，不碰真实数据。真实数据文件只**读**一次用来验 validate 不会误杀。
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json_store
from json_store import JsonStore
from config_manager import ConfigManager, validate, _defaults

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


def read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def listdir(d):
    return sorted(os.listdir(d))


def fresh_dir():
    return tempfile.mkdtemp(prefix="epubreader-t-")


# ---------- 1. 正常写：临时文件不残留，第二次写才出 .bak ----------
print("1. 正常写")
d = fresh_dir()
p = os.path.join(d, "b.json")
s = JsonStore(p, _defaults, validate)
s.load()
s.data["speech_rate"] = 250
check(s.save(), "第一次 save 成功")
check(listdir(d) == ["b.json"], f"第一次写后只有主文件，没有 tmp/.bak：{listdir(d)}")
check(read(p)["speech_rate"] == 250, "内容写进去了")
s.data["speech_rate"] = 260
check(s.save(), "第二次 save 成功")
check(listdir(d) == ["b.json", "b.json.bak"], f"第二次写后出现 .bak：{listdir(d)}")
check(read(p + ".bak")["speech_rate"] == 250, ".bak 是上一版")
check(read(p)["speech_rate"] == 260, "主文件是新版")
shutil.rmtree(d)

# ---------- 2. 写到一半失败：主文件不变，tmp 清掉 ----------
print("2. 写失败")
d = fresh_dir()
p = os.path.join(d, "b.json")
s = JsonStore(p, _defaults, validate)
s.load()
s.data["speech_rate"] = 111
s.save()
s.data["speech_rate"] = 222
real_replace = json_store.os.replace
calls = {"n": 0}


def failing_replace(a, b):
    calls["n"] += 1
    raise OSError("模拟磁盘满")


json_store.os.replace = failing_replace
try:
    ok = s.save()
finally:
    json_store.os.replace = real_replace
check(ok is False, "save 返回 False")
check("模拟磁盘满" in (s.last_error or ""), f"last_error 有原因：{s.last_error}")
check(read(p)["speech_rate"] == 111, "主文件仍是旧内容")
check(listdir(d) == ["b.json"], f"没有 tmp 残留：{listdir(d)}")
# 恢复后再写能成功
check(s.save() and read(p)["speech_rate"] == 222, "故障排除后重写成功")
shutil.rmtree(d)

# ---------- 3. 主文件坏 + .bak 好 → 恢复，坏文件隔离 ----------
print("3. 主坏备好")
d = fresh_dir()
p = os.path.join(d, "b.json")
s = JsonStore(p, _defaults, validate)
s.load(); s.data["speech_rate"] = 1; s.save(); s.data["speech_rate"] = 2; s.save()
with open(p, "w", encoding="utf-8") as f:
    f.write('{"recent_files": [1, 2')  # 截断的 JSON
s2 = JsonStore(p, _defaults, validate)
check(s2.load(), "load 返回 True（恢复成功）")
check(s2.loaded_from_bak, "标记 loaded_from_bak")
check(s2.data["speech_rate"] == 1, "数据来自 .bak")
files = listdir(d)
check(any(f.startswith("b.json.corrupt-") for f in files), f"坏文件被隔离：{files}")
check(os.path.exists(p) and read(p)["speech_rate"] == 1, "主文件已从 .bak 恢复")
check(os.path.exists(p + ".bak"), ".bak 原地保留")
check(s2.load_error and "备份" in s2.load_error, f"load_error 有说明：{s2.load_error}")
# 恢复后再 save：.bak 被轮换成刚恢复的那份（是好数据），不是坏的
s2.data["speech_rate"] = 3
check(s2.save(), "恢复后 save 成功")
check(read(p + ".bak")["speech_rate"] == 1, "轮换出的 .bak 是验证过的好数据")
shutil.rmtree(d)

# ---------- 4. 主文件缺 + .bak 好（第 2 次 replace 前崩溃的形态） ----------
print("4. 主缺备好")
d = fresh_dir()
p = os.path.join(d, "b.json")
s = JsonStore(p, _defaults, validate)
s.load(); s.data["speech_rate"] = 7; s.save(); s.data["speech_rate"] = 8; s.save()
os.remove(p)
s2 = JsonStore(p, _defaults, validate)
check(s2.load() and s2.data["speech_rate"] == 7, "从 .bak 恢复")
check(not any(f.startswith("b.json.corrupt-") for f in listdir(d)), "主文件缺时没有隔离文件")
shutil.rmtree(d)

# ---------- 5. 两个都坏：都保留，降级，不轮换 .bak ----------
print("5. 两个都坏")
d = fresh_dir()
p = os.path.join(d, "b.json")
with open(p, "w", encoding="utf-8") as f:
    f.write("garbage")
with open(p + ".bak", "w", encoding="utf-8") as f:
    f.write('{"bookmarks": "不是对象"}')  # JSON 合法但形状不对
s = JsonStore(p, _defaults, validate)
check(s.load() is False, "load 返回 False")
check(s.degraded, "degraded")
check(s.data == _defaults(), "用默认值")
files = listdir(d)
check(any(f.startswith("b.json.corrupt-") for f in files), f"坏主文件隔离：{files}")
check("b.json.bak" in files, ".bak 原样保留")
bak_before = open(p + ".bak", encoding="utf-8").read()
s.data["speech_rate"] = 999
check(s.save(), "降级状态下 save 仍能写主文件")
check(open(p + ".bak", encoding="utf-8").read() == bak_before, "降级会话不轮换 .bak（坏 .bak 没被空数据盖掉，也没被改）")
s.data["speech_rate"] = 1000
s.save()
check(open(p + ".bak", encoding="utf-8").read() == bak_before, "第二次 save 也不轮换")
shutil.rmtree(d)

# ---------- 6. 内存数据不合法 → 拒绝写 ----------
print("6. 拒绝写非法数据")
d = fresh_dir()
p = os.path.join(d, "b.json")
s = JsonStore(p, _defaults, validate)
s.load(); s.data["speech_rate"] = 5; s.save()
s.data["bookmarks"] = ["坏"]
check(s.save() is False and "不合法" in s.last_error, f"拒绝：{s.last_error}")
check(read(p)["speech_rate"] == 5, "主文件没动")
shutil.rmtree(d)

# ---------- 7. ConfigManager 旧 API 不变 ----------
print("7. ConfigManager API")
d = fresh_dir()
p = os.path.join(d, "bookmarks.json")
cm = ConfigManager(p)
check(cm.add_recent_file("X.epub") is True, "add_recent_file 返回 True")
cm.set_bookmark("X.epub", 3, 4)
cm.save_chapter_note("X.epub", 3, "笔记")
cm.save_sandbox("草稿", 2)
cm2 = ConfigManager(p)
check(cm2.get_bookmark("X.epub") == {"chapter_idx": 3, "sentence_idx": 4}, "书签读回")
check(cm2.load_chapter_note("X.epub", 3) == "笔记", "笔记读回")
check(cm2.load_sandbox() == ("草稿", 2), "sandbox 读回")
check(cm2.config["recent_files"] == ["X.epub"] and cm2.config["last_file"] == "X.epub", "recent 读回")
# 缺键的老文件：补默认值，多余键保留
with open(p, "w", encoding="utf-8") as f:
    json.dump({"recent_files": [], "future_key": {"a": 1}}, f)
cm3 = ConfigManager(p)
check(cm3.config["chapter_notes"] == {} and cm3.config["speech_rate"] == 200, "缺键补默认")
check(cm3.config["future_key"] == {"a": 1}, "未知键保留")
shutil.rmtree(d)

# ---------- 8. 真实数据文件（只读）能通过 validate ----------
print("8. 真实文件 validate")
real = [
    r"C:\Users\rober\Projects\epub reader-backup-2026-09-12\dist\bookmarks.json",
    r"C:\Users\rober\Projects\epub reader-backup-2026-09-12\bookmarks.json",
]
for rp in real:
    if os.path.exists(rp):
        probs = validate(read(rp))
        check(probs == [], f"{os.path.basename(os.path.dirname(rp))}/bookmarks.json 通过：{probs}")
    else:
        print("  skip (不存在):", rp)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}")
    sys.exit(1)
print("ALL OK")
