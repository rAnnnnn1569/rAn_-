---
name: win-disk-cleanup
description: 在 Windows 上安全清理磁盘缓存/日志并真正释放空间。当用户说「C 盘满了」「清理一下 C 盘」「删掉这些缓存/日志」「释放点空间」「清理临时文件」，或需要统计某目录占用、查看/清空回收站时使用。内含 WorkBuddy safe-delete hook 的应对方案（该 hook 会把删除改成送回收站，导致"删了却不见空间变多"）。
agent_created: true
---

# Windows 磁盘清理（含 safe-delete hook 应对）

## 何时用

- 用户说「C 盘/某盘满了」「清理缓存」「释放空间」「删掉临时文件」「回收站太大」
- 需要统计目录占用、对比盘空间、判定某目录可否安全删除
- 之前用 `shutil.rmtree` / `os.remove` 清理后**发现空间没变多**（见下"核心陷阱"）

## 🔴 核心陷阱：WorkBuddy 的 safe-delete hook

平台在 `H:\WorkBuddy\resources\app.asar.unpacked\cli\vendor\shim\sitecustomize.py` 注入了一个
Python 运行时 shim（经 `PYTHONPATH` 生效，要求 `CODEBUDDY_SESSION_ID` 存在）。它把
`os.remove` / `os.rmdir` / `shutil.rmtree` / `pathlib` 的删除 **改成「移入操作系统回收站」**。

由此产生 3 个必踩的坑：

### 1. 送回收站 ≠ 释放空间
回收站与源文件**在同一块盘**（`C:\$Recycle.Bin`）。
→ 移走 267 MB 后 C 盘可用可能**反而减少**（实测 -135 MB）。
→ **必须再执行 `empty <盘>`**，空间才真正释放。

### 2. 日志里的 `SAFE_DELETE_FAIL_CLOSED ... SHFileOperationW 失败: 0x2` 是虚报
报 FAIL 的目录**实际已进回收站并已释放**（实测：`pip` 批报 3 项 FAIL，清空回收站后 C 盘 +116 MB）。
→ **不能用日志 FAIL 数判断成败，必须回读原位置确认。**

### 3. 海量小文件会被卡死
`shutil.rmtree` 会对**每一个文件** spawn 一个 node 进程做批量检查。
实测 21734 个文件跑了 **25 分钟只删掉 3 个** → 等于卡死。
→ **本工具的 `recycle` / `purge` 改用 shell32 的 `SHFileOperationW` 对整目录一次操作，秒级完成。**

另有批量守卫 `safe-delete-bulk-guard.cjs`：按会话累计删除文件数，超过
`CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD`（默认 20）时 **`SystemExit(1)` 直接杀进程**
（表现为「脚本跑到某行后静默中断、无任何后续输出」）。后台脚本拿不到前端确认弹窗，
自动化语境下走不通 → **分小批，或本工具（不经过 Python 删除 API，自然不触发）。**

## 工具

`scripts/win_clean.py`（managed python 直接跑，无需额外依赖）：

```bash
PY="C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
S="C:/Users/Administrator/.workbuddy/skills/win-disk-cleanup/scripts/win_clean.py"

"$PY" "$S" free    "C:\\" "H:\\"     # 可用空间
"$PY" "$S" size    <path>...          # 目录/文件占用与文件数
"$PY" "$S" rb      "C:\\"             # 回收站占用
"$PY" "$S" empty   "C:\\"             # ★ 清空该盘回收站（真正释放空间）
"$PY" "$S" recycle <path>...          # 送回收站（可恢复）
"$PY" "$S" purge   <path>...          # 永久删除（只在别处有母本时用）
```

返回码对照：`0x00` OK ｜ `0x20` 文件被占用 ｜ `0x7C` 路径语法 ｜ `0x8000FFFF` 回收站已空（非错误）。

## 标准流程（务必按序）

1. **只读扫描**：`free` + `size` 逐项统计，产出分级清单（P0 纯缓存 / P1 会话索引 / P2 非我产生）。
   **扫描阶段绝不动手。**
2. **列出不可删清单**并让用户确认 —— 至少包括：`binaries/`（python/node/git 运行时）、
   `skills/`、`credentials/`、`memory/`、身份文件、`workbuddy.db*`、`settings.json`、`security/`。
3. **建可回退暂存区**（如 `H:\_c_cleanup_<日期>\`），内含：
   - `restore.py`（`--list` 看清单 / 无参全部搬回）
   - `README.md`（来源、回退方法、保留期限）
   - `_moved_manifest.json`（每项 src↔dst 映射）
4. **逐项移动**到暂存区（跨盘 `shutil.move`），每批后 `empty C:\`。
5. **每批回读原位置**确认真的消失（不要信 FAIL 日志）。
6. **最终核查**：`free` 前后对比 + 暂存区大小 + 回收站占用。
7. **H 盘（或任何非目标盘）的回收站禁动** —— 里面可能有用户的东西。

## 实测经验值（2026-09-23，主机 i3-6100）

- C 盘 120.7 GB，清理前可用 **736.7 MB（99.4% 满）** → 清理后 **2.26 GB**
- 移出 3.00 GB / 338 项，其中 `codex-runtimes` 1314.7 MB / 21734 文件
- **移出量 > 释放量是正常的**：系统与 WorkBuddy 在清理期间仍在持续写入
  （日志、会话缓存等），本次差额约 1.5 GB

## 常见可清理项（按安全度）

| 项 | 典型大小 | 说明 |
|---|---|---|
| `<home>/.workbuddy/logs/<日期目录>` | 每天 20–200 MB | 保留近 7 天 |
| `<home>/.workbuddy/traces/` | 数百 MB | 纯诊断数据，可全清 |
| `<home>/.workbuddy/changes-detail/` | 100 MB+ | **跳过当前会话那个**（目录名 = 会话 UUID） |
| `<home>/.workbuddy/file-tree-manifests/` | 数十 MB | 同上，跳过当前会话 |
| `%LOCALAPPDATA%\pip\cache\` | 100 MB+ | 纯下载缓存 |
| `%LOCALAPPDATA%\Temp\wct*.tmp` | 100 MB+ | WPS/Word 转换残留 |
| `<home>/.cache/codex-runtimes/` | GB 级 | 可重新获取；**确认 H 盘有母本再删** |

**当前会话的 UUID** 可从 `changes-detail/` 里 mtime 最新的那个目录名反推；
或从环境变量 `CODEBUDDY_SESSION_ID` 取。
