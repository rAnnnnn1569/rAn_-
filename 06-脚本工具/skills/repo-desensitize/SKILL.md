---
name: repo-desensitize
description: 把本地脚本 / 笔记文档「脱敏后」备份进 Git 仓库。当用户提到「把脚本纳入仓库备份」「脚本入库」「备份到 agent_Memory」「脱敏入仓」「代码进仓库」「把笔记整理进公开仓 / rAn_-」「语雀 / 思源导出件入库」等，或准备把含数据库口令 / 令牌 / 客户数据的本地脚本、导出笔记提交到 GitHub 时使用。先脱敏 → 再校验 → 才 commit，凭据绝不进 Git 历史。
agent_created: true
---

# 脚本目录脱敏入库

## 铁律

🔴 **凭据绝不进 Git 历史。** 一旦提交，`revert` 无效，只能 `filter-repo` / BFG 清历史 —— 代价远大于走一遍脱敏流程。

**顺序不可颠倒**：脱敏 → **校验** → 才 commit。
校验不通过**禁止提交**，逐项确认后再来。

🔴 **原文一律不动。** 只对**入库副本**脱敏；本地在跑的生产脚本一字不改（避免影响业务）。
每个副本文件头写【脱敏副本】+ 原文路径，方便日后回溯。

## 四步流程

```
① 摸底   列出待入库文件，逐个判定「有无凭据」
② 脱敏   desensitize.py 生成副本（--clean 原样 / --mask 脱敏）
③ 校验   verify.py 扫描残留，必须输出「✅ 通过」
④ 提交   git add → commit → push（push 失败保留本地，等下一轮，禁 force push）
```

### ① 摸底（别跳过）

```bash
# 扫出含硬编码凭据的文件（只输出文件名与字段，不打印明文）
python - <<'EOF'
import re, pathlib
pat = re.compile(r"""['"]?(\w{2,20})['"]?\s*[:=]\s*['"]([^'"]{4,})['"]""")
SENS = {'password','passwd','pwd','username','user','usr','ip','host',
        'dbname','db','database','port','token','secret','api_key','sessionid'}
for p in sorted(pathlib.Path('.').rglob('*')):
    if p.suffix.lower() not in ('.py','.md','.txt','.json') or '__pycache__' in p.parts:
        continue
    ks = set()
    for line in p.read_text(encoding='utf-8', errors='ignore').splitlines():
        for m in pat.finditer(line):
            if m.group(1).lower() in SENS and len(m.group(2)) >= 4:
                ks.add(m.group(1).lower())
    if ks: print(f"{p}\t{sorted(ks)}")
EOF
```

判定口径：

| 类别 | 处理 |
|---|---|
| 无凭据的纯逻辑工具、词库 | `--clean` 原样入库 |
| 含 DB 口令/令牌/接口地址 | `--mask` 脱敏入库 |
| **客户数据**（车辆、SIM 卡号、公司名单、白名单数据表） | **不入库** |
| 历史冗余（`bak/`、`test*.py`、构建产物） | **不入库** |

### ② 脱敏

```bash
python scripts/desensitize.py \
    --src  "D:\python\whitelist" \
    --dst  "H:\...\agent_Memory\docs\源码\whitelist" \
    --clean wl_audit.py ban_domains.txt ip2domain.py \
    --mask  whitelist_check.py ipfinder.py README.md \
    --skip-ip ban_ips.txt
```

两轮脱敏，覆盖的形态不同 —— **缺一轮就会漏**：

| 轮次 | 目标 | 形态 |
|---|---|---|
| 第一轮 | 键值对 / 赋值 | `"password": "xxx"`、`key = "xxx"`、`mysql_conf={...}` |
| 第二轮 | 语义 | **数组里的裸 IP 字面量**、SIM 卡号、第三方公司名 |

### ③ 校验（不可省）

```bash
python scripts/verify.py "<目标目录>" --skip-ip ban_ips.txt --strict
```

