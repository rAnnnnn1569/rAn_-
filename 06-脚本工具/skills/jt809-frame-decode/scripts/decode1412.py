# -*- coding: utf-8 -*-
"""部省「上报车辆报警处理结果」帧（子业务类型 0x1412，部省规范附录表 A.2）解析器。

用法:
  python decode1412.py "<hex>" [out.txt]
  # hex 可带 0x 前缀；建议先剥掉帧尾 CRC/5D 只给"有效前缀"，
  # 本脚本会把 SPECIFICATION(500B) 自动补零到定长，便于残缺/截断 hex 也能解析。

要点:
  * 数据体起点 = 含帧头 5B 偏移 31（= 部省数据头 30B，比 JT/T809-2019 默认 29B 多 1B）
  * 子业务类型在**数据体第 1 个字段** DATA_TYPE(2B)
  * RESULT: 0x01 已处理完毕 / 0x02 不作处理 / 0x04 误报警 = 最终状态；
            0x00 处理中 / 0x03 将来处理 = 原文明确「视为未处理」
  * 0x1412 **无报警状态/结束时间字段** → 上级判「已处理/未处理」只认本帧是否收到 + ALARM_ID 是否匹配
"""
import sys, datetime, binascii


def unescape(b):
    out = bytearray(); i = 0
    while i < len(b):
        c = b[i]
        if c == 0x5A and i + 1 < len(b) and b[i + 1] in (0x01, 0x02):
            out.append(0x5A if b[i + 1] == 0x01 else 0x5B); i += 2
        elif c == 0x5E and i + 1 < len(b) and b[i + 1] in (0x01, 0x02):
            out.append(0x5D if b[i + 1] == 0x01 else 0x5E); i += 2
        else:
            out.append(c); i += 1
    return bytes(out)


def ts(v):
    if not v:
        return "0 (未填)"
    u = datetime.datetime.fromtimestamp(v, datetime.UTC)
    c = datetime.datetime.fromtimestamp(v + 8 * 3600, datetime.UTC)
    return "UTC %s | 北京 %s | 秒=%d" % (u.strftime("%Y-%m-%d %H:%M:%S"),
                                     c.strftime("%Y-%m-%d %H:%M:%S"), v)


def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    hx = sys.argv[1].strip()
    if hx.lower().startswith("0x"):
        hx = hx[2:]
    out = sys.stdout
    if len(sys.argv) > 2:
        out = open(sys.argv[2], "w", encoding="utf-8")
    P = lambda *a: print(*a, file=out)

    if len(hx) % 2:
        P("!! hex 奇数长度（%d），末位或中间疑有丢字，请核对原文" % len(hx)); return
    p = unescape(binascii.unhexlify(hx))
    P("有效前缀 unescaped 长度 = %d 字节；首字节 = 0x%02X" % (len(p), p[0]))

    start = next((i for i in range(9, 40) if p[i:i + 2] == b"\x14\x12"), None)
    if start is None:
        P("!! 未定位到数据体起点（偏移 9~39 内无 0x1412）"); return
    P("数据头(含5B) = %d 字节   hex=%s" % (start, p[:start].hex().upper()))
    body = p[start:]
    if len(body) > 573:
        P("!! 前缀超长，截断到表 A.2 表长 573"); body = body[:573]
    body += b"\x00" * (573 - len(body))

    P("MSG_ID            = 0x%04X" % int.from_bytes(p[9:11], "big"))
    P("DATA_TYPE         = 0x%04X  (0x1412=上报车辆报警处理结果)" % int.from_bytes(body[0:2], "big"))
    P("DATA_LENGTH       = %d  (应=567)" % int.from_bytes(body[2:6], "big"))
    P("SOURCE_DATA_TYPE  = 0x%04X  (应=0x1402)" % int.from_bytes(body[6:8], "big"))
    pos = 8
    vno = body[pos:pos + 21].split(b"\x00")[0].decode("gbk", "replace"); pos += 21
    color = body[pos]; pos += 1
    aid = body[pos:pos + 34].decode("ascii", "replace"); pos += 34
    result = body[pos]; pos += 1
    deal = int.from_bytes(body[pos:pos + 8], "big"); pos += 8
    spec = body[pos:pos + 500].split(b"\x00")[0].decode("gbk", "replace")
    P("VEHICLE_NO        = %s" % vno)
    P("VEHICLE_COLOR     = 0x%02X %s" % (color, {1: "蓝", 2: "黄", 3: "黑", 4: "白", 5: "绿", 6: "黄绿", 9: "其它"}.get(color, "?")))
    P("ALARM_ID          = %s  (len=%d)" % (aid, len(aid)))
    P("RESULT            = 0x%02X  %s" % (result, {0: "处理中(原文视为未处理)", 1: "已处理完毕", 2: "不作处理", 3: "将来处理(原文视为未处理)", 4: "误报警"}.get(result, "非法")))
    P("DEAL_TIME         = %s" % ts(deal))
    P("SPECIFICATION     = %s" % spec)
    q = aid.split("_")
    if len(q) == 4 and q[3].isdigit():
        P("→ 省码=%s 终端手机号=%s 报警类型=0x%s 开始时间=%s" % (q[0], q[1], q[2], ts(int(q[3]))))
    if out is not sys.stdout:
        out.close()


if __name__ == "__main__":
    main()
