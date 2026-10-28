# Windows 双机日志自动推送 —— SMB 连通排障 SOP

> **场景**：生产 Windows Server 上的业务日志目录会持续增长，需要压缩归档后自动推送到同网段的另一台 Windows Server 长期保存。
> **约束**：不装任何第三方软件（只用系统内置 `tar.exe` / `robocopy` / `schtasks`），不落地 Python 依赖。
> **本文价值**：把整条链路上真实踩过的坑按「分层定位」的次序固化下来，避免下次在错误的方向上耗时间。

---

## 一、先想清楚：这件事的难点不在"压缩"

拆开看是四件独立的事，串起来才叫一条链路：

| 环节 | 技术点 | 出错时的典型表现 |
|---|---|---|
| **① 压缩** | `tar.exe` 把目录打成 zip，保留顶层目录结构 | 包体积异常大 / 目录层级丢失 |
| **② 传输** | SMB（TCP 445）跨机写共享目录 | 连接超时 / 拒绝访问 / 找不到网络路径 |
| **③ 调度** | Windows 任务计划 + 脚本内频率闸门 | 从不执行 / 每次都跑 / 执行时间漂移 |
| **④ 生命周期** | 目标端保留天数、失败不记账、暂存清理 | 磁盘被撑爆 / 失败一次要等两天半 |

**②是唯一会卡住整条链路的环节**，也是排查成本最高的一段。下面第六节是本文重点。

### 数据流

```
源机 (Windows Server)                                  目标机 (Windows Server)
┌──────────────────────────┐                            ┌──────────────────────┐
│ 业务日志目录              │                            │                      │
│   └─ tar.exe → 临时 zip  │─── SMB / TCP 445 ────────▶│ 共享目录             │
│        (本地暂存区)      │    (共享名 / 账号凭据)     │  └─ 按天保留 N 天    │
└──────────────────────────┘                            └──────────────────────┘
      任务计划 02:00 触发
      脚本内 60 小时闸门 → 实际 2.5 天一次
```

---

## 二、一次性准备

### 2.1 目标机

```cmd
net user logpush <强密码> /add /comment:"日志备份接收账号" /expires:never
net share 日志备份=L:\日志备份 /grant:logpush,full
net share                      ← 确认【共享名】真实是什么，与文件夹名可能不同
```

> ⚠️ **`net share` 给的只是"共享权限"**。真正决定能不能写文件的是 NTFS 权限，见第六节 6.5。

### 2.2 源机

```cmd
net user svc_logpush <强密码> /add /comment:"日志备份任务" /expires:never
```

用**将来跑任务的那个账号**保存目标机凭据：

```cmd
cmdkey /add:<目标机IP> /user:logpush /pass:<目标机 logpush 的密码>
```

> 🔴 **凭据按 Windows 账号隔离**——`cmdkey` 存的是"当前登录用户"的凭据。用 A 账号存、用 B 账号跑任务，压缩能成、传输出必失败。这是本环节最容易忽略的一条。

### 2.3 部署脚本

把脚本放到 `D:\Scripts\push_log_archive.ps1`。

> 🔴 **文件必须存成 UTF-8 带 BOM**（`utf-8-sig` + CRLF）。PowerShell 5.1 对无 BOM 的 `.ps1` 按 ANSI(GBK) 解析，脚本里的中文路径会变乱码，表现为**"源目录不存在"**——而目录明明就在那儿。用 VS Code 存的时候认准 "UTF-8 with BOM"。

### 2.4 注册定时任务

```cmd
schtasks /Create /TN "日志备份推送" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\Scripts\push_log_archive.ps1" /SC DAILY /ST 02:00 /RU svc_logpush /RP * /RL HIGHEST /F
```

| 参数 | 说明 |
|---|---|
| `/RU svc_logpush` | 填**存凭据的那个源机账号**，不是目标机的 logpush |
| `/RP *` | 星号＝交互式提示输入密码，密码不留在命令历史里。**不填则任务只在用户登录时运行**，服务器重启后无人登录就静默不执行 |
| `/RL HIGHEST` | 最高权限 |
| `/ST 02:00` | 每天凌晨 2 点触发（真正的 2.5 天由脚本闸门控制） |

注册后核验：

```cmd
schtasks /Query /TN "日志备份推送" /V /FO LIST
```

