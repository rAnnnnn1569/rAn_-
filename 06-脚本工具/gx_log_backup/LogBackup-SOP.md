# 广西日志自动压缩推送 SOP

> 双 Windows Server：源机 → 目标机，每天凌晨自动打包并推送，保留 30 天。
> 最后更新：2026-10-28

---

## 一、拓扑与前提

| 项 | 值 |
|---|---|
| 源机 | `192.0.2.30`（Windows Server），日志目录 `D:\Cgo8\Log\Top809External\<平台日志目录>` |
| 目标机 | `192.0.2.88`（华为云，与源机同 VPC 同子网 `10.0.0.0/24`），落地 `L:\广西日志备份\` |
| 传输 | SMB（TCP 445），源机走 UNC `\\192.0.2.88\<共享名>` |
| 频率 | 任务每天 02:00 触发，脚本内 **60 小时闸门** ⇒ 实际约 **2.5 天一次** |
| 工具 | 纯系统内置：`tar.exe` + `robocopy` + `schtasks`，无 Python |

🔴 **路径认知**：`L:\广西日志备份` 是**目标机上的本地盘符**，源机不存在 L 盘，跨机只能写 UNC。

---

## 二、一次性准备

### 2.1 目标机 192.0.2.88

```cmd
net user logpush <强密码>
net share 广西日志备份=L:\广西日志备份 /grant:logpush,full
net share                      ← 确认【共享名】真实是什么，与文件夹名可能不同
```

### 2.2 源机 192.0.2.30

```cmd
net user svc_logpush <强密码>
rem ⚠️ 下面这条必须【以 svc_logpush 身份】执行，否则计划任务读不到凭据
cmdkey /add:192.0.2.88 /user:logpush /pass:<密码>
dir \\192.0.2.88\<共享名>       ← 验证：能列出文件才算通
```

🔴 **cmdkey 凭据按 Windows 账号隔离**。存凭据的账号必须与计划任务的 `/RU` 完全一致。`SYSTEM` 永远不可用（存不了凭据）。

### 2.3 部署脚本

把 `push_gx_log.ps1` 放到 `D:\脚本\push_gx_log.ps1`。

🔴 **文件必须是 UTF-8 带 BOM**（`utf-8-sig` + CRLF）。PS 5.1 对无 BOM 的 `.ps1` 按 ANSI(GBK) 解析 → 中文路径乱码 → 报"源目录不存在"。

### 2.4 注册定时任务

```cmd
schtasks /Create /TN "广西日志备份推送" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\脚本\push_gx_log.ps1" /SC DAILY /ST 02:00 /RU <源机服务账号> /RP * /RL HIGHEST /F
```

⚠️ `/RP` 与 `/RL` 之间、`/TR` 后面都要有空格，漏了 schtasks 会报参数无效但不一定提示。

---

## 三、脚本执行流程

1. **闸门**：读 `_gx_lastrun.txt`，距上次成功不足 60 小时 → 写一行"跳过"秒退
2. **压缩**：`tar -a -c -f <stage>\xxx.zip -C <父目录> <目录名>`（保留顶层目录结构）
3. **传输**：`robocopy <stage> \\192.0.2.88\<共享名> <zip>`，退出码 **≥8 视为失败**
4. **清理**：删暂存 zip、删目标端超过 30 天的旧包
5. **记账**：仅在传输成功后写时间戳（失败不记账 → 第二天自然重试）

**运行日志**：`D:\Cgo8\Log\_gx_push.log`
**手动补跑**：`powershell -NoProfile -File D:\脚本\push_gx_log.ps1 -Force`

---

## 四、排障（只留最关键的）

### 4.1 症状 → 定位 → 解法

| 现象 | 根因 | 解法 |
|---|---|---|
| 报"源目录不存在"（目录明明在） | `.ps1` 存成了无 BOM 的 UTF-8，PS 按 GBK 解析中文路径乱码 | 另存为 **UTF-8 带 BOM** |
| 压缩早就完成，却十几分钟不返回 | **磁盘 IO 排队**，不是脚本卡死（包越大越明显，283MB 包 0 秒、683MB 包 13 分钟） | 清 `_gx_stage` 残留、查 D 盘剩余空间、把 `-LogFile` 指到另一块盘 |
| 跑完报找不到网络路径 | 共享名 ≠ 文件夹名 | 目标机 `net share` 确认真实共享名 |
| 传输步骤失败、提示拒绝/无效 | 存 cmdkey 的账号 ≠ 任务 `/RU` 账号 | 用同一账号重存凭据 |
| robocopy 报 **错误 5（拒绝访问）**，退出码 8 | **共享权限 ∩ NTFS 权限取交集（取严）**，只改一处无效；或账号不存在于目标机、回落"来宾"变成只读 | `icacls "L:\广西日志备份" /grant logpush:(OI)(CI)F /T` + `net share 广西日志备份 /grant:logpush,full` |
| 445 连不上（最常见，见 4.2） | 三层过滤任一处拦了 | 按 4.2 走 |

### 4.2 445 连不上的三层定位（本次实战路径）

**第一步：源机测端口，拿错误类型**

```cmd
powershell -NoProfile -Command "$sw=[Diagnostics.Stopwatch]::StartNew(); $t=New-Object Net.Sockets.TcpClient; try{$t.Connect('192.0.2.88',445);'OPEN'}catch{'FAIL: '+$_.Exception.InnerException.SocketErrorCode}; $t.Close(); 'ElapsedMs=' + $sw.ElapsedMilliseconds"
```

| 回显 | 含义 |
|---|---|
| `OPEN` | 端口通，问题在共享名 |
| `ConnectionRefused`（秒回） | 主机可达、端口被拒 |
| `TimedOut`（约 21 秒） | **静默丢包** —— 包被丢，或根本没到。⚠️ Windows 防火墙默认就是 DROP，症状与"没到"完全一样 |

**第二步：看防火墙丢包日志，一刀切开防火墙 vs 安全组**

目标机开启丢包日志：

```cmd
netsh advfirewall set currentprofile logging droppedconnections enable
```

源机发一次连接后，目标机查：

```cmd
findstr /i 445 %systemroot%\system32\LogFiles\Firewall\pfirewall.log
```

| 日志结果 | 结论 | 下一步 |
|---|---|---|
| 有 `DROP ... 445 ... RECEIVE` | **Windows 防火墙丢的**（包已到网卡） | 查阻止规则 |
| 一行都没有 | 防火墙没见到包 | 查**华为云安全组**入方向 445 |

测完关掉：`netsh advfirewall set currentprofile logging droppedconnections disable`

**第三步：若为防火墙拦 —— 根因是「阻止规则优先级高于允许规则」**

目标机存在 **「关闭流行危险端口 - TCP / UDP」** 两条**阻止**规则，445 在内。
Windows 里 **阻止 > 允许**，所以单独加 allow 规则永远不生效。

Windows 防火墙**没有"排除某 IP"的原生语法**（那是 IPsec 才有的，且需域/证书环境，单机不可用）。唯一可行是**排除法**：

```cmd
rem ① 先备份安全基线
netsh advfirewall export %USERPROFILE%\fw_backup_20261028.wfw

