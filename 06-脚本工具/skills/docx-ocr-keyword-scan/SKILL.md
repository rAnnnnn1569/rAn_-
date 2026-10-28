---
name: docx-ocr-keyword-scan
description: 在 .docx 里做「全通道」关键词检索——文本层（正文/域代码/页眉页脚/文档属性/超链接/嵌套OLE对象）**加图片 OCR**，用来判定某个域名、IP、编号、敏感词到底有没有出现在文档里（包括截图里）。当用户说「全文通读，包括图片」「查一下这份文档里有没有这个域名/IP」「截图里有没有」「合规检查有没有泄漏某个地址」，或要把一份备案/投标/对外材料做泄漏扫描时使用。
agent_created: true
---

# docx 全通道关键词检索（文本 + 图片 OCR）

## 什么时候用

- 「这份文档里有没有出现 `xxx.com` / 某个 IP / 某个编号」——**尤其强调"包括图片"**时。
- 对外材料（备案、投标、验收）交付前的**泄漏扫描**：不只看正文，还要看截图、页眉页脚、文档属性、域代码。
- 反向用途：确认"只有文本里有、图片里没有"，或"只有图片里有、文本里没有"。

判定原则：**只要有一个通道没查，就不能说"没有"。** 必须把覆盖率和跳过项一并报出来。

## 脚本

`scripts/docx_scan.py`，三个子命令：

```bash
PY="C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
S="C:/Users/Administrator/.workbuddy/skills/docx-ocr-keyword-scan/scripts/docx_scan.py"

# 1) 文本通道（秒级）
"$PY" "$S" text   "<docx>" <kw> [kw...]

# 2) 文本通道 + 图片 OCR（慢，见下方耗时）
"$PY" "$S" images "<docx>" <kw> [kw...] --procs 2 [--cache FILE] [--max N]

# 3) 只检索已有 OCR 缓存（秒级，反复换关键词时用）
"$PY" "$S" report "<cache.jsonl>" <kw> [kw...]
```

- `--cache` 默认落 `_ocr_cache.jsonl`，**可断点续跑**：中断后重跑只处理没跑完的图，不重来。
- `--max N` 只跑前 N 张（冒烟测试用）。
- 换关键词时**务必用 `report` 复用缓存**，别重新 OCR。

## 首次使用：装 OCR 引擎（隔离安装，不污染主环境）

```bash
PY="C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
"$PY" -m pip install --target "C:/Users/Administrator/.workbuddy/binaries/python/envs/ocrlibs" rapidocr-onnxruntime
```

脚本已把 `envs/pylibs`（Pillow）与 `envs/ocrlibs`（rapidocr）自动注入 `sys.path`，无需再设 `PYTHONPATH`。

## 耗时基线（必须提前跟用户说清楚）

本机 i3-6100（**2 物理核 / 4 线程**）实测：**约 7 秒/张（4 进程）、约 16 秒/张（2 进程）**。

| 文档规模 | 4 进程 | 2 进程 |
|---|---|---|
| 100 张图 | ~12 分钟 | ~27 分钟 |
| 614 张图（143MB 备案资料实测） | ~75 分钟 | ~2.5 小时 |

⚠️ **用户要求"留两个核"时就降 `--procs 2`**，脚本会把子进程优先级设为 `BELOW_NORMAL`（0x4000），前台交互优先。别用完 4 个逻辑核不打招呼。

## 踩过的坑（都验证过）

1. 🔴 **宽高比 ≥ 4 的扁条图会整张漏检。** 实例：`image283.png` 1556×159，整图 OCR 返回 **0 行**，切块+放大后恢复 **39 行**；`image76.png` 984×116 同样 0 → 13 行。原因是 det 模型把长边压到 736，159 高只剩 75px，文字低于可检阈值。
   → 脚本内置自动切块（900×500 网格 + 120px 重叠 + 放大到长边 1400），并且**主批跑完自动筛"高风险图"（无文字 / 面积≥120k 但行数≤15 / 宽高比≥4）做第二轮补测**。
