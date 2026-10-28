# -*- coding: utf-8 -*-
"""docx 全通道关键词检索（文本层 + 图片 OCR）

用法：
  python docx_scan.py text   <docx> <kw> [kw...]            # 全文本通道（含嵌套对象/属性/域代码）
  python docx_scan.py images <docx> <kw> [kw...] [--procs N] [--cache FILE] [--max N] [--workdir DIR]
  python docx_scan.py report <cache.jsonl> [kw...]          # 只跑检索，不重新 OCR

设计要点（都是踩过的坑）：
  * OCR 引擎 rapidocr-onnxruntime 需装在独立目录，用 PYTHONPATH 注入，别污染主环境。
  * 图片宽高比 >= 4 或「面积大但识别行数极少」的图，OCR 会整张漏检 → 必须切块+放大补测。
  * 进程数按机器来：本机 i3-6100（2 物理核/4 线程）用 2 进程 + 低于正常优先级，给用户留核。
  * 缓存 jsonl 可断点续跑，中断后重跑只处理未完成的图。
"""
import os
import re
import sys
import io
import json
import math
import time
import zipfile
import hashlib
import collections
import multiprocessing as mp

# ---- 依赖目录（隔离安装）----
HOME = os.path.expanduser('~')
PYLIBS = os.path.join(HOME, '.workbuddy', 'binaries', 'python', 'envs', 'pylibs')
OCRLIBS = os.path.join(HOME, '.workbuddy', 'binaries', 'python', 'envs', 'ocrlibs')
for _p in (PYLIBS, OCRLIBS):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)


# =====================================================================
# 一、文本通道
# =====================================================================
KEY_CHANNELS = {
    'w:instrText(域代码)': r'<w:instrText[^>]*>(.*?)</w:instrText>',
    'w:delText(修订-删除文字)': r'<w:delText[^>]*>(.*?)</w:delText>',
    'w:insText(修订-插入文字)': r'<w:insText[^>]*>(.*?)</w:insText>',
    'w:fldSimple(简单域)': r'<w:fldSimple[^>]*>',
    'w:txbxContent(文本框)': r'<w:txbxContent[^>]*>',
    'wp:docPr/@descr(替代文字)': r'<wp:docPr[^>]*descr="([^"]*)"',
    'wp:docPr/@name': r'<wp:docPr[^>]*name="([^"]*)"',
    'wp:docPr/@title': r'<wp:docPr[^>]*title="([^"]*)"',
    'w:hyperlink/@w:anchor': r'<w:hyperlink[^>]*w:anchor="([^"]*)"',
    'w:bookmarkStart/@name': r'<w:bookmarkStart[^>]*w:name="([^"]*)"',
    'v:shape/@alt': r'<v:shape[^>]*alt="([^"]*)"',
}

# ---- 三级归一化：覆盖"子串匹配"的盲区（字符被拆开 / 夹干扰符 / 全角 / 零宽）----
_ZW = re.compile(r'[\u200b-\u200f\u202a-\u202e\ufeff\u00ad]')


def _to_halfwidth(s):
    """全角 -> 半角（含全角句点 ． 和各种全角标点）"""
    out = []
    for ch in s:
        o = ord(ch)
        if o == 0x3000:
            ch = ' '
        elif 0xFF01 <= o <= 0xFF5E:
            ch = chr(o - 0xFEE0)
        elif o == 0xFF0E:
            ch = '.'
        out.append(ch)
    return ''.join(out)


def _norm(s):
    return _ZW.sub('', _to_halfwidth(s)).lower()


def _nospace(s):
    return re.sub(r'\s+', '', s)


def _alnum_only(s):
    """只留 a-z0-9 —— 最松口径，能抓住任意标点/空白插入的情况"""
    return re.sub(r'[^a-z0-9]', '', s)


def _levels(text, kws):
    """三级检索：L1 原始 / L2 去空白 / L3 仅字母数字。返回 {级别: 命中数}"""
    n1 = _norm(text)
    n2 = _nospace(n1)
    n3 = _alnum_only(n1)
    v2 = [_nospace(_norm(k)) for k in kws]
    v3 = [_alnum_only(_norm(k)) for k in kws]
    return {
        'L1原始': sum(n1.count(k.lower()) for k in kws),
        'L2去空白': sum(n2.count(k) for k in v2),
        'L3仅字母数字': sum(n3.count(k) for k in v3),
    }