重点看「作为用户运行」是不是 `svc_logpush`、「计划任务状态」是不是就绪。

> ⚠️ `/RP` 与 `/RL` 之间、`/TR` 后面**都要有空格**，漏了 schtasks 会报「参数无效」但不一定给出明显提示。

---

## 三、脚本做了什么

```
1. 闸门    读 _lastrun.txt，距上次成功不足 60 小时 → 写一行"跳过"并秒退
2. 压缩    tar -a -c -f <暂存>\xxx.zip -C <父目录> <目录名>   （保留顶层目录结构）
3. 传输    robocopy <暂存> \\<目标机>\<共享名> <zip>          （退出码 ≥8 视为失败）
4. 清理    删本地暂存 zip、删目标端超过 N 天的旧包
5. 记账    仅在传输成功后写时间戳
```

三个设计要点：

- **先压到本地暂存区，再推网络路径**。压缩过程不直接写网络盘，避免把网络抖动混进压缩耗时里。
- **失败不写时间戳**。第二天 02:00 自动重试，不会"失败一次就等两天半"。
- **`robocopy` 退出码 0~7 全部算成功**，≥8 才是失败。写成 `if ($LASTEXITCODE -ne 0) { throw }` 会误判。

---

## 四、"每 2.5 天一次"怎么实现

Windows 任务计划**只支持分钟 / 小时 / 天 / 周 / 月**这类周期，**没有"每 2.5 天"**。

硬设 `/SC HOURLY /MO 60` 会**时间漂移**：02:00 → 14:00 → 02:00 交替，落不到固定凌晨。

所以采用 **每天 02:00 触发 + 脚本内 60 小时闸门**：

- 任务每天都启动脚本
- 脚本读状态文件，距上次成功不足 60 小时就写一行「跳过」并秒退（约 1 秒）
- 满 60 小时才真正压缩传输，成功后更新时间戳

好处：**永远落在凌晨执行**，频率精确 2.5 天，且失败不记账、次日自动重试。

手动强制补跑一次：

```cmd
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Scripts\push_log_archive.ps1 -Force
```

---

## 五、通用检查命令

| 目的 | 命令 |
|---|---|
| 凭据是否存上了 | `cmdkey /list:<目标机IP>` |
| 目标机共享列表 | `net view \\<目标机IP>` |
| 共享是否可访问 | `dir \\<目标机IP>\<共享名>` |
| 本机 IP 与网段 | `ipconfig /all` |
| 是否同网段直连 | `arp -a \| findstr <目标机IP>` |
| 任务是否存在 | `schtasks /Query /TN "日志备份推送" /V /FO LIST` |
| 立即试跑 | `schtasks /Run /TN "日志备份推送"` |

---

## 六、排障：SMB 连通分层定位（本文重点）

### 6.1 分层模型

连接失败时**按这个顺序**往下走，不要跳步：

```
① 链路层      目标机能 ping 通吗？ARP 能解析吗？        → 撞段 / 路由 / VLAN
② 云安全组    入方向规则放行了吗？（云主机才有这层）      → 云平台安全组
③ 主机防火墙  Windows 防火墙放行了吗？                    → 阻止 / 允许规则
④ 共享名      共享名和文件夹名是同一个吗？                → 名称不一致
⑤ 权限        共享权限和 NTFS 权限都给到账号了吗？        → 取交集，只给一处无效
```

### 6.2 第一步：测端口，拿到错误类型

`ping` 通不代表端口通，用 .NET 直接测 TCP 连接并**把错误类型打出来**：

```cmd
powershell -NoProfile -Command "$sw=[Diagnostics.Stopwatch]::StartNew(); $t=New-Object Net.Sockets.TcpClient; try{$t.Connect('<目标机IP>',445);'OPEN'}catch{'FAIL: '+$_.Exception.InnerException.SocketErrorCode}; $t.Close(); 'ElapsedMs=' + $sw.ElapsedMilliseconds"
```

| 回显 | 含义 | 下一步 |
|---|---|---|
| `OPEN` | 端口通 | 问题在共享名或权限，跳到 6.5 / 6.6 |
| `ConnectionRefused`（毫秒级返回） | 主机可达，但端口被主动拒绝 | 主机侧拒绝：服务没监听 / 防火墙 Reject |
| `TimedOut`（约 21 秒） | **静默丢包** | 往下看 6.3 |

