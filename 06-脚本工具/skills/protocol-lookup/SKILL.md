---
name: protocol-lookup
description: |-
  Use this skill when the user asks about vehicle or car telematics (车载) protocols
  in the 中卫北斗/车联网 context, for example JT/T 808/809 message structures and field
  definitions, overspeed (超速) determination rules, active-safety (主动安全) alarm
  type flag bits / alarm IDs / warning levels, Sichuan TSCSDX 0003.1-7 specification
  clauses, or 10号令 / 川交函〔2020〕544号 / 20号文 regulatory provisions. This skill
  searches the protocol corpus in the agent_Memory repo (docs/协议 OCR text plus docs/协议原版
  PDFs) and returns precise, verifiable clauses. Trigger on keywords like 协议, 808, 809,
  超速, 报警, 标志位, 报警ID, 命令字, 报文, 规范, 条款, 主动安全, 定位, 终端.
---

# Protocol Lookup（车载协议查档）

## Overview

This skill answers questions about road-transport vehicle dynamic-monitoring protocols
(JT/T 808-2019, JT/T 809-2019, Sichuan TSCSDX 0003.1~7, and the 10号令 / 544号 / 20号文
regulatory docs) by retrieving the **authoritative original text** from the agent_Memory
repo, rather than answering from memory. It encodes the verified retrieval workflow so a
  future agent reproduces it exactly.

## MANDATORY TRIGGER — always load this skill first (硬性规则)
The user has mandated (2026-08-27): **any protocol-related question MUST trigger this
skill FIRST.** Do NOT answer protocol questions by directly Grep/Reading the txt files
without loading this skill — go through this skill's workflow every time. Trigger
whenever the user's request touches ANY of: 808/809 报文、解析 hex 报文、超速判定、
主动安全报警标志位/报警ID/分级、TSCSDX/苏标/川标 规范条款、10号令/544号/20号文、
定位/轨迹协议字段、命令字、限速规则、报警附件上传(ActiveSafety)、视频协议(1076/1077/1078)
等。 When in doubt, trigger.

**Fallback (skill-first, not skill-only):** Always START with this skill. If its lookup
does NOT yield the answer (e.g. a platform-private alarm code like `54306`/`D404` that is
NOT in the standard corpus, or a clause the OCR txt garbled), THEN fall back to other
methods: directly Grep/Read the txt, consult `docs/用户笔记/` summaries, ask the user for
the platform's alarm dictionary table, or request the missing original doc. Never give up
after the skill alone — but the skill must be the first step, never skipped.

## Lookup priority (查询优先级 — 用户 2026-08-27 指定)

When the same concept appears in more than one standard, resolve conflicts by this
priority order (highest first). **Always prefer 川标 (Sichuan) — the user mandated
"优先筛选川标".** 主动安全 / 主防报警 / 报警补传 / 查岗 / 视频报警 questions should be
answered from Priority 1–2 first.

1. **(最高) 四川新主防（今点）文档集** — 当前川标主防(智能监控)最新实现：
   - `四川新主防今点_升级技术说明.txt` (2026-04)：部↔川2021↔川2025 报警三标映射表 + ALARM_ID(34位) 规则
   - `四川新主防今点_视频补充协议.txt` (2020-7-7)：视频报警码 `0x02xx`
   - `四川新主防今点_平台对接内容.txt`：809 主防消息ID总览
   - `四川新主防今点_查岗功能对接.txt` (2025-11)：查岗 `0x9301`
2. **(最高) 《两客一危部省交互规范（试行）》** (2026-03)：部省业务流程 + 附录 B.1 `0xD201~0xD508` 智能监控报警码
3. **川标 TSCSDX 0003.1~7**：位置/非位置类报警、809/808 交换基础
4. **苏标 DB32/T 3610**（=桂标，广西车辆）
5. **808/809 国标**（扫描件 OCR，兜底）

Rule: conflicts across standards → **Priority 1–2 (川标/部省) definition wins**. Grep
the 今点 set first with `--doc 四川新主防今点`; fall back down the list only if absent.

## Key Facts (verified 2026-08-26)

- **Protocol text corpus** lives in the `agent_Memory` repo (default base
  `H:/agent工作空间/agent_Memory`; override with env `AGENT_MEMORY_REPO` or `--repo`).
