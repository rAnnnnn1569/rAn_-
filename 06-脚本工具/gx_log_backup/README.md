# 广西第三方监管日志 —— 定时打包推送

纯 Windows 内置组件（PowerShell + tar.exe + robocopy），**不依赖 Python**。

| 项 | 值 |
|---|---|
| 源目录 | `D:\Cgo8\Log\Top809External\中-广西第三方监管平台_171-105-30-67-9100zw` |
| 目标 | `\\192.0.2.88\广西日志备份` |
| 产物 | `广西第三方监管_yyyyMMdd_HHmm.zip`，目标端保留最近 30 天 |
| 触发 | 每天 02:00 启动，**脚本内 60 小时闸门**控制为实际每 2.5 天一次 |
| 运行日志 | `D:\Cgo8\Log\_gx_push.log` |
| 压缩暂存 | `D:\Cgo8\Log\_gx_stage`（正常流程传完即删） |
| 执行时间戳 | `D:\Cgo8\Log\_gx_lastrun.txt` |

> 脚本放 `D:\Scripts\` 这种纯英文路径最稳，省掉中文路径在 cmd 里的编码麻烦。

---

## 一、目标机 192.0.2.88 上（一次性）

管理员 CMD：

```
net user logpush <强密码> /add /comment:"日志备份接收账号" /expires:never
net share 广西日志备份=L:\广西日志备份 /grant:logpush,CHANGE
```

- 这里建的 **`logpush` 是目标机账号**，只负责「允许别人往共享里写」。
- 共享名用中文没问题；若改成英文共享名，同步改脚本 `-Target`。
- 确认 445 端口通（同网段一般默认通）。

## 二、源服务器上（一次性）

### 1. 建一个跑任务的专用账号（推荐）

```
net user svc_logpush <强密码> /add /comment:"日志备份任务" /expires:never
```

> **别用 Administrator 之外的临时个人账号**——改密码就断，届时报错很难查。
> **绝对不能用 `SYSTEM`**：SYSTEM 无法保存 `cmdkey` 凭据，压缩能成、传输必失败。

### 2. 用这个账号登录一次，保存目标机凭据

```
cmdkey /add:192.0.2.88 /user:logpush /pass:<目标机 logpush 的密码>
```

🔴 **凭据按 Windows 账号隔离**。这一步必须由「将来跑任务的那个账号」执行。
换个账号跑任务，凭据读不到，症状是：压缩正常、卡在「传输中」然后失败。

### 3. 手动验证两步

```
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Scripts\push_gx_log.ps1 -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Scripts\push_gx_log.ps1
```

第一条只压缩不传输，用来确认中文路径能读到、包有多大；第二条应输出「传输完成」，目标目录出现 zip。

## 三、注册定时任务

管理员 CMD（**注意 `/TR` 后、`/RP` 与 `/RL` 之间都要有空格**）：

```
schtasks /Create /TN "广西日志备份推送" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\Scripts\push_gx_log.ps1" /SC DAILY /ST 02:00 /RU svc_logpush /RP * /RL HIGHEST /F
```

| 参数 | 说明 |
|---|---|
| `/RU svc_logpush` | 填**第二步保存凭据的那个源机账号**，不是目标机的 logpush |
| `/RP *` | 星号＝交互式提示输入密码，密码不会留在命令历史里 |
| `/RL HIGHEST` | 最高权限运行 |
| `/ST 02:00` | 每天凌晨 2 点触发（真正的 2.5 天由脚本闸门控制） |

**`/RP` 必须填。** 只有存了密码，任务才能「不管用户是否登录都运行」；不填的话默认只在用户登录时运行，服务器重启后无人登录就会静默不执行。

注册后检查：

```
schtasks /Query /TN "广西日志备份推送" /V /FO LIST
```

重点看两项：`作为用户运行` 是否是 `svc_logpush`、`计划任务状态` 是否就绪。

立即试跑一次：

```
schtasks /Run /TN "广西日志备份推送"
```

## 四、频率：为什么是「每天触发、实际 2.5 天」

Windows 任务计划**只支持分钟/小时/天/周/月**这类周期，没有「每 2.5 天」。

若硬设 `/SC HOURLY /MO 60`（每 60 小时），触发时间会**漂移**：02:00 → 14:00 → 02:00 交替，落不到固定凌晨。

所以采用 **每天 02:00 触发 + 脚本内 60 小时闸门**：

- 任务每天 02:00 都会启动脚本
- 脚本读 `_gx_lastrun.txt`，距上次成功不足 60 小时就写一行「跳过」并秒退（约 1 秒）
- 满 60 小时才真正压缩传输，成功后更新时间戳

好处：**永远落在凌晨执行**，频率精确 2.5 天；**失败时不写时间戳**，第二天 02:00 自动重试，不会「失败一次等两天半」。

手动强制补跑一次（不受间隔限制）：

```
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Scripts\push_gx_log.ps1 -Force
```

## 五、可调参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-Source` | 上述源目录 | 要打包的目录 |
| `-Target` | `\\192.0.2.88\广西日志备份` | UNC 目标 |
| `-Stage` | `D:\Cgo8\Log\_gx_stage` | 本地压缩暂存 |
| `-LogFile` | `D:\Cgo8\Log\_gx_push.log` | 运行日志 |
| `-KeepDays` | `30` | 目标端保留天数 |
| `-MinIntervalHours` | `60` | 与上次成功至少间隔多少小时，不足则跳过（60 小时＝2.5 天） |
| `-StateFile` | `D:\Cgo8\Log\_gx_lastrun.txt` | 记录上次成功时间 |
| `-Force` | 关 | 无视间隔，强制执行一次 |
| `-DryRun` | 关 | 只压缩不传输，且留下 zip 供检查 |

