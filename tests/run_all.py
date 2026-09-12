"""跑 tests/ 下所有 t_*.py，任一非零退出即整体失败。

不用 pytest：这个仓库的验证方式是一次性脚本 —— 伪造 Tk / COM 对象，
asyncio.run 跑一遍，断言不过就 sys.exit(1)。不连网、不碰真实数据文件。
"""

import os
import subprocess
import sys

# Windows 控制台默认 cp1252，中文 print 会炸；在这里统一处理，子进程靠 PYTHONIOENCODING
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main() -> int:
    scripts = sorted(f for f in os.listdir(HERE) if f.startswith("t_") and f.endswith(".py"))
    if not scripts:
        print("tests/ 下还没有 t_*.py")
        return 0
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=ROOT)
    failed = []
    for name in scripts:
        print(f"== {name}")
        r = subprocess.run([sys.executable, os.path.join(HERE, name)], env=env, cwd=ROOT)
        if r.returncode != 0:
            failed.append(name)
    print()
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print(f"OK ({len(scripts)} scripts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