rem ② 把 192.0.2.30 从阻止规则的地址范围里挖掉
netsh advfirewall firewall set rule name="关闭流行危险端口 - TCP" new remoteip=0.0.0.0-192.0.2.29,192.0.2.31-255.255.255.255

rem ③ 再补一条允许规则（缺一不可，否则落回默认 BlockInbound 仍丢）
netsh advfirewall firewall add rule name="Allow-SMB-445-From-192.0.2.30" dir=in action=allow protocol=TCP localport=445 remoteip=192.0.2.30 profile=any enable=yes
```

⚠️ ③ 之后必须回读验证：`netsh advfirewall firewall show rule name="Allow-SMB-445-From-192.0.2.30"`

### 4.3 确认二层可达（排除撞段/路由不通）

```cmd
arp -a | findstr 192.0.2.88
```

有动态 ARP 条目 ⇒ 同一广播域、ARP 解析成功 ⇒ **同网段直连成立**，撞段与路由问题可排除。

---

## 五、遗留事项

- 🔴 **445 属企业安全基线禁用端口**。本次开的是"来源受限例外"，**属改动基线，需报备**；且策略重新下发后可能被覆盖、备份会再次中断。
- 💡 **长期更优解：不碰 445，换传输通道** —— FTP(21)（Windows 自带服务 + 客户端 `ftp.exe` 脚本）或 SFTP(22)。若安全侧不允许开 445 例外，应直接转 FTP，改动只在传输那一段。
- 本机若装有第三方安全终端（如 HIPS），加完规则仍不通时需在其控制台加白名单。