## 六、必须注意的五点

1. **脚本文件必须是 UTF-8 带 BOM**。PS 5.1 对无 BOM 的 .ps1 按 ANSI(GBK) 解析，中文路径变乱码，报「源目录不存在」。
2. **`-DryRun` 不会删暂存 zip**（故意的，方便你检查内容）。连跑几次 `_gx_stage` 里会堆大文件，记得手动清：
   ```
   del /q D:\Cgo8\Log\_gx_stage\*.zip
   ```
3. **当前是整目录全量打包**。目录天天增长，压缩耗时与体积线性上升（实测 683 MB 包需近 4 分钟）。若目录按天分子文件夹，应改增量。
4. **压缩完成后可能长时间无输出**，这不是卡死。680 MB 级别要回写磁盘，若日志文件和 zip 在同一块盘，`Add-Content` 写日志会排队（实测出现过 13 分钟）。把 `-LogFile` 指到另一块盘可缓解。
5. **不要把密码写进脚本或任务参数**。只走 `cmdkey`，密码存在本机凭据管理器。

## 七、常见报错对照

| 现象 | 原因 | 处理 |
|---|---|---|
| 报「源目录不存在」，路径看起来是乱码 | 脚本被存成无 BOM 的 UTF-8 | 另存为 UTF-8 带 BOM |
| 压缩成功，卡在「传输中」后失败 | 跑任务的账号没存 `cmdkey` 凭据 | 用同一账号重新 `cmdkey /add` |
| 报「目标路径不可访问」 | 88 上共享未开 / 账号无写权限 | 检查 `net share` 与 `/grant` |
| 任务从没执行过 | `/RP` 没填，只在登录时运行 | 重建任务并填 `/RP *` |
| `schtasks` 报「参数无效」 | `/RP *` 与 `/RL` 之间漏空格 | 按第三节的正确命令重写 |
| 每次跑都写「本次跳过」 | 距上次成功不足 60 小时 | 正常行为；要立刻跑加 `-Force` |

## 八、降级条件

若源机是 2008 R2 且 PowerShell 仍为 2.0（`$PSVersionTable.PSVersion` 显示 2.x）：

- `tar.exe` 和 `Compress-Archive` 都不可用
- 两条路：升级 WMF 5.1，或改用 `makecab`（系统自带，压缩率较差、产物是 .cab）
