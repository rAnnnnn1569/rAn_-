---
name: platform-video-no-moov-repair
description: 诊断并修复「监控平台 / 车联网平台导出的 mp4 打不开」。当用户说「平台导出的视频无法播放」「这个 mp4 播不了 / 提示文件损坏 / 格式不支持」「下载的录像打不开，但文件有好几百 MB」「视频只有几秒/几分钟，比导出时段短很多」时使用。适用于导出文件缺 moov（只有 ftyp+free+mdat）、mdat 长度字段为 0、播放器报「无法渲染/不支持」等场景；覆盖 JT/T 1078 平台录像、行车记录仪导出、DVR/ADAS 平台下载件。产出可播放的 mp4。
agent_created: true
---

# 平台导出 mp4 打不开 —— 缺 moov 的诊断与修复

## 何时用

- 平台导出的 `.mp4` 双击打不开、播放器报「无法播放 / 格式不支持 / 文件损坏」
- 文件很大（几百 MB）但播不动；或能播却只有几秒
- 文件名形如 `<车牌>_<通道>_<起>_<止>.mp4`（车联网/1078 录像）

## 核心结论（先记住）

**MP4 的 `moov` 写在文件末尾。平台导出 = 先流式写 `ftyp + free + mdat`，最后才回填 `moov`。**
导出/下载被中断时，`mdat` 的 size 字段停在 **0**、末尾是半截数据、`moov` 整个丢失
→ 播放器拿不到索引，必然打不开。
**同一次截断会同时造成两个症状**：① 播不了；② 内容比标称时段短。

## 步骤

### 1. 结构诊断（确诊）

```python
import struct
data = open(PATH, "rb").read()
off = 0
while off + 8 <= len(data):
    size = struct.unpack(">I", data[off:off+4])[0]
    typ  = data[off+4:off+8].decode("latin-1")
    print(off, size, typ)
    if size < 8:            # size=0 表示"到文件末尾"，box 链在此断裂
        print("!!! box 链断裂，此处 size =", size)
        break
    off += size
# 全文件确认 moov 是否存在（关键！只扫开头会误判）
for tag in (b"moov", b"mvhd", b"trak", b"stsd", b"avcC", b"stsz", b"stco", b"ftyp", b"mdat"):
    print(tag, data.count(tag))
```

判据：`ftyp/free/mdat` 各 1 个、`mdat` size=0、**`moov` 数量 = 0** → 确诊缺 moov。
若 `moov` 存在，本 skill 不适用（那是别的问题）。

### 2. 载荷诊断（确认数据可用）

`mdat` 载荷起点 = `mdat box 起点 + 8`。按 **AVC 4 字节大端长度前缀**走 NAL 链：

```python
i = mdat_payload_off
while i + 5 < len(data):
    L = int.from_bytes(data[i:i+4], "big")
    T = data[i+4] & 0x1F          # 7=SPS 8=PPS 5=IDR 1=P非IDR 6=SEI
    if not (0 < L <= 400000 and T in (1,5,6,7,8,9)):
        break                     # 失步
    i += 4 + L
```

看到 `SPS(7) PPS(8) SEI(6) IDR(5)` 依次出现 → 数据完好，可修。

### 3. 定帧率（SPS VUI，权威）

去掉 emulation prevention（`00 00 03` → `00 00`，跳过 NAL 头字节）后按位解析：
`fps = time_scale / (2 * num_units_in_tick)`。
⚠️ **不要用 `ffmpeg -i xx.h264` 报的 fps** —— 裸流模式下它会默认报 25，不可信。

### 4. 🔑 定真实时长（最有说服力的一步）

车联网录像一般把时间戳烧在画面上。抽首/末帧直接看图：

```
ffmpeg -y -f h264 -i out.h264 -vf "select=eq(n\,0)+eq(n\,<末帧号>)" \
       -fps_mode passthrough -f image2 fr_%02d.png
```

读画面上的时间戳。**先用 2~3 个候选帧率反推时长、再和 OSD 对齐**，比任何结构推断都硬。
（本项目实测：首帧 08:39:59 / 末帧 08:48:22，相差 503s，9269 帧 ÷ 503s ≈ 18.4fps，
补回丢帧后 ≈20fps，与 SPS 声明吻合 → 证明文件只含 8 分 23 秒，而文件名标称 20 分钟。）

### 5. 修复

```
python scripts/fix_no_moov.py "<broken.mp4>" "<输出目录>"
# 得到 out.h264（Annex-B），再重封装：
ffmpeg -y -f h264 -r <fps> -i out.h264 -c:v copy -movflags +faststart fixed.mp4
ffmpeg -i fixed.mp4        # 校验：应显示 mov,mp4,m4a... + Duration + Stream
```

再抽一帧回来确认能解码，即完成闭环。

## 坑

1. **只扫文件开头会误判**：moov 在末尾，必须全文件 `count()`。
2. **`mdat` size=0 不是"空"**，是 ISO 里"持续到文件末尾"的历史约定，别当无效。
3. **裸流 fps 不可信**（见步骤 3），必须解 SPS VUI 或用 OSD 校。
4. 🔴 **"丢失量"取决于重同步口径，别高估**：必须重同步到**下一个合法 NAL**（判据：4 字节长度合法 + 其后紧邻的 NAL 也合法），**不要跳到下一个关键帧**——跳到关键帧会把中间大量正常 P 帧一并算成"丢失"。本项目实测差 20 倍：粗口径（跳关键帧）12.19% vs 细口径 0.56%。典型形态＝每 ~370KB 一处接缝、每处仅 ~2KB。
5. **丢帧会让播放时间轴比真实时间快**（本例约 8%）：帧号 ÷ fps ≠ 真实经过时间，别据此下结论。🔑 **判定"是否真丢帧"的硬指标**：`总帧数 ÷ OSD 实测跨度秒数` = **实际帧率**；若它低于 SPS 声明帧率，就是有丢帧。再用 `SPS/PPS/IDR 计数` 得 GOP 数（本例各 503，1 秒 1 个 GOP），可算出"每 GOP 少几帧"。
6. **别急着判"文件损坏"**：数据常在，只是索引丢了。
7. `Lavc58.35.100`、`x264 - core ...` 这类字符串 = 编码器写进 **SEI** 的版本标记，可用于判断"是否经 FFmpeg 转码"。
8. 修复品**只供应急查看**；根治要回平台重新导出，并排查为什么导出会中断（超时/带宽/存储）。
9. 🔑 **数据流可能是"AVCC + Annex-B 混写"**：mdat 里既有 4 字节长度前缀、又夹着 `00 00 00 01` 起始码（本项目 455 处）。**判据**：`data.count(b"\x00\x00\x00\x01")` 若远多于随机期望（≈ `len / 2^32`）→ 是真起始码；把它们的偏移与失步点比对，**若一一对应，则接缝根因＝两种封装混写，不是数据空洞**（接缝区是高熵数据、256 种字节齐全即是铁证——压缩流里出现 16 个连续 `0x00` 的概率约 2⁻¹²⁸，不可能偶然）。
10. **`free` box 里有大段 0x00 属正常**（本项目 offset 40 处 132 个 0）；不要把"连续零段"一律当成损坏，要看它落在 box 结构里还是落在 mdat 载荷里。

## 环境

- ffmpeg 无需装到系统：`pip install imageio-ffmpeg` → `imageio_ffmpeg.get_ffmpeg_exe()`（约 87MB 静态版，隔离环境即可）。
- 纯 Python 只做 AVCC→Annex-B 拆分，**不需要** openpyxl/其它依赖。
