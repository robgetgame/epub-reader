"""EPUB 解析：章节列表、章节正文、断句。

第 3 步（2026-09-12）重写的三件事：
1. **章 = 目录（nav / NCX）的一个条目**，不是一个 spine 文件（A.7，本人拍板）。
   一个条目覆盖它指向的文件到下一条目指向的文件之间的所有 spine 文档。
   没有目录的书退回「一文件一章」。
2. **开书不再逐章跑 BeautifulSoup 取标题**（B9）。标题来自目录标签，正文按需解析。
   雪中悍刀行 987 个文件：read_epub 0.14 s，原来的逐章解析是开书慢的全部原因。
3. **句子带原文偏移** `Sentence(text, start, end)`。UI 用偏移在原文上打高亮标签，
   不再把正文重排成「一句一段」—— 笔记区尤其不能动使用者的文本（A.2）。
"""

import re
import warnings
from dataclasses import dataclass, field

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

warnings.filterwarnings('ignore', category=UserWarning, module='ebooklib')
warnings.filterwarnings('ignore', category=FutureWarning, module='ebooklib')

# 句末（B6，第 6 步）：
# - 中文句号问号叹号、英文 ?!：连续的算一组（「！！！」「?!」不拆）
# - 英文句点：只有后面跟空白 / 闭合引号 / 文本末尾才算句末 —— 「3.14」「www.a.com」不切；
#   「Mr. Smith」仍会切，中文书为主，接受
# - 换行永远是句末（段落边界）
# - 省略号「……」不算句末：「他……走了。」是一句
# 句末之后紧跟的闭合引号 / 括号归前一句：「他说。”她点头。」→ 「他说。”」「她点头。」
_TERM = re.compile(r'''[。？！?!]+|\.+(?=[\s”’」』）》〉】\]\)"'…]|$)|\n''')
_CLOSERS = re.compile(r'''[”’」』）》〉】\]\)"']*''')
# 一句里至少要有一个字母 / 数字 / 汉字 / 假名才值得念；纯标点（比如单独一行的「……」）只高亮不念
_HAS_WORD = re.compile(r'[\w぀-ヿ㐀-鿿豈-﫿]')


@dataclass
class Sentence:
    text: str      # 去掉首尾空白后的句子
    start: int     # 在整章正文里的偏移（首尾空白已去掉后的精确位置）
    end: int
    spoken: bool = True   # False = 纯标点碎片，高亮但不送 TTS（edge-tts 对「”」这种会报无音频）


@dataclass
class Chapter:
    index: int
    title: str
    item_ids: list = field(default_factory=list)   # 组成这一章的 spine 文档 id，按阅读顺序


