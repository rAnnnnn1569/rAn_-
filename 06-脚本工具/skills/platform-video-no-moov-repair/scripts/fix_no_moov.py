#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复「缺 moov」的平台导出 mp4：抽出 mdat 里的 AVC 流 -> Annex-B .h264

用法:
    python fix_no_moov.py <broken.mp4> [输出目录]

输出:
    <输出目录>/<name>.h264       —— 再用 ffmpeg 重封装：
        ffmpeg -y -f h264 -r <fps> -i <name>.h264 -c:v copy -movflags +faststart fixed.mp4
    <输出目录>/<name>.report.txt —— 诊断报告（box 链 / SPS / 帧数 / 丢帧）
"""
import os
import struct
import sys
from collections import Counter

VALID = {1, 5, 6, 7, 8, 9}
NAL_NAME = {1: "非IDR片", 5: "IDR片", 6: "SEI", 7: "SPS", 8: "PPS", 9: "AUD"}
MAXNAL = 400000


# ---------- 1. box 链 ----------
def scan_boxes(data, limit=32):
    """返回 [(off, size, type)]，遇到 size<8 即停（链断裂）"""
    out = []
    off = 0
    while off + 8 <= len(data) and len(out) < limit:
        size = struct.unpack(">I", data[off:off + 4])[0]
        typ = data[off + 4:off + 8].decode("latin-1", "replace")
        out.append((off, size, typ))
        if size < 8:
            break
        off += size
    return out


def find_mdat_payload(data):
    """定位 mdat 载荷起点；链断了就退回全文搜索"""
    for off, size, typ in scan_boxes(data):
        if typ == "mdat":
            return off, off + 8
    i = data.find(b"mdat", 0, 1 << 20)
    if i > 0:
        return i - 4, i + 4
    return None, None


# ---------- 2. SPS -> fps ----------
class _BR:
    def __init__(self, b):
        self.b, self.pos = b, 0

    def bit(self):
        if self.pos >= len(self.b) * 8:
            raise EOFError
        v = (self.b[self.pos // 8] >> (7 - self.pos % 8)) & 1
        self.pos += 1
        return v

    def u(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v

    def ue(self):
        z = 0
        while self.bit() == 0:
            z += 1
        return (1 << z) - 1 + (self.u(z) if z else 0)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)


def _unescape(nal):                      # 去 NAL 头 + 去 emulation prevention
    body = nal[1:]
    out = bytearray()
    i = 0
    while i < len(body):
        if i + 2 < len(body) and body[i] == 0 and body[i + 1] == 0 and body[i + 2] == 3:
            out += body[i:i + 2]
            i += 3
        else:
            out.append(body[i])
            i += 1
    return bytes(out)


def parse_sps(nal):
    r = _BR(_unescape(nal))
    d = {"profile_idc": r.u(8)}
    r.u(8)
    d["level_idc"] = r.u(8)
    r.ue()
    if d["profile_idc"] in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134):
        if r.ue() == 3:
            r.u(1)
        r.ue()
        r.ue()
        r.u(1)
        if r.u(1):
            raise NotImplementedError("scaling matrix")
    r.ue()                                    # log2_max_frame_num_minus4
    poc = r.ue()
    if poc == 0:
        r.ue()
    elif poc == 1:
        r.u(1)
        r.se()
        r.se()
        for _ in range(r.ue()):
            r.se()
    r.ue()                                    # max_num_ref_frames
    r.u(1)
    pw, ph = r.ue(), r.ue()
    fmbf = r.u(1)
    if not fmbf:
        r.u(1)
    r.u(1)
    if r.u(1):
        r.ue(); r.ue(); r.ue(); r.ue()
    d["width"] = (pw + 1) * 16
    d["height"] = (ph + 1) * 16 * (2 - fmbf)
    d["fps"] = None
    if r.u(1):                                # vui
        if r.u(1) and r.u(8) == 255:
            r.u(16); r.u(16)
        if r.u(1):
            r.u(1)
        if r.u(1):
            r.u(3); r.u(1)
            if r.u(1):
                r.u(8); r.u(8); r.u(8)
        if r.u(1):
            r.ue(); r.ue()
        if r.u(1):                            # timing_info
            nut, ts = r.u(32), r.u(32)
            if nut:
                d["fps"] = ts / (2.0 * nut)
    return d


# ---------- 3. 抽流（带断点重同步） ----------
def extract(data, pay, dst):
    n = len(data)

    def nalu_at(j):
        if j + 5 > n:
            return None
        L = int.from_bytes(data[j:j + 4], "big")
        if not (0 < L <= MAXNAL):
            return None
        T = data[j + 4] & 0x1F
        return (L, T) if T in VALID else None

    def chain(j, maxn=4):
        c = 0
        while c < maxn:
            r = nalu_at(j)
            if not r:
                break
            j += 4 + r[0]
            c += 1
        return c

    i, emitted, frames = pay, 0, 0
    cnt, losses, sps = Counter(), [], None
    with open(dst, "wb") as fo:
        while i < n - 5:
            r = nalu_at(i)
            if r and chain(i) >= 3:
                L, T = r
                fo.write(b"\x00\x00\x00\x01")
                fo.write(data[i + 4:i + 4 + L])
                emitted += L + 4
                cnt[T] += 1
                if T in (1, 5):
                    frames += 1
                if T == 7 and sps is None:
                    sps = data[i + 4:i + 4 + L]
                i += 4 + L
            else:
                j, found, end = i + 1, -1, min(n - 5, i + 200000)
                while j < end:
                    rr = nalu_at(j)
                    if rr and chain(j) >= 3:
                        found = j
                        break
                    j += 1
                if found < 0:
                    losses.append((i, n))
                    break
                losses.append((i, found))
                i = found
    return emitted, frames, cnt, losses, sps


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = sys.argv[1]
    outdir = sys.argv[2] if len(sys.argv) > 2 else os.path.dirname(os.path.abspath(src))
    os.makedirs(outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src))[0]
    dst = os.path.join(outdir, base + ".h264")
    rep_path = os.path.join(outdir, base + ".report.txt")

    data = open(src, "rb").read()
    R = []
    R.append("源文件: %s" % src)
    R.append("大小: %d 字节 (%.2f MB)" % (len(data), len(data) / 1048576.0))
    R.append("")
    R.append("--- box 链 ---")
    for off, size, typ in scan_boxes(data):
        R.append("  @%-10d size=%-12d type=%s" % (off, size, typ))
    R.append("")
    R.append("--- 关键 box 全文件计数 ---")
    for tag in (b"ftyp", b"moov", b"mvhd", b"trak", b"stsd", b"avcC", b"stsz", b"stco", b"mdat"):
        R.append("  %-6s %d" % (tag.decode(), data.count(tag)))
    if data.count(b"moov") == 0:
        R.append("  >>> 确认缺 moov：播放器无索引，必然打不开（符合本工具适用场景）")
    else:
        R.append("  >>> 发现 moov，本场景不符，请另查（可能是编码/封装参数问题）")

    moff, pay = find_mdat_payload(data)
    R.append("")
    R.append("mdat box @%s，载荷起点 %s" % (moff, pay))
    if pay is None:
        R.append("找不到 mdat，终止")
        open(rep_path, "w", encoding="utf-8").write("\n".join(R))
        return 2

    emitted, frames, cnt, losses, sps = extract(data, pay, dst)
    total = len(data) - pay
    R.append("写出 %s（%d 字节）" % (dst, os.path.getsize(dst)))
    R.append("覆盖率 %.2f%%（%d/%d 字节）" % (100.0 * emitted / total, emitted, total))
    R.append("图像数 %d  失步点 %d 处  累计丢失 %d 字节(%.2f%%)"
             % (frames, len(losses),
                sum(b - a for a, b in losses), 100.0 * sum(b - a for a, b in losses) / total))
    R.append("")
    R.append("--- NAL 统计 ---")
    for t, c in cnt.most_common():
        R.append("  type %-2d %-8s × %d" % (t, NAL_NAME.get(t, "?"), c))
    R.append("")
    R.append("--- 断点前 10 ---")
    for a, b in losses[:10]:
        R.append("  %d..%d (%d 字节)" % (a, b, b - a))

    fps = None
    if sps:
        try:
            d = parse_sps(sps)
            fps = d.get("fps")
            R.append("")
            R.append("--- SPS ---")
            R.append("  %s" % sps.hex())
            R.append("  分辨率 %sx%s   profile=%s   fps=%s"
                     % (d.get("width"), d.get("height"), d.get("profile_idc"), fps))
        except Exception as e:
            R.append("  SPS 解析失败: %r" % (e,))

    R.append("")
    R.append("--- 下一步 ---")
    R.append("  ffmpeg -y -f h264 -r %s -i \"%s\" -c:v copy -movflags +faststart \"%s\""
             % (fps or 25, dst, os.path.join(outdir, base + "_fixed.mp4")))
    R.append("  # 再用画面 OSD 时间戳核对真实时长/帧率：")
    R.append("  ffmpeg -y -f h264 -i \"%s\" -vf \"select=eq(n\\,0)+eq(n\\,%d)\" "
             "-fps_mode passthrough -f image2 fr_%%02d.png" % (dst, max(0, frames - 1)))
    R.append("")
    R.append("  预估：按 %.3f fps 计，时长约 %.1f 秒 (%.1f 分钟)"
             % (fps or 25, frames / (fps or 25), frames / (fps or 25) / 60))

    open(rep_path, "w", encoding="utf-8").write("\n".join(R))
    print("\n".join(R))
    return 0


if __name__ == "__main__":
    sys.exit(main())