def _kw_to_l3(k):
    return _alnum_only(_norm(k))


def scan_text(path, kws):
    """遍历所有部件 + 非文本通道 + 嵌套 ZIP，检索关键词。返回行列表。"""
    out = []
    zf = zipfile.ZipFile(path)
    names = zf.namelist()
    low_kws = [k.lower() for k in kws]

    out.append('zip 条目总数: %d' % len(names))

    # 1) 所有 xml/rels/txt 部件（朴素子串 + 剥标签后三级归一化）
    out.append('')
    out.append('== 1) XML/rels 部件命中（朴素子串 L0 / 剥标签三级 L1-L3）==')
    n_part = 0
    for n in names:
        if n.endswith('/') or not n.lower().endswith(('.xml', '.rels', '.txt')):
            continue
        try:
            s = zf.read(n).decode('utf-8', 'ignore')
        except Exception:
            continue
        low = s.lower()
        hit = {k: low.count(k) for k in low_kws if k in low}
        plain = re.sub(r'<[^>]*>', '', s)      # 剥标签：跨 run / 跨元素被拆开的字符重新拼上
        lv = _levels(plain, kws)
        if hit or any(lv.values()):
            n_part += 1
            out.append('  %-44s 朴素=%s  剥标签 L1=%d L2=%d L3=%d'
                       % (n, hit or '无', lv['L1原始'], lv['L2去空白'], lv['L3仅字母数字']))
    out.append('  命中部件数: %d' % n_part)

    # 1b) 全条目字节级兜底：连不被当"文本"的条目（media/*.png、*.bin、*.dat、嵌入字体…）也搜
    out.append('')
    out.append('== 1b) 全条目字节级兜底（ASCII / UTF-16LE，不限扩展名）==')
    n_byte = 0
    for n in names:
        if n.endswith('/'):
            continue
        try:
            data = zf.read(n)
        except Exception:
            continue
        low = data.lower()
        hits = [k for k in low_kws if k.encode('utf-8') in low]
        try:
            t16 = data.decode('utf-16-le', 'ignore').lower()
            hits += ['utf16:' + k for k in low_kws if k in t16]
        except Exception:
            pass
        if hits:
            n_byte += 1
            out.append('  ★ %-56s %s' % (n, sorted(set(hits))))
    out.append('  字节级命中条目: %d  （扫描 %d 个条目）' % (n_byte, len(names)))

    # 2) document.xml 逐段
    doc = zf.read('word/document.xml').decode('utf-8', 'ignore')
    tok = re.compile(r'<w:p\b[^>]*/>|<w:p\b[^>]*>.*?</w:p>|<w:tbl>.*?</w:tbl>', re.S)
    units = tok.findall(doc)

    def ptext(seg):
        return ''.join(re.findall(r'<w:t(?:\s[^>]*)?>(.*?)</w:t>', seg, re.S))

    def pid(seg):
        m = re.search(r'w14:paraId="([0-9A-Fa-f]{8})"', seg)
        return m.group(1) if m else ''

    out.append('')
    out.append('== 2) 正文逐段命中（朴素 w:t 拼接 / 剥标签三级）==')
    hits = 0

    def _check(i, seg):
        """返回 (是否命中, 展示文本, 级别描述)。剥标签后再做三级归一化，
        以免关键词被拆到多个 w:r/w:t 里（如 'bd.zwb' + 'ds.com'）而漏检。"""
        t_raw = ptext(seg)
        plain = re.sub(r'<[^>]*>', '', seg)
        lv = _levels(plain, kws)
        naive = any(k in t_raw.lower() for k in low_kws)
        if not naive and not any(lv.values()):
            return None
        if any(lv.values()):
            lvs = 'L1=%d L2=%d L3=%d' % (lv['L1原始'], lv['L2去空白'], lv['L3仅字母数字'])
            # 命中位置附近文本
            n3 = _alnum_only(_norm(plain))
            frag = plain[:200]
            for k in kws:
                k3 = _kw_to_l3(k)
                pos = n3.find(k3)
                if pos >= 0:
                    frag = re.sub(r'\s+', ' ', plain[max(0, pos - 60):pos + 120])
                    break
        else:
            lvs = '朴素命中'
            frag = t_raw[:200]
        return (lvs, frag)

    for i, seg in enumerate(units):
        if seg.startswith('<w:tbl'):
            inner = re.findall(r'<w:p\b[^>]*/>|<w:p\b[^>]*>.*?</w:p>', seg, re.S)
            for sub in inner:
                r = _check(i, sub)
                if r:
                    hits += 1
                    out.append('  [%4d|表] %s  %s  %s' % (i, pid(sub), r[0], r[1]))
            continue
        r = _check(i, seg)
        if r:
            hits += 1
            out.append('  [%4d] %s  %s  %s' % (i, pid(seg), r[0], r[1]))
    out.append('  正文命中段数: %d' % hits)

    # 3) 非文本通道
    out.append('')
    out.append('== 3) 非文本通道（域代码/替代文字/锚点等）==')
    for label, pat in KEY_CHANNELS.items():
        v = [x for x in re.findall(pat, doc, re.S) if x.strip()]
        h = [x for x in v if any(k in x.lower() for k in low_kws)]
        out.append('  %-30s 总 %-6d 命中 %s' % (label, len(v), h[:8] if h else '无'))

    # 4) 页眉页脚 / 文档属性
    out.append('')
    out.append('== 4) 页眉页脚 / 文档属性 ==')
    for n in sorted(x for x in names if re.search(r'word/(header|footer)\d*\.xml$', x)):
        s = zf.read(n).decode('utf-8', 'ignore')
        if any(k in s.lower() for k in low_kws):
            out.append('  ★ %s 命中' % n)
    for n in ('docProps/core.xml', 'docProps/app.xml', 'docProps/custom.xml'):
        if n in names and any(k in zf.read(n).decode('utf-8', 'ignore').lower() for k in low_kws):
            out.append('  ★ %s 命中' % n)

    # 5) 嵌套 ZIP / OLE
    out.append('')
    out.append('== 5) 嵌套对象（OLE/OOXML）==')
    n_nest = 0
    for n in names:
        if n.endswith('/') or n.lower().endswith(('.xml', '.rels', '.png', '.jpeg', '.jpg')):
            continue
        data = zf.read(n)
        try:
            z2 = zipfile.ZipFile(io.BytesIO(data))
            for m in z2.namelist():
                if m.endswith('/'):
                    continue
                try:
                    s = z2.read(m).decode('utf-8', 'ignore').lower()
                except Exception:
                    continue
                if any(k in s for k in low_kws):
                    n_nest += 1
                    out.append('  ★ %s → %s 命中' % (n, m))
        except Exception:
            blob = data.lower()
            for k in low_kws:
                if k.encode('utf-8') in blob or k.encode('utf-16-le') in blob:
                    n_nest += 1
                    out.append('  ★ %s（二进制）命中 %s' % (n, k))
    if not n_nest:
        out.append('  无命中')

    # 6) 外部关系
    out.append('')
    out.append('== 6) 外部关系（超链接/远端图片）==')
    rp = 'word/_rels/document.xml.rels'
    if rp in names:
        rels = zf.read(rp).decode('utf-8', 'ignore')
        ext = re.findall(r'Target="([^"]+)"[^>]*TargetMode="External"', rels)
        out.append('  外部关系 %d 个' % len(ext))
        for t in sorted(set(ext)):
            out.append('    %s' % t)
    return out