class EpubParser:
    def __init__(self):
        self.book = None
        self.chapters: list[Chapter] = []
        self.path = None

    # ---------- 打开 ----------

    def load_epub(self, file_path):
        """成功 True。失败 False 且 self.book 保持为 None —— 调用方应先建候选 parser，
        成功后再替换旧的（A.1：加载失败不留下新旧混合状态）。"""
        try:
            book = epub.read_epub(file_path)
        except Exception as e:
            print(f"Error loading EPUB {file_path}: {e}")
            return False
        self.book = book
        self.path = file_path
        self.chapters = self._build_chapters()
        return True

    def _spine_docs(self):
        """spine 里按顺序的 (item_id, item)，只要 XHTML 文档。"""
        out = []
        for item_id, _linear in self.book.spine:
            item = self.book.get_item_with_id(item_id)
            if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue
            # EPUB3 的 nav.xhtml 在 spine 里也是 ITEM_DOCUMENT，读出来是一堆目录链接，不算正文。
            # ebooklib 读回来把它做成 EpubNav（is_chapter() False），manifest 里带 properties="nav"
            if "nav" in (getattr(item, "properties", None) or []) or isinstance(item, epub.EpubNav):
                continue
            out.append((item_id, item))
        return out

    def _flat_toc(self):
        """ebooklib 的 toc 是 Link 和 (Section, [children]) 混合的嵌套列表，拍平成 (title, href)。"""
        result = []

        def walk(entries):
            for e in entries:
                if isinstance(e, tuple):
                    section, children = e
                    href = getattr(section, "href", None)
                    title = getattr(section, "title", None)
                    if href:
                        result.append((title or "", href))
                    walk(children)
                else:
                    href = getattr(e, "href", None)
                    if href:
                        result.append((getattr(e, "title", "") or "", href))

        walk(self.book.toc or [])
        return result

    def _build_chapters(self):
        docs = self._spine_docs()
        if not docs:
            return []

        # href（去掉 #锚点）→ spine 位置
        pos_by_name = {}
        for pos, (item_id, item) in enumerate(docs):
            name = item.get_name()
            pos_by_name[name] = pos
            # 有的 toc href 带目录前缀、有的不带；两种都登记
            pos_by_name[name.split("/")[-1]] = pos

        # 目录条目 → (spine 位置, 标题)。同一文件多个条目：优先没有锚点的（那是整章的标题，
        # 带锚点的通常是「第一卷」这种指向文内标题的分卷条目）。
        entries = []
        for title, href in self._flat_toc():
            file_part, _, frag = href.partition("#")
            pos = pos_by_name.get(file_part)
            if pos is None:
                pos = pos_by_name.get(file_part.split("/")[-1])
            if pos is None:
                continue
            entries.append((pos, bool(frag), title.strip()))

        if not entries:
            return self._chapters_from_spine(docs)

        # 每个 spine 位置选一个标题
        title_at = {}
        for pos, has_frag, title in entries:
            if pos not in title_at or (title_at[pos][0] and not has_frag):
                title_at[pos] = (has_frag, title)
        starts = sorted(title_at)

        chapters = []
        # 目录第一个条目之前的文件（封面、版权页……）归成一章；没正文的话读的时候也是空的
        if starts[0] > 0:
            chapters.append(Chapter(index=0, title="（正文前）",
                                    item_ids=[docs[i][0] for i in range(0, starts[0])]))
        for n, start in enumerate(starts):
            end = starts[n + 1] if n + 1 < len(starts) else len(docs)
            title = title_at[start][1] or f"第 {len(chapters) + 1} 章"
            chapters.append(Chapter(index=len(chapters), title=title,
                                    item_ids=[docs[i][0] for i in range(start, end)]))
        return chapters

    def _chapters_from_spine(self, docs):
        """没有目录：一文件一章，标题从文档里的 h1-h3 取（只在这种书上才逐章解析）。"""
        chapters = []
        for item_id, item in docs:
            soup = BeautifulSoup(item.get_content(), 'html.parser')
            tag = soup.find(['h1', 'h2', 'h3'])
            title = tag.get_text().strip()[:40] if tag else ""
            chapters.append(Chapter(index=len(chapters), title=title or f"第 {len(chapters) + 1} 章",
                                    item_ids=[item_id]))
        return chapters

    # ---------- 读 ----------

    def get_chapter_list(self):
        return self.chapters

    @staticmethod
    def _doc_text(item):
        soup = BeautifulSoup(item.get_content(), 'html.parser')
        for unwanted in soup.find_all(['img', 'table', 'figure', 'svg', 'script', 'style', 'head', 'math']):
            unwanted.decompose()
        text = soup.get_text(separator='\n')
        text = text.replace('\r', '')
        text = re.sub(r'[ \t]+\n', '\n', text)
        text = re.sub(r'\n\s*\n', '\n', text)
        return text.strip()

    def get_chapter_text(self, chapter_index):
        """整章纯文本，段落之间一个换行。越界或没开书返回空串。"""
        if not self.book or chapter_index < 0 or chapter_index >= len(self.chapters):
            return ""
        parts = []
        for item_id in self.chapters[chapter_index].item_ids:
            item = self.book.get_item_with_id(item_id)
            if item is None:
                continue
            t = self._doc_text(item)
            if t:
                parts.append(t)
        return "\n".join(parts)

    def get_chapter_sentences(self, chapter_index):
        """(正文, [Sentence])。"""
        text = self.get_chapter_text(chapter_index)
        return text, self.split_into_sentences(text)

    # ---------- 断句 ----------

    @staticmethod
    def split_into_sentences(text):
        """按句末切，返回带偏移的 Sentence 列表。空片段丢掉。
        偏移是相对传入 text 的，不做任何替换 —— 调用方要拿偏移回原文打标签。"""
        sentences = []
        pos = 0
        n = len(text)
        for m in _TERM.finditer(text):
            if m.start() < pos:
                continue          # 闭合引号扩展已经把这个位置吃掉了
            end = m.end()
            if m.group(0) != chr(10):   # 换行本身不带闭合引号扩展
                end = _CLOSERS.match(text, end).end()
            s = _strip_span(text, pos, end)
            if s:
                sentences.append(s)
            pos = end
        if pos < n:
            s = _strip_span(text, pos, n)
            if s:
                sentences.append(s)
        return sentences


def _strip_span(text, start, end):
    """text[start:end] 去掉首尾空白，返回 Sentence 或 None。"""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return None
    t = text[start:end]
    return Sentence(text=t, start=start, end=end, spoken=bool(_HAS_WORD.search(t)))


def sentence_index_at(sentences, offset):
    """偏移落在哪一句：最后一个 start <= offset 的句子；没有就 0。书签恢复用。
    sentences 里既可以是 Sentence 也可以是带 "start" 键的 dict（笔记句子）。"""
    idx = 0
    for i, s in enumerate(sentences):
        start = s["start"] if isinstance(s, dict) else s.start
        if start <= offset:
            idx = i
        else:
            break
    return idx
