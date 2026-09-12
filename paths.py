r"""数据目录、缓存目录、导出目录、单实例锁、旧数据迁移。设计文档 A6 / D17 / D23 / A.1。

为什么要有这个文件：原来所有路径都是相对 CWD 的字符串（"bookmarks.json"、"temp_audio"）。
--onefile 的 exe 从快捷方式启动、从别的目录启动，CWD 都不一样，于是各处冒出一套空的
bookmarks.json —— 使用者机器上 dist\ 和仓库根目录各有一份，就是这么来的。

目录规则：
- 「本机目录」  %LOCALAPPDATA%\EpubReader\   放 config.json、缓存 temp_audio\、单实例锁。永远在本机。
- 「数据目录」  默认 = 本机目录；config.json 里 data_dir 可指到云盘同步目录（Google Drive 镜像模式）。
                放 bookmarks.json、ai_chat.json。
- 「导出目录」  默认 %USERPROFILE%\Documents\EpubReader\Audio\；config.json 里 export_dir 可改。
- --portable    全部回到程序所在目录（旧行为）。

数据目录不可用（云盘没登录、目录不存在、不可写）时：**不**退回本机新建空数据 —— 那又是两套数据。
返回 DataDirStatus(ok=False, reason)，调用方以只读方式启动并提示。
"""

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field

APP_NAME = "EpubReader"


def is_frozen():
    return getattr(sys, "frozen", False)