必须看到 `✅ 通过`。出现 `⚠️` 就回去补脱敏，**不要**用「应该没事」放过。

### ④ 提交

```bash
git add <具体路径>          # 别 git add -A，容易把无关文件带进去
git commit -m "docs(源码): ..."
git push origin main        # 失败则保留本地等下一轮；禁 force push / 禁重置 remote
```

## 脱敏口径

**替换**

- DB 连接五元组：主机 / 端口 / 账号 / 口令 / 库名
- 接口地址、会话令牌、机器人 webhook
- 客户 SIM 卡号、手机号
- **真实公网 IP → `192.0.2.9`**（RFC 5737 文档段，保持代码可解析）
- 第三方公司名 → `<第三方公司>`

**有意保留**（非遗漏，校验时要用 `--skip-ip` / `--keep-company` 声明）

- **词库文件**（如禁止 IP 段清单）：内容全是真实 IP，但它就是审查依据，脱掉就废了
- **私有段 / CGNAT / 文档段网段常量**：`10.0.0.0/8`、`100.64.0.0/10`、`192.0.2.0/24` 等 —— 是判定逻辑的依据，误改会让 `ip_kind()` 之类的函数直接失效
- 本公司名

## 坑（踩过的）

1. 🔴 **只扫 `"password": "xxx"` 会漏**：数组里的裸 IP（`ips = ["1.2.3.4", "5.6.7.8"]`）不在键值对形态里，
   必须靠**第二轮按 IP 语义**扫。2026-09-17 入库 whitelist 工具箱时，第一轮跑完还剩 36~1954 处 IP 残留。
2. 🔴 **没有保护白名单就会误伤**：`100.64.0.0/10` 被替换后，`ip_kind()` 的 CGNAT 判定直接失效。
3. 🔴 **`cell.fill` 那类坑的同类**：校验脚本自己的正则会在**正则字符串**里误报真实 IP
   （`r'\b(?:\d{1,3}\.){3}\d{1,3}\b'`）。`verify.py` 已加 `REGEX_HINT` 过滤，
   判读校验结果时要区分「误报」与「真残留」。
4. **源文件编码不确定**：中文脚本可能 UTF-8 也可能 GBK → 按 `utf-8 → gbk → gb18030` 顺序试解码。
5. **文件头插在 shebang 之后**：`.py` 有 `#!` 时不要把声明插到第一行，否则破坏 shebang。
6. **`git add -A` 会顺手带上 `__pycache__`、临时测试文件** → 永远 `git add <具体路径>`。

## 五、笔记 / 文档入公开仓（第二个工作域）

脚本入库之外，冉哥还会把**语雀 / 思源导出的工作笔记**素材导进公开仓 `rAn_-`。
判定轴不是"文件类型"，而是**内容性质**：能写成**可复用的技术复盘/方法论**的才进，业务台账不进。

### 0. 先做回滚准备（用户会明确要求，自己也该主动做）

```bash
cd <仓库>
git branch backup/before-import-YYYYMMDD HEAD      # 备份分支（指到当前 HEAD）
git rev-parse HEAD                                  # 记下 commit
# 再打一个 zip 快照到仓库外（工作根目录），双保险
```

### 1. 导出件格式解析（纯标准库，无需第三方）

| 格式 | 说明 | 脚本 |
|---|---|---|
| `.lake`（思源 / 语雀导出） | **是 HTML 结构**（`<!doctype lake>` + `<p>/<span>/<card>`），不是 JSON | `scripts/lake2txt.py <in.lake> <out.txt>` |
| `.docx` | zip + `word/document.xml` | `scripts/docx2txt.py <in.docx> <out.txt>` |

⚠️ **两个坑**：
- 脚本参数是**Windows 路径**（`H:/...`）。managed Python **不认 Git Bash 的 `/h/...`**（报 `FileNotFoundError`）。
- 输出**先落 `out.txt` 再 Read**，别指望管道 —— 本机 Bash 工具 PATH 已损坏。

