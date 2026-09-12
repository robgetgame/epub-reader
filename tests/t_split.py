"""第 6 步验收：断句（B6）。闭合引号归前句、连续标点一组、小数点不切、省略号不切、纯标点不念、偏移一致。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from epub_parser import EpubParser, sentence_index_at

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


def split(t):
    ss = EpubParser.split_into_sentences(t)
    check(all(t[s.start:s.end] == s.text for s in ss), f"偏移一致：{t[:20]!r}")
    return ss


def texts(t):
    return [s.text for s in split(t)]


print("1. 闭合引号归前句")
check(texts('他笑道：“你来了。”她点头。') == ['他笑道：“你来了。”', '她点头。'], "中文引号")
check(texts('“嗯。”第二句！') == ['“嗯。”', '第二句！'], "开头引号句")
check(texts('（括号里。）外面。') == ['（括号里。）', '外面。'], "全角括号")
check(texts('He said "Go." She left.') == ['He said "Go."', 'She left.'], "英文引号")
check(texts('「日文引号。」下一句。') == ['「日文引号。」', '下一句。'], "日文引号")

print("2. 连续标点一组")
check(texts('什么！！！真的?!好') == ['什么！！！', '真的?!', '好'], "！！！ 和 ?!")
check(texts('等等。。。然后') == ['等等。。。', '然后'], "。。。")

print("3. 小数点 / 网址不切")
check(texts('圆周率是3.14。下一句。') == ['圆周率是3.14。', '下一句。'], "3.14")
check(texts('www.example.com 是网址。好。') == ['www.example.com 是网址。', '好。'], "网址")
check(texts('Version 2.0.1 released. Next.') == ['Version 2.0.1 released.', 'Next.'], "版本号")
check(texts('Mr. Smith went home.') == ['Mr.', 'Smith went home.'], "Mr. 仍切（已接受）")

print("4. 省略号")
check(texts('他……走了。然后呢') == ['他……走了。', '然后呢'], "…… 不切")
ss = split('第一段\n……\n第三段')
check([s.text for s in ss] == ['第一段', '……', '第三段'], "单独一行的省略号成句")
check([s.spoken for s in ss] == [True, False, True], "纯标点句 spoken=False")

print("5. 换行 / 空白 / 结尾")
check(texts('第一段\n第二段\r\n第三段') == ['第一段', '第二段', '第三段'], "换行切、\\r 不进句子")
check(texts('  前后空白。  ') == ['前后空白。'], "去空白")
check(texts('结尾没标点') == ['结尾没标点'], "无标点尾巴")
check(texts('') == [] and texts('\n\n  \n') == [], "空文本")
check(texts('嗯。') == ['嗯。'] and split('嗯。')[0].spoken, "短的真实语句要念")

print("6. sentence_index_at")
ss = split('一。二。三。')
check(sentence_index_at(ss, 0) == 0 and sentence_index_at(ss, 2) == 1 and sentence_index_at(ss, 99) == 2, "偏移定位")
check(sentence_index_at([{"start": 0}, {"start": 5}], 7) == 1, "dict 也行")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}")
    sys.exit(1)
print("ALL OK")