def app_root():
    """exe 所在目录（打包后）或源码目录。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def local_dir(portable=False):
    if portable:
        return app_root()
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, APP_NAME)


def config_path(portable=False):
    return os.path.join(local_dir(portable), "config.json")


def load_config(portable=False):
    """config.json 是使用者手写的可选文件。坏了就当没有，但把原因带回去让 UI 说出来。"""
    p = config_path(portable)
    if not os.path.exists(p):
        return {}, None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}, f"{p} 顶层不是对象，已忽略"
        return data, None
    except Exception as e:
        return {}, f"{p} 无法读取（{e}），已忽略"


@dataclass
class Layout:
    portable: bool
    local: str                 # 本机目录
    data: str                  # 数据目录
    cache: str                 # temp_audio
    export: str                # 导出 mp3
    data_dir_from_config: bool
    data_ok: bool = True
    data_problem: str | None = None
    config_problem: str | None = None
    notes: list = field(default_factory=list)

    @property
    def bookmarks_path(self):
        return os.path.join(self.data, "bookmarks.json")

    @property
    def ai_chat_path(self):
        return os.path.join(self.data, "ai_chat.json")

    @property
    def lock_path(self):
        return os.path.join(self.local, "instance.lock")


def _writable_dir(path):
    """目录存在且能在里面建文件。云盘目录挂了 / 只读，这一步就能发现。"""
    if not os.path.isdir(path):
        return False
    probe = os.path.join(path, f".write-probe-{os.getpid()}")
    try:
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def resolve_layout(portable=False):
    local = local_dir(portable)
    cfg, cfg_problem = load_config(portable)

    data = local
    from_config = False
    data_ok = True
    data_problem = None
    custom = cfg.get("data_dir")
    if isinstance(custom, str) and custom.strip():
        from_config = True
        data = os.path.abspath(os.path.expandvars(os.path.expanduser(custom.strip())))
        if not _writable_dir(data):
            data_ok = False
            data_problem = (f"config.json 指定的数据目录不可用：{data}\n"
                            "（不存在、没登录云盘、或不可写）。本次以只读方式启动，不会在别处新建数据。")

    export = cfg.get("export_dir")
    if isinstance(export, str) and export.strip():
        export = os.path.abspath(os.path.expandvars(os.path.expanduser(export.strip())))
    elif portable:
        export = os.path.join(app_root(), "Audio")
    else:
        docs = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Documents")
        export = os.path.join(docs, APP_NAME, "Audio")

    layout = Layout(
        portable=portable, local=local, data=data,
        cache=os.path.join(local, "temp_audio"), export=export,
        data_dir_from_config=from_config, data_ok=data_ok,
        data_problem=data_problem, config_problem=cfg_problem,
    )
    # 本机目录和缓存目录一定建；数据目录只在它就是本机目录时建（自定义的不替使用者建）
    for d in (layout.local, layout.cache):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            layout.notes.append(f"无法创建目录 {d}：{e}")
    return layout


# ---------- 单实例锁 ----------

class InstanceLock:
    """第二个实例启动时 acquire() 返回 False。锁文件只在本机目录，永远不进同步目录。
    进程退出（哪怕崩溃）Windows 会自动释放 msvcrt 的锁，不会留下死锁。"""

    def __init__(self, path):
        self.path = path
        self._fh = None

    def acquire(self):
        try:
            import msvcrt
            self._fh = open(self.path, "a+")
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except ImportError:
            return True   # 非 Windows（只在测试环境会遇到）
        except OSError:
            if self._fh:
                self._fh.close()
                self._fh = None
            return False

    def release(self):
        if self._fh:
            try:
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            self._fh.close()
            self._fh = None


# ---------- 旧数据迁移 ----------

MIGRATION_MARKER = "migration_done.json"


def legacy_sources(extra=()):
    """旧版本可能把 bookmarks.json 写在哪：程序目录、程序目录下 dist\\、程序目录的上一级、CWD。
    打包后 app_root 就是 dist\\，那么「上一级」就是仓库根目录 —— 使用者机器上两份都在这两处。"""
    root = app_root()
    candidates = [
        ("app", os.path.join(root, "bookmarks.json")),
        ("dist", os.path.join(root, "dist", "bookmarks.json")),
        ("parent", os.path.join(os.path.dirname(root), "bookmarks.json")),
        ("cwd", os.path.join(os.getcwd(), "bookmarks.json")),
    ]
    candidates += list(extra)
    seen = set()
    out = []
    for label, p in candidates:
        rp = os.path.normcase(os.path.realpath(p))
        if rp in seen or not os.path.isfile(p):
            continue
        seen.add(rp)
        out.append((label, p))
    return out


def _same_content(a, b):
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def migrate_legacy(layout, sources=None):
    """把旧位置的 bookmarks.json 搬进数据目录。**只复制，不删原文件。**

    规则（A.1）：
    - 目标已有 bookmarks.json → 不覆盖；每个内容不同的来源复制为 migrated-<label>-<时间>.json
    - 目标没有 → 修改时间最新的来源成为 bookmarks.json，其余同上保留
    - 全部复制并核对成功后写 migration_done.json；下次启动看到标记就跳过
    - 中断后再启动：目标已有的不动，缺的补齐，幂等
    返回给 UI 显示的说明列表（空 = 什么都没发生）。
    """
    if not layout.data_ok:
        return ["数据目录不可用，本次不迁移旧数据"]
    marker = os.path.join(layout.data, MIGRATION_MARKER)
    if os.path.exists(marker):
        return []
    if sources is None:
        sources = legacy_sources()
    target = layout.bookmarks_path
    # 来源里排除目标本身（portable 模式下 app 目录就是数据目录）
    sources = [(l, p) for l, p in sources
               if os.path.normcase(os.path.realpath(p)) != os.path.normcase(os.path.realpath(target))]
    if not sources:
        _write_marker(marker, [])
        return []

    notes = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    copied = []
    try:
        os.makedirs(layout.data, exist_ok=True)
        sources_by_mtime = sorted(sources, key=lambda lp: os.path.getmtime(lp[1]), reverse=True)
        if not os.path.exists(target):
            label, newest = sources_by_mtime[0]
            _copy_verified(newest, target)
            copied.append((label, newest, target))
            notes.append(f"已把 {newest}（{label}，最新）作为数据文件")
            rest = sources_by_mtime[1:]
        else:
            rest = sources_by_mtime
        for label, p in rest:
            if _same_content(p, target):
                continue
            dest = os.path.join(layout.data, f"migrated-{label}-{stamp}.json")
            _copy_verified(p, dest)
            copied.append((label, p, dest))
            notes.append(f"另一份旧数据 {p} 内容不同，保留为 {os.path.basename(dest)}")
        _write_marker(marker, [{"label": l, "from": s, "to": d} for l, s, d in copied])
    except OSError as e:
        notes.append(f"迁移未完成（{e}），原文件都没动，下次启动会继续")
    return notes


def _copy_verified(src, dst):
    tmp = dst + ".migrating"
    shutil.copyfile(src, tmp)
    if not _same_content(src, tmp):
        os.remove(tmp)
        raise OSError(f"复制后内容不一致：{src}")
    os.replace(tmp, dst)


def _write_marker(marker, copied):
    with open(marker, "w", encoding="utf-8") as f:
        json.dump({"done_at": time.strftime("%Y-%m-%d %H:%M:%S"), "copied": copied}, f,
                  ensure_ascii=False, indent=2)


def migrate_cache_background(src_dir, dst_dir):
    """旧缓存 temp_audio\\ 后台搬过来，能省重新下载；失败无所谓。不阻塞启动。"""
    import threading

    def _job():
        try:
            if not os.path.isdir(src_dir) or os.path.normcase(os.path.realpath(src_dir)) == os.path.normcase(os.path.realpath(dst_dir)):
                return
            os.makedirs(dst_dir, exist_ok=True)
            for name in os.listdir(src_dir):
                if not name.endswith(".mp3"):
                    continue
                dst = os.path.join(dst_dir, name)
                if os.path.exists(dst):
                    continue
                try:
                    shutil.copyfile(os.path.join(src_dir, name), dst + ".part")
                    os.replace(dst + ".part", dst)
                except OSError:
                    pass
        except Exception:
            pass

    threading.Thread(target=_job, daemon=True).start()


def open_in_explorer(path):
    try:
        os.makedirs(path, exist_ok=True)
        os.startfile(path)
    except Exception:
        pass


def import_data_file(layout, src):
    """「文件 → 导入旧数据文件…」：使用者手动指定一份旧 bookmarks.json（比如旧目录改名后自动探测不到了）。
    当前主文件先保留为 migrated-current-<时间>.json，再把 src 复制成主文件。只复制，不删 src。
    返回说明；失败抛 OSError 由 UI 显示。"""
    if not layout.data_ok:
        raise OSError("数据目录不可用")
    os.makedirs(layout.data, exist_ok=True)
    target = layout.bookmarks_path
    stamp = time.strftime("%Y%m%d-%H%M%S")
    notes = []
    if os.path.exists(target):
        if _same_content(src, target):
            return ["选的文件和当前数据完全相同，什么都没做"]
        keep = os.path.join(layout.data, f"migrated-current-{stamp}.json")
        _copy_verified(target, keep)
        notes.append(f"当前数据已保留为 {os.path.basename(keep)}")
    _copy_verified(src, target)
    notes.append(f"已把 {src} 作为数据文件，重启后生效")
    return notes