- **`docs/协议/*.txt`** = OCR text versions of all 11 protocol docs. **Readable and
  grep-able** → this is the PRIMARY source for lookups. (Confirmed: TSCSDX_0003.3.txt
  extracts ~67k clean Chinese chars.)
- **`docs/协议原版/*.pdf`** = original PDFs, split into two kinds:
  - **Text-layer PDFs** (Sichuan `TSCSDX 0003.1~7` series): extract precise text with
    `pdfplumber` / `pypdf` for exact quoting.
  - **Scanned PDFs** (`808-2019.PDF`, `809-2019.PDF`): broken text layer → raw extraction
    yields garbage `(cid:xxx)`. The OCR `.txt` is the ONLY usable text form; do NOT claim
    to "read the PDF" for these — rely on the txt, optionally render a page to an image
    for spot visual check (see Caveats).
- **`docs/用户笔记/公司平台笔记.md`** = the user's own curated authority on alarm
  grading / overspeed levels; cross-check high-frequency exam points there.
- Required libs (already installed in this env): `pypdf` 6.16.2 + `pdfplumber` 0.11.10
  (install via `python -m pip install pypdf pdfplumber` if missing).
- **`docs/用户笔记/*重点总结*.md`** = 用户要求纳入检索范围的**概念性归纳总结**（如
  JT794 / JT1078 / JT1076 终端与视频系列重点总结）。`grep` 子命令会**自动一并搜索**这些
  md，命中行标注 `[总结]` 以区别于协议原文 txt。适合快速定位概念性结论；如需权威原文，
  仍应回 `docs/协议/*.txt` / 原版 PDF 核对。

## Retrieval Workflow

1. **Locate** — Run `scripts/protocol_lookup.py grep "<keyword>"` (see Scripts) to grep
   `docs/协议/*.txt` **以及 `docs/用户笔记/*重点总结*.md`**（总结命中会标 `[总结]`）。
   Keywords: 超速, 报警, 标志位, 报警ID, 0x, 命令字, 报文, 位置汇报,
   鉴权, 补传, 809, 808, 主动安全, 分级, 限速, 视频终端, 通道, 灾备, etc.
   The script prints file + line + context.
2. **Read the section** — Open the matching `.txt` (use the Read tool) at the reported
   line range and extract the relevant clause / table / flag-bit definition.
3. **Verify when precise** — If the clause is from a **text-layer PDF** (TSCSDX 0003.x),
   run `scripts/protocol_lookup.py pdf <pdf-path> [start] [end]` to extract the exact
   page text and confirm wording/numbers. For **scanned 808/809**, the txt is already the
   source of truth; only render-to-image for an ambiguous glyph.
4. **Cross-check** — For alarm flag bits / overspeed levels, also consult
   `docs/用户笔记/公司平台笔记.md` (报警分级 / 超速 sections).
5. **Answer** — Quote the clause(s) with the doc name + section. Flag any point that
   rests on the OCR txt (which may have minor char errors) vs. a verified PDF original.

## Scripts

`scripts/protocol_lookup.py` — three subcommands:

- `grep "<kw>[,<kw2>...]" [--repo PATH] [--doc NAME]`
  Grep all `docs/协议/*.txt` **plus `docs/用户笔记/*重点总结*.md`** for keyword(s); prints
  `file:line:matched-line` with a few context lines. Summary hits are tagged `[总结]`.
  Use `--doc` to restrict to one doc (e.g. `--doc 808` or `--doc JT1078`).
- `pdf <pdf-path> [start] [end] [--repo PATH]`
  Extract text from a text-layer PDF page range (default page 1). For scanned PDFs this
  returns garbage — that itself confirms "scan, use txt".
- `list [--repo PATH]`
  List every protocol doc with its txt/pdf presence and text-layer vs scanned status.

Example:
```
python scripts/protocol_lookup.py grep "超速" --doc 808
python scripts/protocol_lookup.py pdf "docs/协议原版/TSCSDX 0003.3—2025 ... 第3部分.pdf" 20 25
```

## Caveats

- `docs/协议/` has **no txt** for 10号令 (PPTX) and 544号 (PDF) — for those, read the
  original `docs/协议原版/` file directly (PPTX via python-pptx / PDF via pdfplumber).
- OCR txt may contain **occasional character errors**; for disputed numbers/bit definitions,
  verify against the PDF original or the user's notes before stating a conclusion.
- Rendering a scanned PDF page to an image for visual reading is environment-sensitive
  (temp images may not persist across commands here) — treat it as immediate spot-check
  only, never as a persistent artifact.

