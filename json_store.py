"""带备份的原子 JSON 存储。bookmarks.json / ai_chat.json 都走这里。

为什么要单独写一个：原来 `open(path, 'w')` 是先把唯一的数据文件截断成 0 字节，再序列化。
中途崩溃、磁盘满、杀进程，全部笔记和书签就没了；下次启动读不出来就用默认值继续，
再一保存把坏文件盖掉 —— 数据连尸体都不剩。设计文档 B0 / A.1。

规则（A.1）：
- 写：临时文件 → fsync → 把旧主文件轮换成 .bak → 临时文件 os.replace 成主文件。
  .bak 只在「旧主文件是验证过的好数据」时才轮换，坏文件永远不会成为 .bak。
- 读：主文件坏或缺 → 试 .bak；.bak 好就恢复，坏文件改名 .corrupt-<时间> 留着；
  两个都坏 → 两个都留、本次会话用默认值但 **不轮换 .bak**（防止把空默认值轮换成「最后好版本」）。
- 任何失败都不抛给调用方：save() 返回 False，last_error 里有原因。调用方决定要不要停下来。
"""

import json
import os
import shutil
import threading
import time
import uuid


class JsonStore:
    def __init__(self, path, defaults, validate):
        """
        path      主文件路径
        defaults  返回默认 dict 的函数（每次调用给新对象，避免共享可变默认值）
        validate  validate(data) -> list[str]，空列表表示合法；只检查形状，不改数据
        """
        self.path = path
        self.bak_path = path + ".bak"
        self._defaults = defaults
        self._validate = validate
        self._lock = threading.RLock()

        self.data = defaults()
        self.load_error = None       # 给 UI 显示的中文说明；None 表示读得干净
        self.last_error = None       # 最近一次 save 失败的原因
        # 主文件是不是「验证过的好数据」。False 时 save() 不把它轮换成 .bak。
        self._main_is_good = False
        self.loaded_from_bak = False
        self.degraded = False        # 主文件和 .bak 都坏，本次会话用的是默认值
        self.read_only = False       # 数据目录不可用时由调用方置 True：save() 一律拒绝，不在别处新建

    # ---------- 读 ----------

    def _read_valid(self, path):
        """读并校验；返回 (data, None) 或 (None, 原因)。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None, "不存在"
        except Exception as e:  # JSON 坏、编码坏、权限
            return None, f"读取失败：{e}"
        problems = self._validate(data)
        if problems:
            return None, "内容不合法：" + "；".join(problems[:3])
        return data, None

    def _quarantine(self, path):
        """坏文件改名留着，不删。改名失败也不抛 —— 最坏就是它还在原地。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = f"{path}.corrupt-{stamp}"
        try:
            os.replace(path, target)
            return target
        except OSError:
            return None

    def load_readonly(self):
        """只读模式：能读就读，读不了就用默认值。不隔离、不恢复、不写任何东西。"""
        with self._lock:
            data, why = self._read_valid(self.path)
            if data is not None:
                self.data = data
                return True
            self.data = self._defaults()
            self.load_error = None if why == "不存在" else f"只读模式下无法读取数据文件：{why}"
            return False

    def load(self):
        with self._lock:
            self.load_error = None
            self.loaded_from_bak = False
            self.degraded = False
            self._main_is_good = False

            data, why_main = self._read_valid(self.path)
            if data is not None:
                self.data = data
                self._main_is_good = True
                return True

            main_missing = why_main == "不存在"
            bak, why_bak = self._read_valid(self.bak_path)
            if bak is not None:
                # 主文件坏（或写到一半崩了、主文件缺）而 .bak 好：先把坏的挪开，再把 .bak 复制回主文件。
                # 用 copy 不用 rename，.bak 原地留着 —— 万一恢复出来的主文件又出事还有它。
                quarantined = None if main_missing else self._quarantine(self.path)
                try:
                    shutil.copyfile(self.bak_path, self.path)
                    self._main_is_good = True
                except OSError as e:
                    self.last_error = f"从备份恢复失败：{e}"
                self.data = bak
                self.loaded_from_bak = True
                if main_missing:
                    self.load_error = "主数据文件不存在，已从备份 .bak 恢复"
                else:
                    self.load_error = (f"主数据文件损坏（{why_main}），已从备份 .bak 恢复；"
                                       f"坏文件保留为 {os.path.basename(quarantined) if quarantined else '原文件'}")
                return True

            if main_missing and why_bak == "不存在":
                # 全新安装，什么都没有：正常
                self.data = self._defaults()
                self._main_is_good = False   # 还没写过，没东西可轮换
                return True

            # 两个都坏：坏主文件改名留着（否则下次 save 会盖掉它），.bak 原地不动且本次不轮换
            quarantined = None if main_missing else self._quarantine(self.path)
            self.data = self._defaults()
            self.degraded = True
            self.load_error = (f"数据文件和备份都无法读取（主文件：{why_main}；备份：{why_bak}）。"
                               f"坏文件保留为 {os.path.basename(quarantined) if quarantined else '原文件'}，"
                               "备份原样保留；本次以空数据启动，不会覆盖备份。")
            return False

    # ---------- 写 ----------

    def save(self):
        """成功 True，失败 False（原因在 last_error）。失败时主文件保证不变。"""
        with self._lock:
            self.last_error = None
            if self.read_only:
                self.last_error = "只读模式：数据目录不可用，本次不保存"
                return False
            problems = self._validate(self.data)
            if problems:
                # 自己产生的数据都不合法 —— 是代码 bug，不能写盘把好文件盖掉
                self.last_error = "拒绝写入：内存数据不合法：" + "；".join(problems[:3])
                return False

            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            tmp = os.path.join(directory, f".{os.path.basename(self.path)}.{uuid.uuid4().hex}.tmp")
            try:
                os.makedirs(directory, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())

                # 轮换：只有主文件是验证过的好数据才让它变成 .bak
                if self._main_is_good and not self.degraded and os.path.exists(self.path):
                    os.replace(self.path, self.bak_path)

                os.replace(tmp, self.path)
                self._main_is_good = True
                return True
            except Exception as e:
                self.last_error = f"保存失败：{e}"
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                return False
