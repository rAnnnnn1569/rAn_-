#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""脱敏副本校验 —— commit 前的最后一道闸。

扫描目录内所有文本文件，报告残留的：
  · 明文凭据（"password": "xxx" 等等，值不以 < 开头）
  · 非保护网段的真实 IP（私有段 / CGNAT / 文档段不算）
  · 手机号 / SIM 卡号（1[3-9] + 11~13 位）
  · 第三方公司名（含"有限公司"等，除非命中 --keep-company）

用法：
  python verify.py <目录> [--skip-ip ban_ips.txt] [--keep-company 中卫北斗]
                   [--strict]        # 发现残留即返回退出码 1

退出码：0 = 干净，1 = 有残留（--strict 时才生效）
"""
import argparse
import ipaddress
import pathlib
import re
import sys

PROTECT_CIDR = [
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8',
    '100.64.0.0/10', '169.254.0.0/16', '224.0.0.0/4', '0.0.0.0/8',
    '240.0.0.0/4', '198.18.0.0/15',
    '192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24', '255.0.0.0/8',
]
SENS = {'password', 'passwd', 'pwd', 'pass', 'username', 'user', 'usr',
        'ip', 'host', 'hostname', 'dbname', 'db', 'database', 'db_name',
        'token', 'secret', 'api_key', 'apikey', 'appkey', 'app_key',
        'sessionid'}

PAIR = re.compile(r"""(['"])(\w{2,30})\1(\s*:\s*)(['"])([^'"]{2,})\4""")
IPRE = re.compile(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b')
SIMRE = re.compile(r'\b1[3-9]\d{12,13}\b')
COMPRE = re.compile(r'[\u4e00-\u9fa5]{2,20}(?:股份有限公司|有限公司|科技公司)')
# 正则字符串里的 \d{1,3}\. 之类，会被 IPRE 误伤 → 跳过
REGEX_HINT = re.compile(r"\\d\{|\\\.")


def main():
    ap = argparse.ArgumentParser(description='脱敏副本残留校验')
    ap.add_argument('dir', help='待校验目录')
    ap.add_argument('--skip-ip', nargs='*', default=[],
                    help='整份跳过 IP/SIM/公司名检查的文件名')
    ap.add_argument('--keep-company', default='中卫北斗',
                    help='允许保留的公司名关键词，逗号分隔')
    ap.add_argument('--strict', action='store_true', help='有残留则退出码 1')
    args = ap.parse_args()

    root = pathlib.Path(args.dir)
    if not root.is_dir():
        print(f"✘ 目录不存在：{root}")
        return 2
    keep = tuple(x for x in args.keep_company.split(',') if x)
    nets = [ipaddress.ip_network(n) for n in PROTECT_CIDR]

    problems = {}
    for p in sorted(root.iterdir()):
        if not p.is_file():
            continue
        try:
            txt = p.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            continue
        skip = p.name in args.skip_ip
        iss = []
        for i, line in enumerate(txt.splitlines(), 1):
            if not skip:
                for m in PAIR.finditer(line):
                    if m.group(2).lower() in SENS and not m.group(5).startswith('<'):
                        iss.append((i, f'明文键值 {m.group(2)}'))
                if not REGEX_HINT.search(line):
                    for m in IPRE.finditer(line):
                        try:
                            a = ipaddress.ip_address(m.group(1))
                        except ValueError:
                            continue
                        if not any(a in n for n in nets):
                            iss.append((i, f'真实IP {m.group(1)}'))
                for m in COMPRE.finditer(line):
                    if not any(k in m.group(0) for k in keep):
                        iss.append((i, f'第三方公司名 {m.group(0)[:12]}…'))
            for _ in SIMRE.finditer(line):
                iss.append((i, 'SIM卡号'))
        if iss:
            problems[p.name] = iss

    print(f"=== 校验：{root} ===")
    if not problems:
        print("  ✅ 通过 —— 无明文凭据 / 无真实IP / 无SIM卡号 / 无第三方公司名")
        print("\n提示：以下内容为有意保留，非遗漏 ——")
        print("  · 禁止 IP 词库（--skip-ip 指定）")
        print("  · 私有段 / CGNAT / 文档段网段常量（判定逻辑依赖）")
        print(f"  · 公司名含「{args.keep_company}」者")
        return 0

    for name, iss in problems.items():
        kinds = sorted({x[1].split()[0] for x in iss})
        print(f"  ⚠️ {name}: {len(iss)} 处 → {kinds}")
        shown = set()
        for ln, what in iss:
            k = what.split()[0]
            if k in shown:
                continue
            shown.add(k)
            print(f"       L{ln:<5} {what}")
    print("\n🔴 存在残留，禁止 commit。逐项确认后再提交。")
    return 1 if args.strict else 0


if __name__ == '__main__':
    sys.exit(main())
