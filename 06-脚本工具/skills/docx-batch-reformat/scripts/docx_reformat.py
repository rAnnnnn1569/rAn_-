#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""docx 段落级批量改写工具（OOXML 直改 + 零附带损伤校验）

子命令
------
  ref      <docx> <refParaId>                 打印参照段格式指纹（pPr 模板 / run rPr）
  list     <docx> [--pstyle N] [--outline N] [--grep KW] [--pidfile F]
                                              列出段落：pid / 样式 / 大纲 / 文字
  apply    <docx> <refParaId> --pids F [--out F] [--no-outline]
                                              按参照段格式批量改写，只改格式、文字不动
  verify   <before.docx> <after.docx> --pids F
                                              零附带损伤校验（差异段数 == 计划数 / 文字零变化）
  deploy   <docx> <new.docx> [--nobak]        备份 + 试写 3 次 + sha 回读

设计铁律（踩过的坑，勿改）
--------------------------
  1. 段落正则必须「自闭合优先」，否则 <w:p .../> 会贪婪吞到下一段：
       P_RE = re.compile(r"<w:p\\b[^>]*/>|<w:p\\b[^>]*>.*?</w:p>", re.S)
  2. pPr 子元素须遵 schema 顺序：pStyle → spacing → ind → jc → outlineLvl → rPr
  3. 只改 word/document.xml 一个 zip 条目，其余条目必须逐字节不变
  4. 校验的硬指标：差异段落实体数 == 计划变更数（这才叫零附带损伤）
  5. 目标集用 w14:paraId 清单（跨轮次稳定），不要用序号或文字匹配