> ⚠️ **`TimedOut` 无法区分"包没到"和"包到了被丢弃"**。因为 Windows 防火墙的默认动作就是 **DROP（静默丢弃）**，症状与"根本没送到"完全一样。必须靠 6.3 的日志来区分。

### 6.3 第二步：用防火墙丢包日志一刀切开

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
| 有 `DROP ... 445 ... RECEIVE` | **Windows 防火墙丢的**（包已到网卡） | 6.4 |
| 一行都没有 | 防火墙根本没见到包 | 查**云平台安全组**入方向规则 |

测完记得关掉，免得持续写盘：

```cmd
netsh advfirewall set currentprofile logging droppedconnections disable
```

### 6.4 第三步：防火墙拦的，根因通常是"阻止规则优先级高于允许规则"

Windows 防火墙规则里 **阻止（Block）优先于允许（Allow）**。所以很多"我已经加了允许规则怎么还是不通"的场景，根因是机器上存在一条**显式阻止规则**（例如企业安全基线里的"关闭流行危险端口"，把 135/137/138/139/445 这类一起封了）。

**关键约束：Windows 防火墙没有"排除某个 IP"的原生语法。** 界面上的「作用域 → 远程 IP 地址」只有"任何 / 下列 IP 地址"，往下列的是**要被这条规则命中的**地址；「已授权的计算机 / 例外」那个输入框是 **IPsec 身份验证主体选择器**（选域用户/计算机对象），**填不了 IP**，而且需要 IPsec 连接安全策略（依赖域或证书），单机无域环境用不了。

所以想让某一台机器例外，只能用**排除法**——把该 IP 从地址范围里挖掉：

```cmd
rem ① 先备份安全基线（出问题时可整体还原）
netsh advfirewall export %USERPROFILE%\fw_backup.wfw

rem ② 把源机 IP 从阻止规则的地址范围里挖掉
netsh advfirewall firewall set rule name="<阻止规则名>" new remoteip=0.0.0.0-192.0.2.29,192.0.2.31-255.255.255.255

rem ③ 再补一条允许规则（缺一不可，否则落回默认 BlockInbound 仍丢）
netsh advfirewall firewall add rule name="Allow-SMB-445-From-<源机IP>" dir=in action=allow protocol=TCP localport=445 remoteip=<源机IP> profile=any enable=yes
```

改完**必须回读验证**：

```cmd
netsh advfirewall firewall show rule name="Allow-SMB-445-From-<源机IP>"
```

要看到「已启用：是」「配置文件：任何」「本地端口：445」三项都对。

> 🔴 **这是改动企业安全基线，务必报备**。更根本的问题是：445 被列为危险端口是有道理的（勒索软件头号入口），长期看应该换传输通道而不是长期开例外——见第八节。

### 6.5 第四步：能列目录但写不进去 —— 共享权限 ∩ NTFS 权限

典型症状：脚本的连通性检查通过（能列目录），`robocopy` 却报

```
错误 5 (0x00000005) 无法写入目标目录 ... 由于以下错误，Robocopy 未复制任何文件
```

**错误 5 = 拒绝访问**，而 robocopy 退出码 **8 = 有文件复制失败**。

根因在 Windows 的权限模型：**最终权限 = 共享权限 ∩ NTFS 权限（默认取严）**。只改一处等于没改。

- 共享权限查：`net share <共享名>`
- NTFS 权限查：`icacls "L:\日志备份"`

典型翻车现场：共享权限给了 `Everyone, FULL`（所以能枚举目录），但 NTFS 权限是**显式设置**的、只列了 `Administrators` 和 `SYSTEM`（注意没有 `(I)` 继承标记，说明没继承父目录），账号压根不在里面 → 写入被拒。

修复：

```cmd
net share <共享名> /grant:<账号>,full
icacls "L:\日志备份" /grant <账号>:(OI)(CI)F /T
```

`/T` 让目录里**已存在**的文件和子目录一起生效，否则新权限只对以后新建的管用。

> 🔴 顺带两个必做项：服务账号用 `net user <账号> /passwordchg:no` 关掉"下次登录必须更改密码"（某些认证路径会因此被拒，典型错误 1907）；共享权限的 `Everyone` 应收紧成指定账号——那等于任何人可写可删你的归档包。
>
> 另外，如果账号其实建在了**另一台机器**上，目标机认不出就会回落"来宾"访问，通常表现为**只读**，症状同样是错误 5。用 `net user <账号>` 确认账号真实存在于目标机。