2. 🔴 **`is_private`/`is_global` 那套 IP 判断跟这里无关，但同类问题**：不要只看"有没有命中"，要报**覆盖率**（总图数 / 有文字 / 跳过 / 无文字）。本脚本实测 614 张 = 573 有文字 + 41 极小图标（放大 6 倍补测后确认无文字）。
3. **极小图（<100px）不能直接当"没内容"丢掉**：脚本会跳过 OCR 但在报告里列出，必要时用 6 倍放大补测（实测 41 张里只有 1 张 98×20 有字：「开启智能发送」）。
4. **模糊检索防 OCR 误读**：只搜全串 `bd.example.com` 不够，要再搜 `example` / `xample` / `ample` / `.com:8` 等片段。实测就是靠搜 `example` 才把第二处（表格里的 `bd.example.com`）捞出来。
5. **视觉二次确认不可省**：OCR 置信度 0.88~0.93 也要**裁切放大后亲眼看一眼**（`PIL` crop + `resize×5` + `LANCZOS`）。命中结论要附截图证据。
6. **OCR 会把账号名认成域名片段**：`zwbdkj`、`zwbdcs` 是平台**用户名**（不是域名），别误报。必须回原图确认上下文。
7. **本机 Bash 工具 PATH 残缺**（`tail`/`sleep`/`dirname` 都 `command not found`）→ 命令里别用管道和 `sleep`；等后台任务用 `python -c "import time; time.sleep(N)"`。脚本输出**重定向到文件再 Read**。
8. **写 `.docx` 时若 WPS 开着该文件会被独占锁**（`PermissionError [WinError 32]`)；读不受影响，OCR 扫描随时可做。
9. **文本层别漏的通道**：`word/_rels/document.xml.rels`（外部超链接）、`docProps/*.xml`（文档属性/作者）、`w:instrText`（域代码，如 `XE` 索引项）、`wp:docPr@descr`（替代文字）、`word/embeddings/*`（嵌套 OLE，需递归当 zip 打开）。实例：备案资料 656 个条目里正文只出现 3 次 `iot.example.com`，其余通道干净。
10. 🔴 **说清"子串匹配还是精确匹配"，并做三级归一化**（2026-09-23 冉哥追问后补的）。
   - 脚本用的是**子串包含**（`in` / `count`），**不是精确等值** —— 它比精确匹配**更宽**，所以"0 命中"结论更强。报告里要主动讲明口径，别让用户以为是 `==` 比较。
   - 但子串匹配有**自己的盲区**：关键词被拆开就漏。典型：`bd.zwb` 和 `ds.com` 分在两个 `w:r`；字中间夹了空格/零宽字符（U+200B）/全角句点（`．`）；跨元素拼接。
   - 对策是**三级归一化**（脚本已内置）：`L1 原始` → `L2 去空白` → `L3 只留 a-z0-9`。**报告要给出每一级的命中数**，而不是只给一个"0"。
   - 同时加**剥标签**（`re.sub(r'<[^>]*>','',seg)`）—— 这一步天然把跨 run 的碎片拼回来。逐段做（不要全局做，否则相邻段落会被拼出**假邻接**）。全局口径与逐段口径差异 = 假邻接量，可作交叉校验。
11. **全条目字节级兜底**：不只搜 `.xml/.rels/.txt`，而是把 zip 里**每个条目**的原始字节都搜一遍（ASCII + UTF-16LE 两种编码），这样 `media/*.png|jpeg`、`*.bin`、嵌入字体、`*.dat` 这些"不被当文本"的条目也覆盖了 —— 图片的 EXIF/tEXt 块、OLE 二进制流里藏的字符串都能捞到。实测 656 个条目全扫，零遗漏。
12. **修订痕迹通道别忘**：`w:delText`（**修订-删除的文字**——删掉了但还存着）、`w:insText`、`w:fldSimple`、`w:txbxContent`（文本框内文字）。这些之前不在通道清单里。实测本份文档 `w:delText` = 0 条、`w:txbxContent` = 0，盲区不存在；但如果文档开过修订，`w:delText` 就是真泄漏点。

## 输出

脚本把结果打 stdout；本机惯例是重定向到文件再 Read。报告的结构化清单（域名/IP 全量分组）见
`H:\agent工作空间\.workbuddy\tmp\inventory.py` 的思路：按 `zwbd 相关 / 其他域名 / 外网 IP / 内网 IP` 分组，附命中图片名，便于用户逐条核对。