"""
import argparse, csv, glob, hashlib, os, re, shutil, sys, time, zipfile

P_RE = re.compile(r"<w:p\b[^>]*/>|<w:p\b[^>]*>.*?</w:p>", re.S)
RUN_RE = re.compile(r"<w:r\b[^>]*?>.*?</w:r>|<w:r\b[^>]*?/>", re.S)
RPR_RE = re.compile(r"<w:rPr>.*?</w:rPr>", re.S)
T_RE = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.S)
PID_RE = re.compile(r'w14:paraId="([^"]+)"')
DOCXML = "word/document.xml"


# ----------------------------------------------------------------- 基础
def read_doc_xml(path):
    with zipfile.ZipFile(path) as z:
        return z.read(DOCXML).decode("utf-8")


def para_list(x):
    return list(P_RE.finditer(x))


def para_text(seg):
    return "".join(T_RE.findall(seg))


def para_pid(seg):
    m = PID_RE.search(seg)
    return m.group(1) if m else None


def find_para(x, pid):
    for m in P_RE.finditer(x):
        if 'w14:paraId="%s"' % pid in m.group(0):
            return m
    return None


def sha(path, n=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(n)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def writable(path):
    try:
        with open(path, "r+b"):
            return True
    except Exception:
        return False


# ----------------------------------------------------------------- 模板提取
def extract_ppr_body(ref_seg):
    """参照段 pPr 去掉 outlineLvl 与段落标记 rPr，作为模板前段。"""
    m = re.search(r"<w:pPr>(.*?)</w:pPr>", ref_seg, re.S)
    body = m.group(1) if m else ""
    body = re.sub(r"<w:outlineLvl[^>]*/>", "", body)
    body = re.sub(r"<w:rPr>.*?</w:rPr>", "", body, flags=re.S)
    return body


def extract_run_rpr(ref_seg):
    """参照段第一个纯文字 run 的 rPr；无则返回 None（表示应删除 run 的 rPr）。"""
    for m in RUN_RE.finditer(ref_seg):
        seg = m.group(0)
        if any(k in seg for k in ("<w:drawing", "<w:pict", "<w:object")):
            continue
        if "<w:t" not in seg:
            continue
        r = RPR_RE.search(seg)
        return r.group(0) if r else None
    return None


STRUCT = ("<w:drawing", "<w:pict", "<w:object", "<w:fldChar", "<w:instrText",
          "<w:tab", "<w:br", "<w:sym", "<w:noBreakHyphen")


def reformat_para(seg, ppr_body, run_rpr, keep_outline=True):
    """只换格式：pPr 整体替换（outlineLvl 沿用原值）+ 段内每个 run 的 rPr 替换。"""
    m = re.search(r"<w:pPr>.*?</w:pPr>", seg, re.S)
    if m:
        head, tail = seg[:m.start()], seg[m.end():]
    else:
        mm = re.match(r"<w:p\b[^>]*?>", seg)
        if not mm:
            return None
        head, tail = mm.group(0), seg[mm.end():]

    ol = re.search(r'<w:outlineLvl w:val="(\d+)"', seg) if keep_outline else None
    olxml = '<w:outlineLvl w:val="%s"/>' % ol.group(1) if ol else ""

    def sub(mm):
        s = mm.group(0)
        if run_rpr is None:
            return RPR_RE.sub("", s, count=1)
        if RPR_RE.search(s):
            return RPR_RE.sub(run_rpr, s, count=1)
        return re.sub(r"^(<w:r\b[^>]*?>)", lambda z: z.group(1) + run_rpr, s, count=1)

    tail = RUN_RE.sub(sub, tail)
    return head + "<w:pPr>" + ppr_body + olxml + "</w:pPr>" + tail


# ----------------------------------------------------------------- 校验
def structural_checks(x_old, x_new, tag=""):
    """结构不变性：段落数 / drawing / 表格 / XML 可解析 / 无游离 pPr / pPr 顺序"""
    import xml.etree.ElementTree as ET
    errs = []
    for nm, pat in (("段落", r"<w:p\b"), ("drawing", r"<w:drawing"), ("表格", r"<w:tbl>")):
        a, b = len(re.findall(pat, x_old)), len(re.findall(pat, x_new))
        print("  %-8s 原/新: %d %d" % (nm, a, b))
        if a != b:
            errs.append("%s数量变化" % nm)
    try:
        ET.fromstring(x_new.encode("utf-8"))
        print("  XML 解析: OK")
    except Exception as e:
        errs.append("XML 解析失败: %s" % e)

    stray = 0
    for m in re.finditer(r"<w:pPr>", x_new):
        pre = x_new[:m.start()]
        j = pre.rfind("<w:p ")
        k = max(pre.rfind("</w:p>"), pre.rfind("<w:p/>"),
                pre.rfind("<w:tbl>"), pre.rfind("</w:tbl>"))
        if k > j:
            stray += 1
    print("  游离 pPr:", stray)
    if stray:
        errs.append("游离 pPr %d 处" % stray)

    ORD = ["<w:pStyle", "<w:spacing", "<w:ind", "<w:jc", "<w:outlineLvl", "<w:rPr>"]
    bad = 0
    for m in re.finditer(r"<w:pPr>(.*?)</w:pPr>", x_new, re.S):
        s = m.group(1)
        if "w:pStyle" not in s:
            continue
        idxs = [s.find(k) for k in ORD if s.find(k) >= 0]
        if idxs != sorted(idxs):
            bad += 1
    print("  pPr 子元素顺序异常:", bad)
    if bad:
        errs.append("pPr 顺序异常 %d 处" % bad)
    return errs


def verify_pair(before_docx, after_docx, plan_pids):
    xb, xa = read_doc_xml(before_docx), read_doc_xml(after_docx)
    pb, pa = para_list(xb), para_list(xa)
    print("段落实体数 改前/改后: %d %d" % (len(pb), len(pa)))
    errs = []
    if len(pb) != len(pa):
        return ["段落实体数不一致"]

    plan = set(plan_pids)
    changed, unplanned = [], []
    for i in range(len(pb)):
        if pb[i].group(0) == pa[i].group(0):
            continue
        pid = para_pid(pb[i].group(0))
        changed.append(pid)
        if pid not in plan:
            unplanned.append((i, pid))
    miss = [p for p in plan if p not in set(changed)]
    print("实际差异段落: %d == 计划 %d ? %s" % (len(changed), len(plan), len(changed) == len(plan)))
    print("  计划外被改:", len(unplanned), unplanned[:8])
    print("  计划内未生效:", len(miss), miss[:8])
    if unplanned:
        errs.append("计划外被改 %d" % len(unplanned))
    if miss:
        errs.append("漏改 %d" % len(miss))

    tb, ta = "".join(T_RE.findall(xb)), "".join(T_RE.findall(xa))
    print("全文文字完全一致:", tb == ta)
    if tb != ta:
        for i in range(min(len(tb), len(ta))):
            if tb[i] != ta[i]:
                print("   首差 @%d: %r || %r" % (i, tb[i - 60:i + 60], ta[i - 60:i + 60]))
                break
        errs.append("文字发生变化")

    # 条目级零差异
    with zipfile.ZipFile(before_docx) as zb, zipfile.ZipFile(after_docx) as za:
        nb = {i.filename for i in zb.infolist()}
        na = {i.filename for i in za.infolist()}
        if nb != na:
            errs.append("zip 条目名集合不一致")
        d = {n for n in nb if zb.read(n) != za.read(n)}
    print("内容差异条目:", sorted(d))
    if d - {DOCXML}:
        errs.append("非 document.xml 条目被改: %s" % sorted(d - {DOCXML}))

    print("\n校验错误数:", len(errs), errs)
    return errs


# ----------------------------------------------------------------- 子命令
def cmd_ref(a):
    x = read_doc_xml(a.docx)
    m = find_para(x, a.ref)
    if not m:
        raise SystemExit("参照段 %s 未找到" % a.ref)
    seg = m.group(0)
    print("=== 参照段 %s ===" % a.ref)
    print("文字:", para_text(seg)[:200])
    print("\n-- pPr 模板（已剔除 outlineLvl / 段落rPr）--")
    print(extract_ppr_body(seg))
    print("\n-- run rPr --")
    print(extract_run_rpr(seg))
    print("\n-- 原文 --")
    print(seg[:1500])


def cmd_list(a):
    x = read_doc_xml(a.docx)
    out = []
    for i, m in enumerate(para_list(x)):
        seg = m.group(0)
        ps = re.search(r'<w:pStyle w:val="([^"]+)"', seg)
        ol = re.search(r'<w:outlineLvl w:val="(\d+)"', seg)
        t = para_text(seg)
        rec = dict(idx=i, pid=para_pid(seg), pstyle=ps.group(1) if ps else "",
                   outline=ol.group(1) if ol else "", text=t)
        if a.pstyle and rec["pstyle"] != a.pstyle:
            continue
        if a.outline is not None and rec["outline"] != a.outline:
            continue
        if a.grep and a.grep not in t:
            continue
        out.append(rec)
        print("idx=%-5d pid=%-10s pStyle=%-6s out=%-3s %s" % (
            rec["idx"], rec["pid"], rec["pstyle"] or "-", rec["outline"] or "-", t[:60]))
    print("\n合计:", len(out))
    if a.pidfile:
        with open(a.pidfile, "w", encoding="utf-8") as f:
            for r in out:
                f.write(r["pid"] + "\n")
        print("pid 清单已写:", a.pidfile, len(out), "条")


def cmd_apply(a):
    x = read_doc_xml(a.docx)
    ref = find_para(x, a.ref)
    if not ref:
        raise SystemExit("参照段 %s 未找到" % a.ref)
    ppr_body = extract_ppr_body(ref.group(0))
    run_rpr = extract_run_rpr(ref.group(0))
    print("模板 pPr :", ppr_body)
    print("模板 rPr :", run_rpr)

    pids = [l.strip() for l in open(a.pids, encoding="utf-8") if l.strip()]
    print("\n目标段:", len(pids))

    paras = para_list(x)
    by_pid = {}
    for m in paras:
        by_pid.setdefault(para_pid(m.group(0)), m)
    new_seg, missing = {}, []
    for pid in pids:
        m = by_pid.get(pid)
        if not m:
            missing.append(pid); continue
        seg = m.group(0)
        if re.match(r"<w:p\b[^>]*/>$", seg):
            missing.append(pid + "(自闭合空段)"); continue
        ns = reformat_para(seg, ppr_body, run_rpr, keep_outline=not a.no_outline)
        if ns is None:
            missing.append(pid + "(无起始标签)"); continue
        new_seg[pid] = ns
    print("已改:", len(new_seg), " 缺失:", len(missing), missing[:10])

    out = x
    for m in sorted(paras, key=lambda z: z.start(), reverse=True):
        pid = para_pid(m.group(0))
        if pid in new_seg:
            out = out[:m.start()] + new_seg[pid] + out[m.end():]

    errs = structural_checks(x, out)
    if errs:
        raise SystemExit("结构自检失败: %s" % errs)

    dst = a.out or os.path.join(os.getcwd(), "docx_new.docx")
    with zipfile.ZipFile(a.docx) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == DOCXML:
                data = out.encode("utf-8")
            zo.writestr(item, data)
    print("\n已生成:", dst, os.path.getsize(dst), "bytes")


def cmd_verify(a):
    pids = [l.strip() for l in open(a.pids, encoding="utf-8") if l.strip()]
    errs = verify_pair(a.before, a.after, pids)
    raise SystemExit(1 if errs else 0)


def cmd_deploy(a):
    d = os.path.dirname(a.docx)
    if not a.nobak:
        ts = time.strftime("%Y%m%d_%H%M%S")
        bak = os.path.join(d, "_bak_%s_%s.docx" % (os.path.basename(a.docx)[:-5], ts))
        shutil.copy2(a.docx, bak)
        print("备份:", bak, os.path.getsize(bak))

    for i in range(3):
        if writable(a.docx):
            shutil.copy2(a.new, a.docx)
            same = sha(a.new) == sha(a.docx)
            print("写回成功(第%d次)" % (i + 1), " sha 一致:", same)
            print("  注意: copy2 会保留源文件 mtime，mtime 显示为产物生成时刻属正常")
            raise SystemExit(0 if same else 1)
        print("第%d次: 被占用" % (i + 1))
        if i < 2:
            time.sleep(4)
    print("!! 目标被独占锁定。判据: 目录内有无对应 ~$ 锁文件；wps.exe 是否在跑。")
    sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description="docx 段落级批量改写")
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("ref"); p.add_argument("docx"); p.add_argument("ref"); p.set_defaults(f=cmd_ref)
    p = sp.add_parser("list"); p.add_argument("docx")
    p.add_argument("--pstyle"); p.add_argument("--outline")
    p.add_argument("--grep"); p.add_argument("--pidfile"); p.set_defaults(f=cmd_list)
    p = sp.add_parser("apply")
    p.add_argument("docx"); p.add_argument("ref"); p.add_argument("--pids", required=True)
    p.add_argument("--out"); p.add_argument("--no-outline", action="store_true"); p.set_defaults(f=cmd_apply)
    p = sp.add_parser("verify")
    p.add_argument("before"); p.add_argument("after"); p.add_argument("--pids", required=True)
    p.set_defaults(f=cmd_verify)
    p = sp.add_parser("deploy")
    p.add_argument("docx"); p.add_argument("new"); p.add_argument("--nobak", action="store_true")
    p.set_defaults(f=cmd_deploy)

    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
