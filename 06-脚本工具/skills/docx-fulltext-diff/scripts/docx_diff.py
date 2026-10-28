# -*- coding: utf-8 -*-
"""两份 docx 全文严格对照：元数据 + 去TOC正文 + 句子级 diff。

用法（优先）：在 scripts/ 下建 targets.txt（UTF-8，无 BOM），三行：
    <A 文件绝对路径>
    <B 文件绝对路径>
    <输出目录，可省略>
然后：
    <managed python> docx_diff.py

也支持命令行：docx_diff.py <A> <B> [outdir]（路径含中文时用 targets.txt 更稳）。
"""
import zipfile, re, html, os, io, sys, difflib, traceback
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))


def read_targets():
    a = b = out = None
    tf = os.path.join(HERE, 'targets.txt')
    if os.path.isfile(tf):
        lines = [l.strip().strip('"') for l in open(tf, encoding='utf-8').read().splitlines()]
        lines = [l for l in lines if l]
        if len(lines) >= 2:
            a, b = lines[0], lines[1]
            if len(lines) >= 3:
                out = lines[2]
    if len(sys.argv) >= 3:
        a, b = sys.argv[1], sys.argv[2]
        if len(sys.argv) >= 4:
            out = sys.argv[3]
    return a, b, out


A, B, OUT = read_targets()
if not A or not B:
    sys.exit('缺少目标：请在脚本目录建 targets.txt（两行路径），或命令行传 <A> <B> [outdir]')
OUT = OUT or os.path.join(HERE, 'out')
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(OUT, 'err.txt')
if os.path.exists(LOG):
    os.remove(LOG)

# ---------------------------------------------------------------- 提取

T_RE = re.compile(r'<w:t(?:\s[^>]*)?>(.*?)</w:t>', re.S)   # 注意：不能写 <w:t[^>]*>（会误吃 <w:tab>）


def paras(path):
    """返回 [(style, text, img_count)]，已去空段。"""
    z = zipfile.ZipFile(path)
    xml = z.read('word/document.xml').decode('utf-8', 'ignore')
    out = []
    for para in re.split(r'</w:p>', xml):
        t = html.unescape(''.join(T_RE.findall(para)))
        t = re.sub(r'\s+', ' ', t).strip()
        if not t:
            continue
        m = re.search(r'<w:pStyle[^>]*w:val="([^"]+)"', para)
        style = m.group(1) if m else ''
        imgs = len(re.findall(r'<a:blip|<w:drawing|<w:pict', para))
        out.append((style, t, imgs))
    return out


def meta(tag, path):
    b = io.StringIO()
    b.write('########## %s ##########\n文件: %s\n' % (tag, path))
    st = os.stat(path)
    b.write('大小: %.1f KB (%d 字节)\n' % (st.st_size / 1024.0, st.st_size))
    z = zipfile.ZipFile(path)
    names = z.namelist()
    b.write('ZIP 部件数: %d\n' % len(names))
    for part in ['word/document.xml', 'docProps/core.xml', 'docProps/app.xml']:
        if part in names:
            b.write('  %s = %.1f KB\n' % (part, len(z.read(part)) / 1024.0))
    imgs = [n for n in names if n.startswith('word/media/')]
    b.write('内嵌媒体: %d 个 / %.1f KB\n' % (len(imgs), sum(z.getinfo(n).file_size for n in imgs) / 1024.0))
    b.write('媒体类型: %s\n' % dict(Counter(os.path.splitext(n)[1].lower() for n in imgs)))
    for k in ['header', 'footer', 'footnotes', 'endnotes', 'comments']:
        b.write('%s 部件: %d\n' % (k, len([n for n in names if re.match(r'word/%s\d*\.xml' % k, n)])))
    if 'docProps/core.xml' in names:
        c = z.read('docProps/core.xml').decode('utf-8', 'ignore')
        for t in ['dc:creator', 'cp:lastModifiedBy', 'dcterms:created', 'dcterms:modified', 'cp:revision']:
            m = re.search(r'<%s[^>]*>(.*?)</%s>' % (t, t), c, re.S)
            b.write('core.%-22s = %s\n' % (t, m.group(1) if m else '(无)'))
    if 'docProps/app.xml' in names:
        c = z.read('docProps/app.xml').decode('utf-8', 'ignore')
        for t in ['Pages', 'Words', 'Characters', 'Paragraphs', 'Application', 'TotalTime']:
            m = re.search(r'<%s>(.*?)</%s>' % (t, t), c, re.S)
            b.write('app.%-22s = %s\n' % (t, m.group(1) if m else '(无)'))
    if 'word/document.xml' in names:
        xml = z.read('word/document.xml').decode('utf-8', 'ignore')
        b.write('w:tbl 表格数: %d\nw:drawing 图片引用: %d\nw:p 段落总数(含空): %d\n'
                % (len(re.findall(r'<w:tbl>', xml)), len(re.findall(r'<w:drawing', xml)),
                   len(re.findall(r'<w:p[ >]', xml))))
        for k in ['w:ins', 'w:del']:
            b.write('%s 数: %d\n' % (k, len(re.findall('<' + k + r'[ >]', xml))))
    rows = paras(path)
    cnt, samp = Counter(), {}
    for s, t, im in rows:
        cnt[s] += 1
        samp.setdefault(s, [])
        if len(samp[s]) < 3:
            samp[s].append(t[:44])
    b.write('样式分布(样式ID: 段数 | 示例)  ← TOC 样式会与正文标题样式段数一一对应:\n')
    for s, n in sorted(cnt.items(), key=lambda x: -x[1]):
        b.write('  %-10s %4d | %s\n' % (s or '(none)', n, ' ;; '.join(samp[s])))
    b.write('\n')
    return b.getvalue(), rows


