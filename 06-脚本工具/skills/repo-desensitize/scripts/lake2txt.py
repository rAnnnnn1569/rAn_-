#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""思源 .lake (HTML 结构) -> 带结构的纯文本。纯标准库。
用法: python lake2txt.py <in.lake> <out.txt>
"""
import sys, re, html

def strip_tags(s):
    s = re.sub(r'<br\s*/?>', '\n', s)
    s = re.sub(r'<[^>]+>', '', s)
    return html.unescape(s)

def main():
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, 'r', encoding='utf-8', errors='replace') as f:
        t = f.read()

    # 丢弃 head/meta/title
    t = re.sub(r'<!doctype[^>]*>', '', t, flags=re.I)
    t = re.sub(r'<title>.*?</title>', '', t, flags=re.I | re.S)
    t = re.sub(r'<meta[^>]*>', '', t, flags=re.I)

    # 内联卡片(图片/附件) -> 占位
    t = re.sub(r'<card\b[^>]*type="inline"[^>]*>.*?</card>', ' [图片] ', t, flags=re.I | re.S)
    t = re.sub(r'<card\b[^>]*>.*?</card>', ' [卡片] ', t, flags=re.I | re.S)

    out = []
    # 块级标签逐个规范化
    t = re.sub(r'<h([1-6])[^>]*>', lambda m: '\n' + '#' * int(m.group(1)) + ' ', t, flags=re.I)
    t = re.sub(r'</h[1-6]>', '\n', t, flags=re.I)
    t = re.sub(r'<li[^>]*>', '\n- ', t, flags=re.I)
    t = re.sub(r'</li>', '', t, flags=re.I)
    t = re.sub(r'<(ul|ol)[^>]*>', '\n', t, flags=re.I)
    t = re.sub(r'<(p|div|blockquote|pre|tr)[^>]*>', '\n', t, flags=re.I)
    t = re.sub(r'</(p|div|blockquote|pre|tr|ul|ol|table)>', '\n', t, flags=re.I)
    t = re.sub(r'</t[dh]>', ' | ', t, flags=re.I)
    t = re.sub(r'<table[^>]*>', '\n', t, flags=re.I)
    t = re.sub(r'<code[^>]*>', '`', t, flags=re.I)
    t = re.sub(r'</code>', '`', t, flags=re.I)

    # 剩余标签剥离
    t = re.sub(r'<[^>]+>', '', t)
    t = html.unescape(t)

    lines = []
    for ln in t.split('\n'):
        s = re.sub(r'[ \t\xa0]+', ' ', ln).strip()
        lines.append(s)
    # 压缩连续空行
    res = []
    blank = 0
    for s in lines:
        if s == '':
            blank += 1
            if blank <= 1:
                res.append('')
        else:
            blank = 0
            res.append(s)

    with open(dst, 'w', encoding='utf-8') as f:
        f.write('\n'.join(res))
    print('OK', dst, len(res), 'lines')

if __name__ == '__main__':
    main()