# =====================================================================
# 二、图片 OCR
# =====================================================================
_G = {}


def _low_priority():
    try:
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
    except Exception:
        pass


def _init_worker(docx_path):
    _low_priority()
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    import numpy as np
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    from rapidocr_onnxruntime import RapidOCR
    _G['np'] = np
    _G['Image'] = Image
    _G['eng'] = RapidOCR()
    _G['zf'] = zipfile.ZipFile(docx_path)


def _ocr_array(im, np, eng, Image):
    res, _ = eng(np.array(im))
    return [{'t': it[1], 's': round(float(it[2]), 3)} for it in (res or [])]


def _work_single(name):
    """整图 OCR；宽高比极端的图自动切块+放大。"""
    np = _G['np']; Image = _G['Image']; eng = _G['eng']; zf = _G['zf']
    try:
        data = zf.read(name)
        im = Image.open(io.BytesIO(data)).convert('RGB')
    except Exception as e:
        return {'name': name, 'err': str(e)[:100], 'lines': []}
    W, H = im.size
    if max(W, H) < 100:
        return {'name': name, 'w': W, 'h': H, 'skip': 'too_small', 'lines': []}
    lines, seen, ntile = [], set(), 0
    ar = max(W, H) / max(1, min(W, H))
    need_tile = ar >= 4.0 or max(W, H) > 1800
    if not need_tile:
        try:
            lines = _ocr_array(im, np, eng, Image)
        except Exception as e:
            return {'name': name, 'w': W, 'h': H, 'err': 'ocr:' + str(e)[:100], 'lines': []}
        ntile = 1
    else:
        cols = max(1, math.ceil(W / 900.0))
        rows = max(1, math.ceil(H / 500.0))
        ov = 120
        for r in range(rows):
            y0 = max(0, int(r * H / rows) - (ov if r else 0))
            y1 = min(H, int((r + 1) * H / rows) + (ov if r < rows - 1 else 0))
            for c in range(cols):
                x0 = max(0, int(c * W / cols) - (ov if c else 0))
                x1 = min(W, int((c + 1) * W / cols) + (ov if c < cols - 1 else 0))
                t = im.crop((x0, y0, x1, y1))
                mx = max(t.size)
                sc = max(1.0, min(3.0, 1400.0 / mx))
                if sc > 1.01:
                    t = t.resize((int(t.width * sc), int(t.height * sc)), Image.LANCZOS)
                ntile += 1
                try:
                    got = _ocr_array(t, np, eng, Image)
                except Exception:
                    got = []
                for ln in got:
                    k = ln['t'].strip()
                    if k and k not in seen:
                        seen.add(k)
                        lines.append(ln)
    for ln in list(lines):
        seen.add(ln['t'].strip())
    return {'name': name, 'w': W, 'h': H, 'tiles': ntile, 'nlines': len(lines), 'lines': lines}


