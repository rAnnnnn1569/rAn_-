#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
protocol_lookup.py — 中卫北斗/车联网 车载协议查档工具

子命令:
  grep "<kw>[,<kw2>...]" [--repo PATH] [--doc NAME] [--ctx N]
      在 docs/协议/*.txt 中按关键词检索，输出 文件:行号:匹配行(含上下文)。
  pdf <pdf-path> [start] [end] [--repo PATH]
      抽取文本层 PDF 的指定页范围文字（扫描件会返回乱码，反过来证明它是扫描件）。
  list [--repo PATH]
      列出全部协议文档：txt/pdf 是否存在、文本层/扫描件状态。

依赖: pypdf / pdfplumber (python -m pip install pypdf pdfplumber)
"""
import os
import sys
import glob
import argparse

DEFAULT_REPO = os.environ.get("AGENT_MEMORY_REPO", r"H:/agent工作空间/agent_Memory")

# txt 文件名(去掉扩展名) -> 说明
TXT_DOCS = {
    "JT808-2019_OCR": "JT/T 808-2019 道路运输车辆卫星定位系统车载终端通讯协议",
    "JT809-2019_OCR": "JT/T 809-2019 道路运输车辆卫星定位系统平台数据交换",
    "20号文_OCR": "川运安稽便(2020)20号 主动安全智能防控服务商工作指南",
    "TSCSDX_0003.1_平台": "四川 TSCSDX 0003.1 平台",
    "TSCSDX_0003.2_车载终端": "四川 TSCSDX 0003.2 车载终端",
    "TSCSDX_0003.3_终端通信协议及数据格式": "四川 TSCSDX 0003.3 终端通信协议及数据格式",
    "TSCSDX_0003.4_平台数据交换": "四川 TSCSDX 0003.4 平台数据交换",
    "TSCSDX_0003.5_出租汽车专用终端": "四川 TSCSDX 0003.5 出租汽车专用终端",
    "TSCSDX_0003.6_出租专用终端通信协议": "四川 TSCSDX 0003.6 出租专用终端通信协议",
    "TSCSDX_0003.7_出租数据交换与共享": "四川 TSCSDX 0003.7 出租数据交换与共享",
    "DB32T3610.1-2019": "DB32/T 3610.1-2019 江苏地标 主动安全智能防控系统 第1部分：平台",
    "DB32T3610.2-2019": "DB32/T 3610.2-2019 江苏地标 主动安全智能防控系统 第2部分：车载终端",
    "DB32T3610.3-2019": "DB32/T 3610.3-2019 江苏地标 主动安全智能防控系统 第3部分：通信协议及数据格式",
    "两客一危部省交互规范-试行": "《“两客一危”动态监控报警信息数据交互技术规范（试行）》（中国交通通信信息中心 2026-03，部级，含附录B.1智能监控报警码0xD201~0xD508）",
    "四川新主防今点_平台对接内容": "四川新主防(今点) 平台对接内容总览（809主防对接：链路/基础/位置附加/视频四类消息ID清单）",
    "四川新主防今点_视频补充协议": "SC《道路运输车辆卫星定位系统视频通信协议》四川补充协议(2020-7-7)：JT/T 1078四川扩展，音视频参数、视频报警标志位、视频平台间子业务类型、视频报警类型编码0x02xx",
    "四川新主防今点_升级技术说明": "四川动态监控报警数据交互-平台升级技术说明(2026-04)：部↔川2021↔川2025报警类型三标映射表 + ALARM_ID(34位)规则（优先级最高）",
    "四川新主防今点_查岗功能对接": "四川省主防系统企业查岗功能对接文档(2025-11)：查岗0x9301下发 + 二维码签到 + 签到查询接口",
}

# 文本层 vs 扫描件(实测 2026-08-26)
TEXT_LAYER_PDFS = ["TSCSDX 0003.1", "TSCSDX 0003.2", "TSCSDX 0003.3",
                   "TSCSDX 0003.4", "TSCSDX 0003.5", "TSCSDX 0003.6", "TSCSDX 0003.7"]
SCANNED_PDFS = ["808-2019", "809-2019"]
TEXT_LAYER_PDFS += ["DB32T3610.1-2019", "DB32T3610.2-2019", "DB32T3610.3-2019"]
TEXT_LAYER_PDFS += ["两客一危部省交互规范-试行"]
TEXT_LAYER_PDFS += ["四川新主防今点_升级技术说明"]


def resolve_repo(args_repo):
    return args_repo or DEFAULT_REPO


def cmd_grep(keywords, repo, doc_filter=None, ctx=2):
    kws = [k.strip() for k in keywords.split(",") if k.strip()]
    if not kws:
        print("ERROR: 未提供关键词", file=sys.stderr)
        return 1
    pattern = "(.{0,30}(?i:" + "|".join(kws) + "))"  # 粗匹配，含上下文
    import re
    rx = re.compile(pattern)
    txt_dir = os.path.join(repo, "docs", "协议")
    files = sorted(glob.glob(os.path.join(txt_dir, "*.txt")))
    # 用户笔记下的"重点总结"md 一并纳入检索范围（概念性归纳，便于协议类问题统一检索）
    summary_dir = os.path.join(repo, "docs", "用户笔记")
    files += sorted(glob.glob(os.path.join(summary_dir, "*重点总结*.md")))
    hits = 0
    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        if doc_filter and doc_filter.lower() not in base.lower():
            continue
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except Exception as e:
            print(f"  [跳过] {f}: {e}")
            continue
        for i, line in enumerate(lines):
            if rx.search(line):
                hits += 1
                lo = max(0, i - ctx)
                hi = min(len(lines), i + ctx + 1)
                tag = " [总结]" if "重点总结" in os.path.basename(f) else ""
                print(f"\n### {os.path.basename(f)}{tag} : 行 {i+1} ###")
                for j in range(lo, hi):
                    mark = ">>" if j == i else "  "
                    print(f"{mark} {j+1:>5}| {lines[j].rstrip()}")
    if hits == 0:
        print("（无匹配）")
    else:
        print(f"\n=== 共 {hits} 处匹配 ===")
    return 0


def cmd_pdf(pdf_path, repo, start, end):
    # pdf_path 可为相对 repo 的路径或绝对路径
    if not os.path.isabs(pdf_path):
        cand = os.path.join(repo, pdf_path)
        pdf_path = cand if os.path.exists(cand) else pdf_path
    if not os.path.exists(pdf_path):
        print(f"ERROR: 找不到 PDF: {pdf_path}", file=sys.stderr)
        return 1
    try:
        import pdfplumber
    except ImportError:
        print("ERROR: 需安装 pdfplumber (python -m pip install pdfplumber)", file=sys.stderr)
        return 1
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        s = max(1, start or 1)
        e = min(n, end or s)
        print(f"# {os.path.basename(pdf_path)} 总页 {n}，抽取 {s}-{e}")
        for i in range(s - 1, e):
            t = pdf.pages[i].extract_text() or ""
            print(f"\n----- 第 {i+1} 页 -----")
            print(t)
    return 0


def cmd_list(repo):
    txt_dir = os.path.join(repo, "docs", "协议")
    pdf_dir = os.path.join(repo, "docs", "协议原版")
    print(f"协议文本库: {repo}\n")
    print(f"{'文档(txt名)':<42} {'txt':<5} {'pdf':<5} {'类型'}")
    print("-" * 80)
    # 遍历 pdf 原版，反查 txt
    if os.path.isdir(pdf_dir):
        for f in sorted(os.listdir(pdf_dir)):
            if not f.lower().endswith((".pdf", ".pptx")):
                continue
            stem = os.path.splitext(f)[0]
            typ = ("文本层" if any(stem.startswith(p) for p in TEXT_LAYER_PDFS)
                   else "扫描件" if any(stem.startswith(p) for p in SCANNED_PDFS)
                   else "其它")
            # 找 txt 名包含关键字的
            txt_name = next((k for k in TXT_DOCS if k.split("_OCR")[0].split("_")[0] in stem
                             or k.replace("_", " ") in stem or stem.replace(" ", "").replace("—2025", "") in k), "")
            has_txt = "有" if txt_name else "无"
            print(f"{stem[:40]:<42} {has_txt:<5} {'有':<5} {typ}")
    print("\n注: 10号令(PPTX)、544号(PDF) 无 txt 版，仅有原版。")
    # 用户笔记重点总结（概念归纳，已被 grep 检索）
    summary_dir = os.path.join(repo, "docs", "用户笔记")
    sm = sorted(glob.glob(os.path.join(summary_dir, "*重点总结*.md")))
    if sm:
        print("\n--- 用户笔记重点总结（概念归纳，grep 已覆盖）---")
        for f in sm:
            print(f"  {os.path.basename(f)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="车载协议查档工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("grep", help="在 docs/协议/*.txt 检索关键词")
    g.add_argument("keywords", help="关键词，逗号分隔，如 超速,标志位")
    g.add_argument("--repo", default=None)
    g.add_argument("--doc", default=None, help="限定文档(文件名片段)，如 808")
    g.add_argument("--ctx", type=int, default=2, help="上下文行数")

    p = sub.add_parser("pdf", help="抽取 PDF 文本层")
    p.add_argument("pdf_path")
    p.add_argument("start", type=int, nargs="?", default=1)
    p.add_argument("end", type=int, nargs="?", default=None)
    p.add_argument("--repo", default=None)

    l = sub.add_parser("list", help="列出协议文档清单")
    l.add_argument("--repo", default=None)

    args = ap.parse_args()
    repo = resolve_repo(args.repo)
    if not os.path.isdir(repo):
        print(f"ERROR: 仓库路径不存在: {repo}", file=sys.stderr)
        return 1

    if args.cmd == "grep":
        return cmd_grep(args.keywords, repo, args.doc, args.ctx)
    if args.cmd == "pdf":
        return cmd_pdf(args.pdf_path, repo, args.start, args.end)
    if args.cmd == "list":
        return cmd_list(repo)
    return 1


if __name__ == "__main__":
    sys.exit(main())