## Frame delimiter: 808 vs 809 (DO NOT confuse — common mistake)
- **JT/T 808** frames are delimited by `0x7E` ... `0x7E` (start/end identical).
- **JT/T 809** frames are delimited by `0x5B` ... `0x5D` (ASCII `[` ... `]`).
- **ALWAYS check the first/last byte BEFORE parsing.** 808 and 809 have completely
  different header layouts and checksum algorithms. Applying 808 rules to a 809 frame
  (or vice-versa) yields garbage: wrong message ID, failing checksum, nonsensical
  fields. This exact mistake was made once — `5B`/`5D` is 809, NOT 808.

## Parsing a raw hex message
1. Identify delimiter first: `0x7E` = 808, `0x5B`/`0x5D` = 809.
2. **808-2019 header** (big-endian): MsgID(2) + MsgBodyProps(2) + PhoneBCD(6/10) +
   Seq(2) + optional SubPkg(4) + optional Encrypt(1); checksum = XOR over header+body.
3. **809-2019 header**: Version(3) + MSG_SN(4) + MSG_TIME(8, UTC) + SRC_ADDR(2) +
   DST_ADDR(2) + MSG_LENGTH(4) + ENCRYPT(1) + CRC(1, CRC16-CCITT poly 0x1021); data
   body follows. CRC16-CCITT is computed from header start through the byte before CRC.
4. If header fields look wrong (length=0, time not a valid UTC, large zero-padding),
   the capture is likely a **re-encapsulated / logged / platform-private record** that
   reuses the delimiter but is NOT a live compact 809 frame — say so, don't force-fit.

## Alarm type codes: standard vs platform-private
- SuBiao (DB32/T 3610) alarm-type codes: Table 50 = hex `0x0001`~`0x0314`
  (decimal 1~788); attachment alarm codes = 4-decimal `外设ID+模块类型`
  (e.g. `6401` = forward-collision warning, 外设 64 = ADAS module).
- **5-digit decimal codes in the platform UI (e.g. `54306`) and `Dxxx` codes inside alarm
  filenames map to the NEW ministry trial spec《“两客一危”动态监控报警信息部省数据交互
  技术规范（试行）》(中国交通通信信息中心, 2026-03), Appendix B.1 class 4 「智能监控报警」
  (source tagged 地方标准/规范)** — NOT platform-private, NOT in 川标/桂标/苏标:
  `54306` = `0xD422` 车辆非法位移报警, `D404` = `0xD404` 急减速报警, etc. Convert
  decimal→hex (`0xD + last3 hex digits`) and look up B.1. These 0xD201~0xD508 codes are
  the standard mapping for 主防/DSM/terminal alarms in the 2026-03 ministry trial spec.
  Only if a code falls OUTSIDE `0xD201~0xD508` treat it as platform-private and query
  sys_dict. (Verified 2026-08-27 from the user-provided PDF; supersedes earlier
  "platform-private" assumption for 54306/D404.)
- **For the current 川标 主防 (智能监控) implementation, consult 四川新主防（今点）_升级技术说明
  (Priority 1) FIRST** — it gives the 部技术规范 ↔ 川标2021 ↔ 川标2025 报警类型三标映射表
  and the ALARM_ID(34位) rule (`车籍4_SIM13_类型4_Unix时间戳10`). It confirms 川标2025 主防
  报警已对齐部规范、统一用 `0xDxxx` (NOT the older TSCSDX 0003.x position/non-position
  ranges 0x0001~0x00FF / 0xA001). Video alarms have a SEPARATE code family `0x02xx`
  (SC 视频补充协议) — same event as the `0xD401`-style codes reported via `0x1402`.
- The 2026-03 ministry trial spec **is now in the corpus** (text-layer PDF
  `docs/协议原版/两客一危部省交互规范-试行.pdf` + OCR txt `docs/协议/两客一危部省交互规范-试行.txt`,
  registered in `TXT_DOCS` / `TEXT_LAYER_PDFS`). Grep with `--doc 两客一危`. Its B.1
  alarm-code table resolves the 0xDxxx family; summary at
  `docs/用户笔记/两客一危部省交互规范重点总结.md`.

## References

`references/protocol_inventory.md` — full file inventory (txt ↔ pdf mapping, text-layer
vs scanned status) and the cross-reference pointers used above.