def _work_retry(name):
    """高风险图：仅切块补测。"""
    return _work_single(name)


def ocr_images(docx_path, cache, procs=2, maxn=0, workdir=None):
    zf = zipfile.ZipFile(docx_path)
    media = [n for n in zf.namelist() if n.startswith('word/media/') and not n.endswith('/')]

    def key(n):
        d = ''.join(c for c in os.path.basename(n) if c.isdigit())
        return int(d) if d else 0
    media.sort(key=key)
    if maxn:
        media = media[:maxn]

    done = set()
    if os.path.exists(cache):
        for line in open(cache, encoding='utf-8'):
            if line.strip():
                done.add(json.loads(line)['name'])
    todo = [n for n in media if n not in done]
    print('图片总数 %d，待 OCR %d' % (len(media), len(todo)), flush=True)

    fout = open(cache, 'a', encoding='utf-8')
    ctx = mp.get_context('spawn')
    n, t0 = 0, time.time()
    if todo:
        with ctx.Pool(procs, initializer=_init_worker, initargs=(docx_path,)) as pool:
            for rec in pool.imap_unordered(_work_single, todo, chunksize=1):
                fout.write(json.dumps(rec, ensure_ascii=False) + '\n')
                fout.flush()
                n += 1
                if n % 10 == 0:
                    el = time.time() - t0
                    print('  %d/%d  %.1fs/张  剩余约 %.0f 分钟'
                          % (n, len(todo), el / n, el / n * (len(todo) - n) / 60), flush=True)
    fout.close()

    # 高风险图补测
    recs = {}
    for line in open(cache, encoding='utf-8'):
        if line.strip():
            r = json.loads(line)
            recs[r['name']] = r

    def risky(r):
        w, h = r.get('w', 0), r.get('h', 0)
        if w <= 0 or h <= 0 or r.get('skip'):
            return False
        n_ = len(r.get('lines') or [])
        area, ar = w * h, max(w, h) / max(1, min(w, h))
        return (n_ == 0) or (area >= 120000 and n_ <= 15) or (ar >= 4.0 and max(w, h) >= 800)

    retry = [n_ for n_, r in recs.items() if risky(r) and not r.get('retried')]
    if retry:
        print('高风险图补测 %d 张（切块+放大）' % len(retry), flush=True)
        with ctx.Pool(procs, initializer=_init_worker, initargs=(docx_path,)) as pool:
            for rec in pool.imap_unordered(_work_retry, retry, chunksize=1):
                rec['retried'] = True
                fout = open(cache, 'a', encoding='utf-8')
                fout.write(json.dumps(rec, ensure_ascii=False) + '\n')
                fout.close()
                print('  补测 %s 行数=%s' % (rec['name'], rec.get('nlines')), flush=True)
    print('OCR 完成（累计 %d 条记录）' % (len(recs) + (len(retry) if retry else 0)))


