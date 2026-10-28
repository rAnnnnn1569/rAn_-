# -*- coding: utf-8 -*-
"""
JT/T 808 报文工具：给「裸消息体」补全消息头 → 输出完整帧；并可解析 0x0200 消息体。

用法：
  python build808.py --body <hex> --parse                      # 只解析消息体
  python build808.py --body <hex> --sim 13800138000 --seq 1    # 补头，输出完整帧
  python build808.py --body <hex> --sim 13800138000 --seq 1 --msgid 0200 --out frame.txt

要点：
  * 消息头 = 消息ID(2) + 消息体属性(2) + 终端手机号(6, BCD) + 消息流水号(2)  共 12 字节
  * 消息体属性 = 消息体长度(bit0-9) | 加密方式(bit10-12) | 分包(bit13)
  * 校验码 = 从消息ID到消息体末尾 全部字节逐字节异或(XOR)
  * 转义：0x7E -> 0x7D 0x02 ; 0x7D -> 0x7D 0x01 （作用范围=消息头+消息体+校验码）
"""
import argparse
import sys


# ---------- 基础 ----------
def xor_checksum(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
    return c


def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == 0x7E:
            out += b"\x7d\x02"
        elif b == 0x7D:
            out += b"\x7d\x01"
        else:
            out.append(b)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == 0x7D and i + 1 < len(data):
            out.append(0x7E if data[i + 1] == 0x02 else 0x7D)
            i += 2
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def sim_to_bcd(sim: str) -> bytes:
    s = "".join(ch for ch in sim if ch.isdigit())
    if len(s) == 11:
        s = "0" + s          # 11 位手机号左补 0 → 12 位 BCD
    if len(s) != 12:
        raise ValueError("终端手机号需为 11 位或 12 位数字，当前=%r" % sim)
    return bytes((int(s[i]) << 4) | int(s[i + 1]) for i in range(0, 12, 2))


def build_frame(body: bytes, msgid: int = 0x0200, sim_bcd: bytes = b"\x00" * 6,
                seq: int = 1, encrypt: int = 0, subpack: int = 0) -> bytes:
    attr = (len(body) & 0x03FF) | ((encrypt & 0x07) << 10) | ((subpack & 0x01) << 13)
    header = msgid.to_bytes(2, "big") + attr.to_bytes(2, "big") + sim_bcd + seq.to_bytes(2, "big")
    payload = header + body
    return b"\x7e" + escape(payload + bytes([xor_checksum(payload)])) + b"\x7e"


# ---------- 0x0200 消息体解析 ----------
AV_TABLE = {
    0x01: ("里程", "1/10km"), 0x02: ("油量", "1/10L"), 0x03: ("行驶记录仪速度", "1/10km/h"),
    0x04: ("需人工确认报警事件ID", ""), 0x05: ("胎压", "Pa"), 0x06: ("车厢温度", "℃"),
    0x11: ("超速报警附加信息", ""), 0x12: ("进出区域/路线报警附加信息", ""),
    0x13: ("路段行驶时间不足/过长报警附加信息", ""), 0x14: ("视频相关报警(按位)", ""),
    0x15: ("视频信号丢失(按位,通道1-32)", ""), 0x16: ("视频信号遮挡(按位,通道1-32)", ""),
    0x17: ("存储器故障(bit0-11主存/bit12-15灾备)", ""), 0x18: ("异常驾驶行为报警详细描述", ""),
    0x19: ("保留", ""), 0x25: ("扩展车辆信号状态位(按位)", ""), 0x2A: ("IO状态位(按位)", ""),
    0x2B: ("模拟量 AD0|AD1", ""), 0x30: ("无线通信网络信号强度", ""), 0x31: ("GNSS定位卫星数", "颗"),
    0x32: ("车辆基础限速值", "km/h"), 0x33: ("当前道路路网信息", ""),
    0x64: ("驾驶辅助功能报警", ""), 0x65: ("驾驶员行为监测报警", ""), 0x66: ("轮胎状态监测", ""),
    0x67: ("盲区监测报警", ""), 0x70: ("激烈驾驶报警", ""), 0x71: ("卫星定位系统", ""),
    0x72: ("载重报警", ""), 0x73: ("限高报警", ""), 0x80: ("限速值信息", ""),
    0x81: ("超速行为报警信息", ""), 0x83: ("主动安全防控事件附加信息", ""),
    0xA1: ("定位消息流水号", ""), 0xA2: ("终端手机号(BCD[10])", ""),
}
STATUS_BITS = {
    0: "ACC(1=开)", 1: "定位(1=有效)", 2: "南纬", 3: "西经", 4: "停运",
    5: "经纬度加密", 6: "前撞预警", 7: "车道偏移预警", 10: "油路断开", 11: "电路断开",
    12: "车门加锁", 13: "门1开(前)", 14: "门2开(中)", 15: "门3开(后)", 16: "门4开(驾驶席)",
    17: "门5开(自定义)", 18: "使用GPS定位", 19: "使用北斗定位", 20: "使用GLONASS定位",
    21: "使用Galileo定位", 22: "车辆行驶中",
}
ALARM_BITS = {
    0: "紧急报警", 1: "超速报警", 2: "疲劳驾驶", 3: "危险驾驶行为", 4: "GNSS模块故障",
    5: "GNSS天线未接/被剪断", 6: "GNSS天线短路", 7: "终端主电源欠压", 8: "终端主电源掉电",
}


def bits(v, table):
    on = [("%d:%s" % (b, n)) for b, n in sorted(table.items()) if v >> b & 1]
    return "、".join(on) if on else "无"


def parse_0200(body: bytes):
    if len(body) < 28:
        raise ValueError("0x0200 消息体至少 28 字节，当前 %d" % len(body))
    alarm = int.from_bytes(body[0:4], "big")
    status = int.from_bytes(body[4:8], "big")
    lat = int.from_bytes(body[8:12], "big") / 1e6
    lon = int.from_bytes(body[12:16], "big") / 1e6
    alt = int.from_bytes(body[16:18], "big")
    speed = int.from_bytes(body[18:20], "big") / 10.0
    direction = int.from_bytes(body[20:22], "big")
    t = body[22:28]
    ts = "20%02x-%02x-%02x %02x:%02x:%02x" % tuple(t)
    print("=== 位置基本信息（28 字节）===")
    print("报警标志 : 0x%08X  -> %s" % (alarm, bits(alarm, ALARM_BITS)))
    print("状态     : 0x%08X  -> %s" % (status, bits(status, STATUS_BITS)))
    print("纬度     : %.6f  %s" % (abs(lat), "南纬" if status >> 2 & 1 else "北纬"))
    print("经度     : %.6f  %s" % (abs(lon), "西经" if status >> 3 & 1 else "东经"))
    print("高程     : %d m" % alt)
    print("速度     : %.1f km/h" % speed)
    print("方向     : %d 度" % direction)
    print("时间     : %s (BCD)" % ts)
    print()
    print("=== 位置附加信息项列表 ===")
    i, n = 28, 0
    while i < len(body):
        if i + 2 > len(body):
            print("!! 尾部残缺：偏移 %d 处不足 ID+长度" % i)
            break
        aid, ln = body[i], body[i + 1]
        val = body[i + 2:i + 2 + ln]
        if len(val) != ln:
            print("!! 0x%02X 声明长度 %d，实际只剩 %d 字节 → 截断" % (aid, ln, len(val)))
            break
        name, unit = AV_TABLE.get(aid, ("未定义/厂商自定义", ""))
        n += 1
        if ln <= 4:
            hv = "0x" + val.hex().upper() + " (=%d)" % int.from_bytes(val, "big")
            if unit == "1/10km":
                hv += " -> %.1f km" % (int.from_bytes(val, "big") / 10.0)
            elif unit == "1/10L":
                hv += " -> %.1f L" % (int.from_bytes(val, "big") / 10.0)
            elif unit == "1/10km/h":
                hv += " -> %.1f km/h" % (int.from_bytes(val, "big") / 10.0)
        else:
            hv = "%d 字节, 0x%s(截断显示)" % (ln, val[:8].hex().upper())
        print("0x%02X len=%-2d  %-34s %s %s" % (aid, ln, name, hv, unit))
        if aid == 0x25 and ln == 4:
            print("          └ 位解析: %s" % bits(int.from_bytes(val, "big"),
                  {0: "近光灯", 1: "远光灯", 2: "右转向灯", 3: "左转向灯", 4: "制动", 5: "倒档",
                   6: "雾灯", 7: "示廓灯", 8: "喇叭", 9: "空调", 10: "空挡", 11: "缓速器",
                   12: "ABS", 13: "加热器", 14: "离合器"}))
        if aid == 0x2B and ln == 4:
            v = int.from_bytes(val, "big")
            print("          └ AD0=%d  AD1=%d" % (v & 0xFFFF, v >> 16))
        if aid == 0x14 and ln == 4:
            print("          └ 位解析: %s" % bits(int.from_bytes(val, "big"),
                  {0: "视频信号丢失", 1: "主存储器故障", 2: "灾备存储单元故障", 3: "其他视频设备故障",
                   4: "客车超载", 5: "异常驾驶行为", 6: "特殊报警录像达存储阈值", 29: "图像移动侦测",
                   30: "急弯/S弯", 31: "激烈颠簸"}))
        i += 2 + ln
    print()
    print("附加信息合计 %d 项 / %d 字节；消息体总长 %d 字节" % (n, len(body) - 28, len(body)))
    if i != len(body):
        print("!! 终止位置 %d != 消息体长度 %d —— 解析未对齐" % (i, len(body)))
    else:
        print(">> TLV 链恰好对齐到消息体末尾，结构自洽")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--body", required=True, help="消息体 hex（不含消息头/校验/7E）")
    ap.add_argument("--sim", default=None, help="终端手机号（11/12 位数字）")
    ap.add_argument("--seq", type=int, default=1, help="消息流水号")
    ap.add_argument("--msgid", default="0200", help="消息ID，默认 0200")
    ap.add_argument("--parse", action="store_true", help="解析消息体")
    a = ap.parse_args()

    body = bytes.fromhex(a.body.replace(" ", "").replace("\n", ""))
    print("输入消息体：%d 字节" % len(body))
    print()

    msgid = int(a.msgid, 16)

    if a.parse:
        if msgid == 0x0200:
            parse_0200(body)
        else:
            print("（--parse 目前只实现 0x0200）")
        print()

    if a.sim:
        sim_bcd = sim_to_bcd(a.sim)
        frame = build_frame(body, msgid=msgid, sim_bcd=sim_bcd, seq=a.seq)
        attr = (len(body) & 0x03FF)
        header = msgid.to_bytes(2, "big") + attr.to_bytes(2, "big") + sim_bcd + a.seq.to_bytes(2, "big")
        cs = xor_checksum(header + body)
        print("=== 补全消息头 ===")
        print("消息ID     : 0x%04X" % msgid)
        print("消息体属性 : 0x%04X  (长度 %d 字节, 不加密, 不分包)" % (attr, len(body)))
        print("终端手机号 : %s  -> BCD %s" % (a.sim, sim_bcd.hex().upper()))
        print("消息流水号 : 0x%04X" % a.seq)
        print("校验码     : 0x%02X  (消息ID..消息体 逐字节异或)" % cs)
        print()
        print("=== 完整报文（7E ... 7E）===")
        print(frame.hex().upper())
        print()
        print("长度 %d 字节（含首尾 7E，已转义）" % len(frame))
        # 自检：反转义后 XOR 校验
        inner = unescape(frame[1:-1])
        chk = xor_checksum(inner[:-1])
        print()
        print("=== 自检 ===")
        print("反转义后 %d 字节；重算校验码 0x%02X vs 帧内 0x%02X -> %s"
              % (len(inner), chk, inner[-1], "一致" if chk == inner[-1] else "不一致!!"))
        print("XOR(消息ID..校验码) = 0x%02X -> %s"
              % (xor_checksum(inner), "通过" if xor_checksum(inner) == 0 else "不通过!!"))


if __name__ == "__main__":
    main()
