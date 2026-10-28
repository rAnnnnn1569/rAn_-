# 运维技能包（skills）

一组可直接复用的「技能」目录。每个目录是一份 `SKILL.md` + `scripts/`，约定俗成的结构：

```
<skill-name>/
├── SKILL.md          # 触发条件 + 操作流程 + 铁律 + 踩坑
├── scripts/          # 实际干活的脚本
└── references/       # 可选：查表用的大块资料
```

`SKILL.md` 是给人（和 AI）读的操作说明，`scripts/` 是给机器跑的。把「什么时候用」和「具体怎么做」写在一起，下次不用回忆。

---

## 一、协议类

| 技能 | 解决什么 |
|---|---|
| [`jt808-frame-build/`](jt808-frame-build/) | 给 JT/T 808 的**裸消息体**补全消息头，输出可直接下发的 `7E…7E` 完整帧；反向解析 `0x0200` 的位置基本信息 + 附加信息 TLV |
| [`jt809-frame-decode/`](jt809-frame-decode/) | 解码 JT/T 809 帧（`0x5B…0x5D`），定位「解析越界 / 源数组长度不足 / CRC 不过 / 数据体为空」；含 `0x1412` 报警处理专项 |
| [`protocol-lookup/`](protocol-lookup/) | 协议条款速查：808 / 809 / 1078 / 苏标 / 川标的关键字段与判定规则，带标准清单索引 |

**这几个不是"文档翻译"，是把踩过的坑写成了判定逻辑**：

- `jt808-frame-build` 里有 **12 字节 / 17 字节双头长的判别式**。拿错头长会把整个消息体错位 5 字节，而错位后的字段值看起来「像那么回事」，能一路错到解析结论。
- `jt809-frame-decode` 的 CRC 覆盖范围是 `data[1:-3]`（不含起始 `0x5B`）。差一个字节就永远校验不过。

## 二、媒体与系统类

| 技能 | 解决什么 |
|---|---|
| [`platform-video-no-moov-repair/`](platform-video-no-moov-repair/) | 监控平台导出的 mp4 打不开/提示损坏 —— 典型原因是 `moov` 在文件尾或缺失，本工具做无损修复 |
| [`win-disk-cleanup/`](win-disk-cleanup/) | Windows 磁盘清理：先统计占用摸清大头，再按安全顺序清理，最后校验空间真的释放了 |

`platform-video-no-moov-repair` 处理的场景很具体：**文件大小正常、播放器说格式不支持**。这时不是文件坏了，是索引位置不对。

## 三、文档处理类

| 技能 | 解决什么 |
|---|---|
| [`docx-fulltext-diff/`](docx-fulltext-diff/) | 两版 Word 的严格全文对比：元数据 / 篇幅 / 图片表格数 → 逐段正文 → 章节树 → 段落级与句子级 diff |
| [`docx-batch-reformat/`](docx-batch-reformat/) | 按某个参照段的格式，批改 N 个段落（字体 / 字号 / 缩进 / 对齐 / 样式），文字一字不改 |
| [`docx-ocr-keyword-scan/`](docx-ocr-keyword-scan/) | Word 里做「全通道」关键词检索：文本层（正文 / 域代码 / 页眉页脚 / 文档属性 / 超链接 / 嵌套 OLE）**加图片 OCR** |

`docx-ocr-keyword-scan` 的价值在于「确认某样东西**不存在**」。只查正文文本层是没有说服力的 —— 域代码、页眉页脚、文档属性、图片里都可能藏着。

## 四、仓库与流程类

| 技能 | 解决什么 |
|---|---|
| [`repo-desensitize/`](repo-desensitize/) | 把本地脚本 / 笔记脱敏后备份进 Git：摸底 → 脱敏 → 校验 → 才 commit。含 `lake2txt.py` / `docx2txt.py` / `desensitize.py` / `verify.py` |

**顺序不可颠倒**：脱敏 → **校验** → 才 commit。凭据一旦进 Git 历史，`revert` 是无效的，只能 `filter-repo` / BFG 清历史，代价远大于走一遍流程。

---

## 使用方式

这些目录本身就是「可直接读取的操作手册」，两种用法：

**当文档读** —— 打开对应 `SKILL.md`，按里面的步骤做。铁律和踩坑都写在最前面。

**当工具跑** —— 直接调 `scripts/` 下的脚本：

```bash
# 例：解析 808 裸消息体
python scripts/build808.py --payload "0000000000000000012345678901..."

# 例：对比两版 Word
python scripts/docx_fulltext_diff.py A.docx B.docx --out ./out
```

> 各脚本的参数以对应 `SKILL.md` 里的说明为准，不要凭名字猜。

## 目录约定

- `scripts/out/` 是**运行产物**目录，里面的 `.gitignore` 写成 `*` + `!.gitignore`，即**自身忽略**，产物不会入库。
- 脚本一律不把凭据写在代码里，走参数或外置配置。
- `SKILL.md` 的 frontmatter 里 `description` 要写清「什么时候用」，而不是「这是什么」——前者能触发，后者不能。