# =====================================================================
# 三、检索 OCR 缓存
# =====================================================================
def load_merged(cache):
    recs = {}
    for line in open(cache, encoding='utf-8'):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get('retried') or r.get('retry'):
            b = recs.get(r['name']) or {}
            old = list(b.get('lines') or [])
            seen = set(x['t'].strip() for x in old)
            for x in (r.get('lines') or []):
                if x['t'].strip() not in seen:
                    seen.add(x['t'].strip())
                    old.append(x)
            recs[r['name']] = {'name': r['name'], 'w': r.get('w'), 'h': r.get('h'), 'lines': old}
        elif r['name'] not in recs:
            recs[r['name']] = r
    return recs


def scan_cache(cache, kws):
    """OCR 结果检索。两级口径：L1 原始子串 / L2 归一化（去空白+半角化+去零宽）。
    OCR 常在标点前后插空格（'bd. zwbds. com'），只做 L1 会漏。"""
    recs = load_merged(cache)
    low = [k.lower() for k in kws]
    low_n = [_nospace(_norm(k)) for k in kws]
    out = []
    out.append('图片记录 %d 张；有文字 %d 张；总识别行 %d'
               % (len(recs), sum(1 for r in recs.values() if r.get('lines')),
                  sum(len(r.get('lines') or []) for r in recs.values())))
    out.append('')
    for k, kn in zip(low, low_n):
        hits = []
        for name, r in recs.items():
            for ln in (r.get('lines') or []):
                t = ln['t']
                if k in t.lower():
                    hits.append(('L1', os.path.basename(name), round(ln['s'], 2), t))
                elif kn and kn in _nospace(_norm(t)):
                    hits.append(('L2', os.path.basename(name), round(ln['s'], 2), t))
        n1 = sum(1 for h in hits if h[0] == 'L1')
        n2 = sum(1 for h in hits if h[0] == 'L2')
        out.append('【%s】命中 %d 处（L1 原始 %d / L2 归一化新增 %d）' % (k, len(hits), n1, n2))
        for lv, nm, sc, t in hits[:60]:
            out.append('   %-2s %-20s %.2f  %s' % (lv, nm, sc, t[:150]))
        out.append('')
    return out


# =====================================================================
def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    mode = sys.argv[1]
    docx = sys.argv[2]
    rest = sys.argv[3:]

    def opt(flag, default=None):
        if flag in rest:
            i = rest.index(flag)
            v = rest[i + 1]
            del rest[i:i + 2]
            return v
        return default

    procs = int(opt('--procs', '2'))
    cache = opt('--cache')
    workdir = opt('--workdir')
    maxn = int(opt('--max', '0'))
    kws = [x for x in rest if not x.startswith('--')]

    if mode == 'text':
        lines = scan_text(docx, kws or ['bd.'])
    elif mode == 'images':
        if not cache:
            cache = os.path.join(os.path.dirname(os.path.abspath(docx)),
                                 '_ocr_cache.jsonl')
        ocr_images(docx, cache, procs=procs, maxn=maxn, workdir=workdir)
        lines = scan_cache(cache, kws or ['bd.'])
    elif mode == 'report':
        lines = scan_cache(docx, kws or ['bd.'])
    else:
        print(__doc__)
        return
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