### 6.6 症状速查表

| 现象 | 根因 | 处理 |
|---|---|---|
| 报"源目录不存在"，路径看着像乱码 | `.ps1` 存成无 BOM 的 UTF-8，PS 按 GBK 解析 | 另存为 **UTF-8 带 BOM** |
| 压缩早就完成，却十几分钟不返回 | **磁盘 IO 排队**，不是脚本卡死（包越大越明显） | 清暂存区残留、查磁盘剩余空间、把日志文件指到另一块盘 |
| 找不到网络路径 | 共享名 ≠ 文件夹名 | 目标机 `net share` 确认真实共享名 |
| 连接超时（21 秒） | 静默丢包 | 按 6.2 → 6.3 走 |
| 错误 5 / 退出码 8 | 权限取交集 | 见 6.5 |
| 压缩正常、传输失败 | 存 cmdkey 的账号 ≠ 任务 `/RU` 账号 | 用同一账号重存凭据 |
| 目标路径不可访问 | 共享未开 / 账号无写权限 | 检查 `net share` 与 `icacls` |
| 任务从没执行过 | `/RP` 没填，只在登录时运行 | 重建任务并填 `/RP *` |
| 每次都写"本次跳过" | 距上次成功不足 60 小时 | 正常行为；要立刻跑加 `-Force` |
| 日志里 robocopy 那几行是乱码 | robocopy 用 OEM/GBK 写、脚本用 UTF-8 写，同一文件两种编码 | robocopy 日志分流到独立文件，主日志保持纯 UTF-8 |

---

## 七、附带踩到的三个"环境级"坑

### 7.1 PowerShell 别名劫持

PowerShell 里很多内置命令名被别名覆盖，参数语义完全不同：

| 想执行 | 直接敲会变成 | 必须写 |
|---|---|---|
| `sc`（服务控制） | `Set-Content` | **`sc.exe`** |
| `curl` | `Invoke-WebRequest` | `curl.exe` |
| `rm` | `Remove-Item`（参数不同） | `Remove-Item`（显式写，别用别名） |
| `ls` / `cat` / `sort` / `select` | `Get-ChildItem` / `Get-Content` / `Sort-Object` / `Select-Object` | 显式写 cmdlet |

**给 Windows 命令之前，先在脑子里按目标 shell 解析一遍**，确认不会被别名或参数差异吞掉。不确定就优先给原生 cmdlet 版。

### 7.2 老机器（PS 2.0 / 2008 R2 级）默认给 CMD

`Get-CimInstance` / `Get-WinEvent -FilterHashtable` 这类是 PowerShell 3.0+ 才有的，PS 2.0 上直接报 `CommandNotFoundException`。老机一律给 CMD 命令（`sc query` / `sc qc` / `net share` / `icacls` / `netsh`），没有别名劫持也没有版本问题。

### 7.3 脚本文件本身是 UTF-8 BOM，还是日志是 UTF-8

两件事分开：`.ps1` **必须是 UTF-8 带 BOM**（否则路径乱码）；而运行日志若要人读，就要**纯 UTF-8**（robocopy 自己的输出用控制台代码页，别和脚本输出混在一个文件里）。

---

## 八、选型延伸：445 不是唯一解，也未必是最好的解

445 被写进"流行危险端口"黑名单，是有客观原因的。更稳妥的长期方案是换传输通道：

| 方案 | 端口 | 代价 |
|---|---|---|
| **FTP** | 21 | Windows 自带 FTP 服务 + 客户端 `ftp.exe`，脚本化上传，无需第三方软件 |
| **SFTP / SCP** | 22 | 目标机装 OpenSSH Server，源机用 `pscp.exe`（单文件） |

迁移时**只需要替换"传输"那一段**，压缩、清理、记账逻辑完全不动。

---

## 九、一句话总结

> 这条链路上，**压缩和调度都是可预测的，不可预测的只有 SMB 连通**。
> 排查的唯一正确姿势是**分层**：先用错误类型判断"包到没到"，再用丢包日志区分"谁丢的"，最后才动规则和权限。
> 反过来——先加允许规则、先改共享、先怀疑密码——都是白费时间。