def dump(rows, path):
    lines = []
    for i, (s, t, im) in enumerate(rows, 1):
        tag = ('[S%s]' % s) if s else ''
        img = (' {IMGx%d}' % im) if im else ''
        lines.append('%04d %s%s %s' % (i, tag, img, t))
    open(path, 'w', encoding='utf-8').write('\n'.join(lines))
    return lines


def guess_toc_styles(rows):
    """启发式：结尾带页码的段（- 1 - / 12）且样式集中的，判为 TOC 样式。"""
    cand = {}
    for s, t, im in rows:
        if re.search(r'(?:-\s*\d+\s*-|\d+)$', t) and re.search(r'^\d+(\.\d+)*\.?\s', t):
            cand[s] = cand.get(s, 0) + 1
    return set(k for k, v in cand.items() if v >= 3)


try:
    m1, ra = meta('A = %s' % os.path.basename(A), A)
    m2, rb = meta('B = %s' % os.path.basename(B), B)
    open(os.path.join(OUT, 'meta.txt'), 'w', encoding='utf-8').write(m1 + m2)

    toc_a, toc_b = guess_toc_styles(ra), guess_toc_styles(rb)
    with open(os.path.join(OUT, 'toc_styles.txt'), 'w', encoding='utf-8') as f:
        f.write('A 猜测 TOC 样式: %s\nB 猜测 TOC 样式: %s\n' % (sorted(toc_a), sorted(toc_b)))

    ba = [r for r in ra if r[0] not in toc_a]
    bb = [r for r in rb if r[0] not in toc_b]
    ta = [r[1] for r in ba]
    tb = [r[1] for r in bb]
    dump(ba, os.path.join(OUT, 'A_body.txt'))
    dump(bb, os.path.join(OUT, 'B_body.txt'))
    dump(ra, os.path.join(OUT, 'A_with_toc.txt'))
    dump(rb, os.path.join(OUT, 'B_with_toc.txt'))

    def sents(rows):
        out = []
        for i, (s, t, im) in enumerate(rows, 1):
            for p in re.split(r'(?<=[。！？；])', t):
                p = p.strip()
                if len(p) >= 8:
                    out.append((i, p))
        return out

    pa, pb = sents(ba), sents(bb)
    la = [x[1] for x in pa]
    lb = [x[1] for x in pb]
    sm = difflib.SequenceMatcher(None, la, lb, autojunk=False)
    b = io.StringIO()
    b.write('A 正文段落=%d 字符=%d 句子=%d\n' % (len(ba), sum(len(x) for x in ta), len(la)))
    b.write('B 正文段落=%d 字符=%d 句子=%d\n' % (len(bb), sum(len(x) for x in tb), len(lb)))
    b.write('句子相似度=%.4f\n\n' % sm.ratio())
    nA = nB = nC = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        if tag == 'delete':
            for k in range(i1, i2):
                nA += 1
                b.write('[仅A A段%d] %s\n' % (pa[k][0], la[k]))
        elif tag == 'insert':
            for k in range(j1, j2):
                nB += 1
                b.write('[仅B B段%d] %s\n' % (pb[k][0], lb[k]))
        elif (i2 - i1) == 1 and (j2 - j1) == 1:
            nC += 1
            b.write('[改写 A段%d->B段%d]\n   A> %s\n   B> %s\n' % (pa[i1][0], pb[j1][0], la[i1], lb[j1]))
        else:
            for k in range(i1, i2):
                nA += 1
                b.write('[仅A A段%d] %s\n' % (pa[k][0], la[k]))
            for k in range(j1, j2):
                nB += 1
                b.write('[仅B B段%d] %s\n' % (pb[k][0], lb[k]))
    b.write('\n=== 仅A %d / 仅B %d / 一对一改写 %d ===\n' % (nA, nB, nC))
    open(os.path.join(OUT, 'sentdiff.txt'), 'w', encoding='utf-8').write(b.getvalue())
    open(os.path.join(OUT, 'done.txt'), 'w', encoding='utf-8').write(
        'OK A(全)=%d A(正文)=%d B(全)=%d B(正文)=%d 仅A=%d 仅B=%d 改写=%d\n'
        % (len(ra), len(ba), len(rb), len(bb), nA, nB, nC))
except Exception:
    open(LOG, 'w', encoding='utf-8').write(traceback.format_exc())
