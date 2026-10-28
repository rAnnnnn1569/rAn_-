#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""脚本目录「脱敏后入库」工具 —— 生成脱敏副本到 Git 仓库。

两轮脱敏：
  第一轮（键值对）："password": "xxx" / key = "xxx" 形式的凭据
  第二轮（语义）：裸 IP 字面量、SIM 卡号、第三方公司名

用法：
  python desensitize.py \
      --src  "D:\\python\\whitelist" \
      --dst  "H:\\agent工作空间\\agent_Memory\\docs\\源码\\whitelist" \
      --clean wl_audit.py ban_domains.txt \
      --mask  whitelist_check.py ipfinder.py README.md

  --clean   原样复制的文件（无凭据）
  --mask    脱敏后复制的文件（加【脱敏副本】文件头）
  --skip-ip 整份跳过第二轮 IP/SIM/公司名清洗的文件（如禁止 IP 词库）
  --src-label 文件头里标注的原文路径前缀（默认取 --src）

跑完务必再执行 verify.py 校验残留。
"""
import argparse
import pathlib
import re

# ── 第一轮：键值对占位符 ──────────────────────────────────
PH = {
    'password': '<你的强密码>', 'passwd': '<你的强密码>', 'pwd': '<你的强密码>',
    'pass': '<你的强密码>',
    'username': '<数据库账号>', 'user': '<数据库账号>', 'usr': '<数据库账号>',
    'ip': '<数据库主机IP>', 'host': '<数据库主机IP>', 'hostname': '<数据库主机IP>',
    'dbname': '<数据库名>', 'db': '<数据库名>', 'database': '<数据库名>',
    'db_name': '<数据库名>',
    'token': '<令牌>', 'secret': '<密钥>', 'api_key': '<API Key>',
    'apikey': '<API Key>', 'appkey': '<AppKey>', 'app_key': '<AppKey>',
    'sessionid': '<令牌>',
}
SENS = set(PH)

# 变量名里含这些子串 → 赋值字符串一律脱敏
VAR_SEC = ['password', 'passwd', 'pwd', 'token', 'secret', 'sessionid', 'sso',
           'webhook', 'ipurl', 'appkey', 'apikey', 'mysql_conf']

# ── 第二轮：保留的网段（脱敏时不动）────────────────────────
PROTECT_CIDR = [
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8',
    '100.64.0.0/10', '169.254.0.0/16', '224.0.0.0/4', '0.0.0.0/8',
    '240.0.0.0/4', '198.18.0.0/15',
    '192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24', '255.0.0.0/8',
]
IP_PLACEHOLDER = '192.0.2.9'      # RFC 5737 文档段，保持代码可解析

PAIR = re.compile(r"""(['"])(\w{2,30})\1(\s*:\s*)(['"])([^'"]{2,})\4""")
ASSIGN = re.compile(r"""(\b[\w]{2,40}\b)(\s*=\s*)(['"])([^'"]{4,})\3""")
IPRE = re.compile(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b')
SIMRE = re.compile(r'\b1[3-9]\d{11,13}\b')
COMPRE = re.compile(r'[\u4e00-\u9fa5]{2,20}(?:股份有限公司|有限公司|科技公司)')

HEADER = """# ─────────────────────────────────────────────────────────────
【脱敏副本】原文：{origin}
脱敏范围：数据库连接配置（主机/端口/账号/口令/库名）、接口地址与会话令牌、
          客户 SIM 卡号、机器人 webhook、真实公网 IP、第三方公司名称
占位符：  <数据库主机IP> <数据库账号> <你的强密码> <数据库名> <令牌>
          <API Key> <SIM卡号> <第三方公司> {ip_ph}
⚠️ 已去除凭据，不能直接运行；运行须在本地补齐配置。
─────────────────────────────────────────────────────────────
"""


def read_text(p: pathlib.Path) -> str:
    b = p.read_bytes()
    for enc in ('utf-8', 'gbk', 'gb18030'):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode('utf-8', 'ignore')


def mask_credentials(t: str) -> str:
    """第一轮：键值对 / 赋值形式的凭据脱敏。"""
    def _pair(m):
        k = m.group(2).lower()
        if k in SENS:
            return (f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}"
                    f"{m.group(4)}{PH[k]}{m.group(4)}")
        return m.group(0)

    def _assign(m):
        v = m.group(1).lower()
        if any(s in v for s in VAR_SEC):
            return f"{m.group(1)}{m.group(2)}{m.group(3)}<已脱敏>{m.group(3)}"
        return m.group(0)

    return ASSIGN.sub(_assign, PAIR.sub(_pair, t))


def mask_semantic(t: str, keep_company: tuple = ('中卫北斗',)) -> str:
    """第二轮：裸 IP / SIM 卡号 / 第三方公司名。"""
    import ipaddress
    nets = [ipaddress.ip_network(n) for n in PROTECT_CIDR]

    def _ip(m):
        try:
            a = ipaddress.ip_address(m.group(1))
        except ValueError:
            return m.group(0)
        return m.group(0) if any(a in n for n in nets) else IP_PLACEHOLDER

    t = IPRE.sub(_ip, t)
    t = SIMRE.sub('<SIM卡号>', t)
    t = COMPRE.sub(
        lambda m: m.group(0) if any(k in m.group(0) for k in keep_company)
        else '<第三方公司>', t)
    return t


def main():
    ap = argparse.ArgumentParser(description='脚本目录脱敏入库')
    ap.add_argument('--src', required=True, help='源目录')
    ap.add_argument('--dst', required=True, help='目标目录（仓库内）')
    ap.add_argument('--clean', nargs='*', default=[], help='原样复制（无凭据）')
    ap.add_argument('--mask', nargs='*', default=[], help='脱敏复制')
    ap.add_argument('--skip-ip', nargs='*', default=[],
                    help='整份跳过第二轮清洗的文件名（如禁止 IP 词库）')
    ap.add_argument('--keep-company', default='中卫北斗',
                    help='保留的公司名关键词（默认 中卫北斗）')
    ap.add_argument('--src-label', default=None, help='文件头标注的原文路径前缀')
    args = ap.parse_args()

    src = pathlib.Path(args.src)
    dst = pathlib.Path(args.dst)
    dst.mkdir(parents=True, exist_ok=True)
    label = args.src_label or str(src)
    keep = tuple(x for x in args.keep_company.split(',') if x)

    log = []
    for name in args.clean:
        s = src / name
        if not s.exists():
            log.append(f"  ✘ 缺失 {name}")
            continue
        (dst / name).write_text(read_text(s), encoding='utf-8')
        log.append(f"  ✅ 原样  {name}")

    for name in args.mask:
        s = src / name
        if not s.exists():
            log.append(f"  ✘ 缺失 {name}")
            continue
        t = mask_credentials(read_text(s))
        if name not in args.skip_ip:
            t = mask_semantic(t, keep_company=keep)
        origin = f"{label}\\{name}"
        if t.startswith('#!'):
            first, _, rest = t.partition('\n')
            t = first + '\n' + HEADER.format(origin=origin, ip_ph=IP_PLACEHOLDER) + rest
        else:
            t = HEADER.format(origin=origin, ip_ph=IP_PLACEHOLDER) + t
        (dst / name).write_text(t, encoding='utf-8')
        log.append(f"  🔒 脱敏  {name}")

    print("=== 已生成脱敏副本 ===")
    print('\n'.join(log))
    print(f"\n目标目录：{dst}")
    print("⚠️ 下一步必须执行 verify.py 校验残留，通过后再 commit")


if __name__ == '__main__':
    main()