### 2. 筛选口径（哪些素材能进公开仓）

| 素材类型 | 处理 |
|---|---|
| **故障复盘 / 排查方法论 / 命令速查 / 协议概念学习** | ✅ 整理成独立文档入库（放对应分区，如 `05-故障复盘/`） |
| **平台操作手册、客户/车辆台账、白名单数据、日报流水** | ❌ **不入库**（业务/客户数据） |
| **直接含明文凭据的文档** | ❌ 原文不入库；**只提炼脱敏后的技术案例** |
| **源码正文**（`.py`/配置全文） | ❌ 走上面第一工作域（`desensitize.py`），别当笔记处理 |
| **待办清单**（含人名、日期、客户名） | ❌ 不入库 |

### 3. 笔记类脱敏替换表（与脚本口径不同，别复用脚本规则）

| 原文 | 替换为 |
|---|---|
| 真实公网 IP | `192.0.2.x` / `203.0.113.x`（RFC 5737 文档段） |
| 真实内网 IP | `192.0.2.x` 或直接写 `<内网主机>` |
| 真实域名 | `备案域名` / `<对端平台>` |
| **第三方平台 / 厂商 / 客户公司名、监管机构名** | **角色名**：`<对端平台>`、`上级监管平台`、`设备厂商` |
| 同事姓名（"X 工""X 哥"） | `同事` |
| 车牌号、SIM 卡号 | `<车牌>`、`<SIM>`（示例用 `川A00000` 这类明显占位） |
| 账号 / 口令 / MD5 / 令牌 | `<账号>`、`<密码MD5>`、`<令牌>` |
| 内部脚本文件名、表名 | 泛化角色名（`recharge.py`）或 `<续费记录表>` |

> **保留**：公开云厂商与开源软件的**通用技术事实**（如"阿里云 VMS 语音 API"、"MongoDB 官方源"），
> 这类不暴露雇主业务关系。**暴露业务链条的名字一律换角色名。**

### 4. 提交前**必须**跑一遍残留扫描

```bash
cd <仓库>
grep -rEn "<真实IP片段>|<域名关键字>|<口令片段>|<车牌前缀>|<厂商标识>|<同事称呼>|<内部表名>" . \
  --include=*.md --include=*.sh --include=*.example
# 输出为空才算通过
git add <具体路径>          # 别 git add -A
git commit -m "..."
git push origin main        # 先直连，失败 3 次再切 7892；禁 force push / 禁改 remote
git fetch origin && git log --oneline -1 origin/main   # 核验远端真的同步了
```

### 5. 案例

| 时间 | 来源 → 目标 | 结果 |
|---|---|---|
| 2026-09-29 | 语雀导出 5×`.lake` + 5×`.md` + 2×`.docx` → `rAn_-` | 写入 6 篇新文档 + 2 篇更新；4 份素材**判定不入库**（含明文凭据的故障记录 docx、待办清单、源码正文、日报流水段）；全仓扫描仅 1 处机构名命中→改为角色名；commit `0016307` 直连 push 成功 |
| 2026-09-29 | 工作笔记"问题排查实例"5 段 + 故障记录 docx 的代码案例 → `rAn_-/05-故障复盘/` | 新增 5 篇（Nginx 源码部署未服务化、Zabbix 可用性色块辨析、RAID 卡掉盘应急切换、接口传参契约变更 `json=`→`data=` 修复实录、链路诊断速记）；commit `c7729f7` 直连 push 成功 |

## 已入库案例

| 时间 | 目标 | 结果 |
|---|---|---|
| 2026-09-17 | `D:\python\whitelist` → `agent_Memory/docs/源码/whitelist/` | 24 个文件（9 原样 + 14 脱敏 + 1 说明）；扫描出 15 个文件含明文 MySQL 五元组、1 个含会话令牌+SIM 卡号；两轮脱敏后校验 0 残留；未入库 76 个 xlsx 客户数据、客户名单 json |
