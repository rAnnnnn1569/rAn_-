#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JT/T 809-2019 / 川标 T/SCSDX 0003.4 报文帧解码器
--------------------------------------------------
用途：把一条 809 报文的 hex 解成「头标识 / 数据头各字段 / 数据体 / CRC / 尾标识」，
      并用「暴力搜索 CRC 覆盖区间」反推帧的真实边界，从而判断：
        - 帧是否完整（CRC 自洽）
        - 数据头 + 数据体 实际多少字节
        - 与标准数据头长度（29 字节）差多少 → 定位「解析越界」的根因

用法：
  python decode809.py "0x5B0000001D0000..."
  python decode809.py "5B 00 00 00 1D ..."            # 带空格也行
  python decode809.py --file frame.txt
  python decode809.py --header-len 26 "<hex>"          # 试别的数据头长度假设
  python decode809.py --json "<hex>"                   # 机器可读输出
"""
import argparse
import json
import re
import sys

# ───────── 表7 数据头字段（JT/T 809-2019 / 川标 0003.4，均为大端） ─────────
HEADER_FIELDS = [
    ("MSG_LENGTH",       4, "数据长度（含头标识/数据头/数据体/CRC/尾标识）"),
    ("MSG_SN",           4, "报文序列号"),
    ("MSG_ID",           2, "业务数据类型"),
    ("MSG_GNSSCENTERID", 4, "下级平台接入码（上级分配）"),
    ("VERSION_FLAG",     3, "协议版本号标识 Vx.y.z"),
    ("ENCRYPT_KEY",      4, "数据加密密钥（0=不加密）"),
    ("TIME",             8, "发送时间（UTC 秒，2019 版新增字段）"),
]
HEADER_LEN = sum(n for _, n, _ in HEADER_FIELDS)   # 29

# ───────── 业务数据类型标识（常用部分，来自 docs/协议/*） ─────────
MSG_NAMES = {
    0x1001: "主链路登录请求", 0x1002: "主链路登录应答", 0x1003: "主链路注销请求",
    0x1004: "主链路注销应答", 0x1005: "主链路连接保持请求", 0x1006: "主链路连接保持应答",
    0x1007: "主链路断开通知", 0x1008: "下级平台主动关闭主从链路通知",
    0x1102: "信息传办情况上报应答",
    0x9001: "从链路连接请求", 0x9002: "从链路连接应答", 0x9003: "从链路注销请求",
    0x9004: "从链路注销应答", 0x9005: "从链路连接保持请求", 0x9006: "从链路连接保持应答",
    0x9007: "从链路断开通知", 0x9008: "上级平台主动关闭主从链路通知",
    0x1201: "车辆注册信息上传", 0x1202: "实时上传车辆定位信息", 0x1203: "车辆定位信息自动补报",
    0x1205: "启动车辆定位信息交换应答", 0x1206: "结束车辆定位信息交换应答",
    0x1207: "申请交换指定车辆定位信息请求", 0x1208: "取消交换指定车辆定位信息请求",
    0x1209: "补发车辆定位信息请求", 0x120A: "上报驾驶员身份识别信息应答",
    0x120B: "上报车辆电子运单应答", 0x120C: "主动上报驾驶员身份信息",
    0x120D: "主动上报车辆电子运单信息", 0x120E: "主动上报车辆行驶路线信息",
    0x9201: "车辆注册信息应答", 0x9202: "交换车辆定位信息", 0x9203: "车辆定位信息交换补发",
    0x9204: "交换车辆静态信息", 0x9205: "启动车辆定位信息交换请求",
    0x9206: "结束车辆定位信息交换请求", 0x9207: "申请交换指定车辆定位信息应答",
    0x9208: "取消交换指定车辆定位信息应答", 0x9209: "补发车辆定位信息应答",
    0x920A: "上报驾驶员身份识别信息请求", 0x920B: "上报车辆电子运单请求",
    0x920C: "上报车辆行驶路线请求", 0x920D: "车辆行驶线路请求应答",
    0x1301: "平台查岗应答", 0x1302: "下发平台间报文应答", 0x1303: "上传平台间消息补传请求",
    0x9301: "平台查岗请求", 0x9302: "下发平台间报文请求 / 下发平台间消息补传请求",
    0x1401: "报警督办应答", 0x1402: "上报报警信息", 0x1403: "上报报警预警消息",
    0x1404: "报警附件目录请求应答", 0x1412: "主动上报报警处理结果信息",
    0x9401: "报警督办请求", 0x9404: "报警附件目录请求",
    0x1503: "下发车辆报文请求应答", 0x9503: "下发车辆报文请求",
    0x1701: "时效口令上报", 0x1802: "主动请求停止实时音视频传输应答",
    0x9802: "主动请求停止实时音视频传输请求", 0x1809: "实时视频请求应答（川标）",
    0x9809: "实时视频请求（川标）",
}

# 0x1002 / 0x9002 等应答消息的 RESULT 码含义（表12 / 表17）
RESULT_CODES = {
    0x00: "成功", 0x01: "IP 地址不正确", 0x02: "接入码不正确",
    0x03: "用户没有注册", 0x04: "密码错误",
    0x05: "资源紧张,稍后再连接(已经占用)", 0xFF: "其他",
}


def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """JT/T 809：CRC16-CCITT，生成多项式 0x1021，大端输出。"""
    crc = init
    for x in data:
        crc ^= x << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def unescape(data: bytes) -> bytes:
    """按 8.2.5 逆转义：5A01→5B、5A02→5A、5E01→5D、5E02→5E"""
    out, i = bytearray(), 0
    while i < len(data):
        b = data[i]
        if b in (0x5A, 0x5E) and i + 1 < len(data):
            pair = (b, data[i + 1])
            if pair == (0x5A, 0x01):
                out.append(0x5B); i += 2; continue
            if pair == (0x5A, 0x02):
                out.append(0x5A); i += 2; continue
            if pair == (0x5E, 0x01):
                out.append(0x5D); i += 2; continue
            if pair == (0x5E, 0x02):
                out.append(0x5E); i += 2; continue
        out.append(b); i += 1
    return bytes(out)


def parse_hex(text: str) -> bytes:
    t = re.sub(r"(?i)0x", "", text)
    t = re.sub(r"[^0-9a-fA-F]", "", t)
    if len(t) % 2:
        raise ValueError(f"hex 长度为奇数（{len(t)} 位），无法按字节切分")
    return bytes.fromhex(t)


def find_crc_ranges(b: bytes, target: int):
    """暴力找「哪一段的 CRC 等于 target」，用于反推帧真实边界。"""
    hits = []
    for init in (0xFFFF, 0x0000):
        for s in range(0, 3):
            for e in range(2, len(b) + 1):
                if crc16_ccitt(b[s:e], init) == target:
                    hits.append({"start": s, "end": e, "nbytes": e - s, "init": init})
    return hits


def decode(hexstr: str, header_len: int = HEADER_LEN) -> dict:
    raw = parse_hex(hexstr)
    r = {"输入字节数": len(raw), "unescape": False}

    # 1) 标识位
    head_ok = raw[0] == 0x5B if raw else False
    tail_ok = raw[-1] == 0x5D if raw else False
    r["头标识"] = f"0x{raw[0]:02X}" if raw else "N/A"
    r["尾标识"] = f"0x{raw[-1]:02X}" if raw else "N/A"
    r["标识位正确"] = bool(head_ok and tail_ok)

    # 2) 转义检查（内容里出现 5A/5E 才需要逆转义）
    inner = raw[1:-1] if len(raw) > 2 else b""
    has_esc = any(p in (0x5A, 0x5E) for p in inner)
    if has_esc:
        un = unescape(raw)
        if un != raw:
            raw = un
            r["unescape"] = True
            inner = raw[1:-1]

    # 3) CRC：按规范「从数据头到校验码前」，即尾标识前 2 字节
    crc_field = int.from_bytes(raw[-3:-1], "big") if len(raw) >= 4 else None
    r["CRC字段"] = f"0x{crc_field:04X}" if crc_field is not None else "N/A"
    hits = find_crc_ranges(raw, crc_field) if crc_field is not None else []
    r["CRC命中区间"] = hits
    r["CRC自洽"] = bool(hits)

    # 4) 内容 = 头标识之后 ~ CRC 之前
    content_end = len(raw) - 3            # 尾标识(1) + CRC(2)
    content = raw[1:content_end]
    # 若暴力搜索结果里有更贴合规范的区间，用它
    if len(hits) == 1:
        content = raw[hits[0]["start"]:hits[0]["end"]]
    r["数据头+数据体字节数"] = len(content)
    r["数据头应为"] = header_len
    r["差值"] = len(content) - header_len      # <0 → 连数据头都不够
    r["数据体字节数"] = len(content) - header_len

    # 5) 按表7 解数据头（大端）
    fields, off = {}, 0
    body = content[header_len:]
    for name, size, desc in HEADER_FIELDS:
        chunk = content[off:off + size]
        val = None
        if len(chunk) == size:
            if name == "VERSION_FLAG":
                val = f"V{chunk[0]}.{chunk[1]}.{chunk[2]}  (raw {chunk.hex().upper()})"
            elif name == "TIME":
                secs = int.from_bytes(chunk, "big")
                val = f"{secs} 秒 (0 = 未填)" if secs else "0（未填）"
            else:
                val = int.from_bytes(chunk, "big")
        fields[name] = {"bytes": chunk.hex().upper(), "len": len(chunk), "value": val, "desc": desc}
        off += size
    r["数据头"] = fields

    mid = fields.get("MSG_ID", {}).get("value")
    if isinstance(mid, int):
        r["MSG_ID名称"] = MSG_NAMES.get(mid, "（未收录）")
        r["消息方向提示"] = (
            "上级→下级" if 0x9000 <= mid < 0xA000 or 0x1000 <= mid < 0x1100 or 0x9300 <= mid else
            "下级→上级" if 0x1100 <= mid < 0x9000 else "未知")
    r["数据体hex"] = body.hex().upper()
    if body and body[0] in RESULT_CODES and isinstance(mid, int) and mid in (
            0x1002, 0x1004, 0x1006, 0x9002, 0x9004, 0x9006):
        r["RESULT"] = f"0x{body[0]:02X} = {RESULT_CODES[body[0]]}"
    return r


def main():
    ap = argparse.ArgumentParser(description="JT/T 809 / 川标 0003.4 报文帧解码器")
    ap.add_argument("hex", nargs="?", help="报文 hex（可带 0x、空格、换行）")
    ap.add_argument("--file", help="从文件读 hex（一行或整文件）")
    ap.add_argument("--header-len", type=int, default=HEADER_LEN,
                    help=f"假定的数据头字节数（默认 {HEADER_LEN}）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args()

    text = open(a.file, encoding="utf-8", errors="ignore").read() if a.file else (a.hex or "")
    if not text:
        text = sys.stdin.read()
    if not text.strip():
        ap.error("没有输入：给 hex 参数、--file 或从标准输入喂")

    r = decode(text, a.header_len)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("CRC自洽") else 1

    print("=" * 68)
    print(f"总字节数          : {r['输入字节数']}")
    print(f"头标识 / 尾标识   : {r['头标识']} / {r['尾标识']}   {'✔' if r['标识位正确'] else '✘ 不符 0x5B/0x5D'}")
    if r["unescape"]:
        print("转义              : 已逆转义（内容含 5A/5E 序列）")
    print(f"CRC 字段          : {r['CRC字段']}")
    print(f"CRC 命中区间      : {r['CRC命中区间']}")
    print(f"CRC 自洽          : {'✔ 帧完整、未传输截断' if r['CRC自洽'] else '✘ 校验不过（可能丢字节/转义未处理）'}")
    print("-" * 68)
    print(f"数据头+数据体     : {r['数据头+数据体字节数']} 字节")
    print(f"标准数据头        : {r['数据头应为']} 字节  (4+4+2+4+3+4+8)")
    d = r["差值"]
    if d < 0:
        print(f"差值              : {d} 字节  ⚠ 连数据头都不够 → 解析必然越界")
    elif d == 0:
        print(f"差值              : 0 字节  ⚠ 数据体为空（应答类消息本该有 RESULT）")
    else:
        print(f"差值              : +{d} 字节（数据体 {r['数据体字节数']} 字节）")
    print("-" * 68)
    for name, info in r["数据头"].items():
        flag = "" if info["len"] == dict((n, s) for n, s, _ in HEADER_FIELDS)[name] else "  ⚠字段被截断"
        print(f"  {name:<17} {info['bytes']:<18} {info['value']}{flag}")
    print("-" * 68)
    print(f"MSG_ID 名称       : {r.get('MSG_ID名称', '-')}   ({r.get('消息方向提示', '-')})")
    print(f"数据体 hex        : {r['数据体hex'] or '(空)'}")
    if "RESULT" in r:
        print(f"RESULT            : {r['RESULT']}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
