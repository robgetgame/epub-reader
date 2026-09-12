# EPUB Reader

Windows 桌面程序：打开 `.epub`，按章朗读（本机 SAPI 或微软在线神经音色），句子级高亮，
每章一份可编辑、可朗读的笔记，AI 生成章节讲解并可追问。单人使用。

## 运行

- 直接跑 `dist\EpubReader.exe`（用 `build.ps1` 打包），或者源码：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe main.py
```

- 数据在 `%LOCALAPPDATA%\EpubReader\`（`bookmarks.json` 书签和笔记、`ai_chat.json` 对话、
  `temp_audio\` 音频缓存、`config.json` 可选配置）。顶栏「数据目录」直达。换新 exe 直接覆盖，数据不动。
- 导出的 mp3 在 `文档\EpubReader\Audio\`。
- 第一次从旧版升级：旧的 `bookmarks.json` 用「旧笔记 → 导入旧数据文件…」选进来；旧笔记和旧书签原样保留在
  「旧笔记」窗口里，可以复制到当前章。书签要手动重新定位一次。

## AI 讲解

- 走 OpenRouter。key 放 Windows 用户环境变量 `OPENROUTER_KEY`（设完重启程序）：

```powershell
[Environment]::SetEnvironmentVariable("OPENROUTER_KEY", "你的key", "User")
```

- 默认模型 `deepseek/deepseek-v4.1-flash`。`config.json` 可改：

```json
{
  "model": "deepseek/deepseek-v4.1-flash",
  "history_turns": 10,
  "data_dir": "C:\Users\你\Google Drive\EpubReader",
  "export_dir": "D:\Audio"
}
```

- `data_dir` 指到云盘目录可多机同步（Google Drive 用「镜像文件」模式，不要「流式传输」）。
  缓存和实例锁永远留本机。目录不可用时程序只读启动并提示，不会在别处新建数据。
- 每本书第一次打开 AI 窗口会自动判定是小说还是非虚构（右栏「这本书的类型」可改），
  讲解稿按类型用不同结构，长度约本章的 1/10，语言跟书走。费用用 OpenRouter 返回的实际值累计。

## 笔记里的多音色标签

手写笔记可以用 `{yunxi}…{/yunxi}`、`{xiaoxiao}`、`{yunyang}`、`{guy}`、`{aria}`、`{jenny}` 切换在线音色。
AI 生成的讲解稿是单人叙述，不带标签。

## 开发

- 没有测试框架：`python tests\run_all.py` 跑 `tests\t_*.py`，任一失败退出非零。全部不联网、不碰真实数据。
- 设计文档和两轮审计在 `docs\`；原始规格和修订附录在 `AGENTS.md`。
- 打包：`.\build.ps1`（先跑测试，测试不过不打包）。
