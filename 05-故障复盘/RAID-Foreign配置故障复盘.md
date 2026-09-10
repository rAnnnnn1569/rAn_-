# 生产服务器"不可达"故障复盘（2026-09-01）

> 结论先行：根因是 **PERC H330 控制器 NVRAM 中的 RAID 配置与硬盘 DDF 元数据不匹配，导致磁盘被标记为 Foreign、虚拟盘未组装**，BIOS 找不到任何可启动设备，操作系统从未加载。

---

## 一、事故概述

| 项目 | 内容 |
|---|---|
| 设备 | 生产服务器（Dell PowerEdge 物理机，托管于机房） |
| 网络 | 内网 `192.0.2.10`，公网 `203.0.113.10` |
| 承载业务 | 808 代理网关（代理网关程序 容器）+ 809 DNAT/SNAT 中转（6 组上级监管平台链路） |
| 现象 | SSH(22) / ping / telnet **三者全不通** |
| 处置结果 | 在 Lifecycle Controller 提示处**按 F 导入 Foreign 配置**即恢复，未重装、未丢数据 |
| 业务影响 | 809 侧多条上级监管平台链路全部断链（其中部分上级平台有 IP 白名单管控） |

---

## 二、根因链条（自上而下六层）

| 层 | 状态 | 如何观测到这一层 |
|---|---|---|
| 1. 现象 | ping / SSH / telnet 全不通 | 业务侧反推：`curl 203.0.113.10:7822/api/monitor` 是否有响应 |
| 2. 网络 | 网络协议栈根本没有运行 | 三者全不通 → 可排除"仅禁 ICMP"分支 |
| 3. 系统 | 操作系统未加载，POST 后停在启动失败画面 | iDRAC KVM 或机房直看屏幕（**托管物理机的关键通道**） |
| 4. 启动 | `No boot device available or Operating System detected` | POST 画面关键字直接指向启动层 |
| 5. 存储 | `0 Virtual Drive(s)`，RAID 卷未组装 | F10 Lifecycle Controller → PERC 配置 |
| 6. **根因** | **硬盘 State = Foreign** | PERC Physical Disk Management |

### 机制解释

PERC 控制器在**两处**保存 RAID 配置：

1. 控制器自身的 NVRAM
2. 阵列中**每块物理盘**上的 SNIA DDF 元数据区（记录 RAID 级别、条带大小、盘序、奇偶校验轮转）

开机时控制器比对两处记录。**只要不一致，就把盘标记为 Foreign** —— 这是**元数据不匹配，不是物理故障**。盘上数据完好无损，只是控制器拒绝自动组装虚拟盘。

---

## 三、根因定位方法论（可复用）

### 3.1 分层模型要补上"主机状态层"

常规网络排障的分层是 应用 → 传输 → 网络 → 链路 → 物理。但这次故障点在**分层模型之外**：

```
应用/传输层（SSH 22）
网络层（ICMP）
链路层
────────── 常规排障到此为止 ──────────
操作系统是否加载      ← 本次故障区
固件/POST 是否通过
存储/RAID 是否组装    ← 根因所在
```

**判据**：ping + TCP(SSH) + telnet 三者全不通时，第一假设不应是"网络策略拦了"，而应是"主机有没有跑起来"。因为若机器活着只禁 ICMP，telnet 22 仍能握手。

### 3.2 先补信息，再收敛假设

"ping 不通"这一个现象能推出十种可能，逐一列举成本极高。正确做法是**先找信息量最大、获取成本最低的观测点**：

| 观测点 | 成本 | 信息量 |
|---|---|---|
| tracert / 换网络复测 | 低 | 中（只能排除本端） |
| 7822 监控接口 | 低 | 中（能判断主机死活） |
| **POST 画面 / iDRAC KVM** | 中 | **极高（假设空间从 10 砍到 2）** |

这次若一开始就通过 iDRAC 看到屏幕，可直接跳过全部网络排查。

### 3.3 "找不到设备" ≠ "设备坏了"

必须区分两种状态：

| 类别 | 表现 | 含义 | 处置 |
|---|---|---|---|
| **逻辑层未导入** | Physical Disk Mgmt 中**能看到盘**，State = Foreign | 数据在，元数据不匹配 | 按 F 导入，30 秒，无损 |
| **物理层不可见** | Physical Disk Mgmt 中**看不到盘** | 盘掉线 / SAS 线松 / 背板故障 | 检查硬件连接 |

**关键判断动作**：先进 Physical Disk Management 看盘在不在。盘在 → 数据就在。

### 3.4 处置方案必须从无损到破坏性排序

| 方案 | 性质 | 耗时 | 数据风险 |
|---|---|---|---|
| 按 F 导入 Foreign | **无损** | 30 秒 | 无 |
| Rescue 模式修 GRUB | 基本无损 | 30 分钟 | 低 |
| 重装系统 + 恢复程序 | **破坏性** | 8 小时+ | 高 |

**这次若直接按重装方案执行，将真实丢失**：`/opt/app/src/转发程序/`（本地全盘搜索确认**无任何备份**）及其 appsettings.json 中的上级平台 IP、端口、接入码与接入密钥。

### 3.5 绝对禁操作

在 PERC 导入确认框中，**Import 与 Clear 方向相反**：

- **Import** = 把盘上 DDF 元数据写入控制器 NVRAM → 保留数据
- **Clear** = 擦除盘上 DDF 元数据，盘退回 Unconfigured Good → **虚拟盘路由被摧毁，数据不可访问**

不确定时两个都不选，退出并保留现场。

---

## 四、过程中的两次误判（教训）

### 误判一：按"网络层问题"展开排查

- **原因**：把"主机在运行"当成了隐含前提，未加验证。
- **代价**：产出了整套 tracert / 换网络复测 / 借其他服务器当探针的方案，全部无效。
- **修正**：拿到 POST 画面后推翻。

### 误判二：判定"找不到启动设备 = 盘坏了/RAID 掉了"，直接规划重装

- **原因**：`All of the disks from your previous configuration are gone` 这句提示字面吓人，但它其实是 PERC 在所有元数据都标为 Foreign 时的**通用提示**，不代表物理掉盘。
- **代价**：产出 8 步重装方案并做了备份资产盘点（抢救数据、RAID 配置、换源、装 docker、恢复程序目录等）。
- **修正**：拿到 PERC 画面看到 `Foreign Configuration(s) found on adapter` 后推翻。

### 误判中的正资产

备份资产盘点虽未用上，但产出了两条长期有价值的事实：

1. `H:\代理网关程序260227.tar`（139MB / 135 文件 / 2026-02-27）= **代理网关程序 程序目录完整备份**，含三份 appsettings，此前未登记。
2. 确认 `转发程序` **本地无备份** —— 这是真实风险敞口，本次侥幸未触发。

**结论**：恢复能力盘点本身是对的，但应在"决定是否重装"**之前**做，用于评估重装代价；而不是在决定重装之后做。

---

## 五、尚未闭环：什么触发了 Foreign（本次最大遗留）

**按 F 只消除了症状，没有消除根因。** Dell 官方 KB（Defect ID 225870）明确表述：

> 驱动器处于外部状态是**更大问题的征兆，它并不是问题所在**。首先应该对 PERC 适配器将驱动器置于外部状态的原因进行故障处理……如果没有发现 SAS 连接重置的根本原因，则驱动器**可能会再次处于外部状态**。

### 候选原因（按本机硬件特性排序）

本机 RAID 卡为 **PERC H330**，Dell 官方规格确认其为**入门级卡：无备用电池组（BBU=否）、无非易失性高速缓存、仅支持直写与不预读**，采用 LSI SAS3008 芯片。

⚠️ **修正一处常见误传**：通用资料常把"RAID 卡电池/电容掉电"列为 Foreign 诱因，**这一条对 H330 不适用**（它根本没有 BBU）。本机应重点排查以下项：

| 优先级 | 候选原因 | 排查方法 |
|---|---|---|
| 1 | 硬盘被抽插或槽位变动（清灰、挪机、换风扇时碰到托架） | 询问机房近期有无操作；对比盘位 |
| 2 | SAS 线缆松动 / 背板接触不良 | 重新插拔 SAS 线；看 iDRAC SEL 日志有无 SAS 链路重置 |
| 3 | 异常断电或不正确的重启 / 电源循环 | 查机房供电记录、iDRAC 电源事件日志 |
| 4 | 电源相关事件（PSU 故障、断电） | iDRAC 硬件日志 |
| 5 | 控制器固件异常（当前 FW 25.3.0.0016） | 与 Dell 支持矩阵比对，评估是否需升级 |

### 定位真实根因的动作

```bash
# 1. 查看 iDRAC 硬件事件日志（SEL），能给出确切时间与事件类型
racadm getsel -i
# 或在 iDRAC Web 界面：维护 → 系统事件日志

# 2. 只读方式查看 Foreign 配置（不触发导入/清除）
perccli /c0/fall show all
perccli /c0 /eall /sall show      # 区分 UGood 与 Foreign 状态

# 3. 确认虚拟盘当前状态（Optimal / Degraded）
perccli /c0 /vall show

# 4. 确认物理盘数量与健康
perccli /c0 /eall /sall show all
```

---

## 六、后续改进措施

| # | 措施 | 目的 |
|---|---|---|
| 1 | **配置 iDRAC 并接入监控**（IP/账号/SNMP trap → zabbix） | 硬件事件主动告警，无需等 SSH 断了才发现 |
| 2 | **将 RAID 状态纳入定期巡检**（`perccli /c0 /vall show` 检查是否为 Optimal） | 在盘掉线变成启动故障前发现 |
| 3 | **补齐 `转发程序` 备份** | 当前唯一无备份的关键资产，含上级平台接入密钥 |
| 4 | **将本次处置固化为 runbook** | 下次遇到 Dell POST 启动失败可按图索骥 |
| 5 | **评估 27 单点风险** | 809 多条链路全经此机，无冗余；考虑迁移备案 IP |
| 6 | 系统盘与数据盘分区分离（`/opt/app` 独立分区） | 未来即使重装系统也不动程序文件 |

---

## 七、本次可复用的速查：Dell POST 启动失败画面关键字

| 屏幕文字 | 含义 |
|---|---|
| `Booting from iBA XE Slot 0101` | 主板跳过硬盘，尝试从网卡 PXE 启动 |
| `PXE-E61: Media test failure, check cable` | PXE 启动失败 |
| `No boot device available or Operating System detected` | 无任何可启动设备（关键告警） |
| `Current boot mode is set to BIOS` | 传统 BIOS 模式（非 UEFI） |
| `Foreign Configuration(s) found on adapter` | **盘数据在，RAID 元数据未导入** |
| `Press any key to continue or 'F' to import` | **按 F 导入即可恢复** |
| `All of the disks from your previous configuration are gone` | 元数据全为 Foreign 时的通用提示，**非物理掉盘** |
| `0 Virtual Drive(s)` | 当前无 RAID 卷 |

按键功能：`F1` 重试启动 / `F2` 进 BIOS / `F10` Lifecycle Controller / `F11` Boot Manager / `Ctrl+R` PERC 配置。

---

## 八、待确认事项

1. 导入后虚拟盘状态是 **Optimal 还是 Degraded**？（决定是否需要补盘）
2. 机箱内实际插了几块盘？是否只有 Disk ID 0 一块？
3. **事故前机房是否有过操作**（清灰、搬动、换硬件、断电）？
4. iDRAC SEL 日志中，Foreign 事件发生的具体时间与前置事件是什么？
5. 事故持续多长时间？809 侧有多少条链路断链、涉及多少车辆？
6. 该机是否已纳入 zabbix 监控？iDRAC 是否已配置？

---

## 根因定位补充（2026-09-02，取证输出分析后）

> 上文复盘时 Foreign 已按 F 消除，但"为什么会变 Foreign"未闭合。本节基于第二轮远程取证输出与厂商官方文档，给出根因结论。

### 一、三条硬证据

| # | 证据 | 出处 | 性质 |
|---|---|---|---|
| 1 | LBA **539737368** 在 2026-08-08 07:19:05 与 2026-09-01 17:49:18 两次报 `Sense Key : Medium Error`（CDB 均为 `28 00 20 2b bd 18`）| `/var/log/messages-20260809` + `dmesg` | 已确认 |
| 2 | 虚拟盘 `RAID Level : Primary-0, Secondary-0, Qualifier-0` = **RAID 0**，931.0 GB，仅 Slot 0 一块盘 | `MegaCli64 -LDInfo/-PdList` | 已确认 |
| 3 | 运行内核 **3.10.0-327.el7.x86_64**（CentOS 7.2 GA，2015-11），用户态却已是 **7.9.2009** | `uname -r` / `cat /etc/centos-release` | 已确认 |

**证据 1 的关键含义**：同一 LBA 反复报错 = **该坏道从未被重映射**。SAS 盘的坏块重映射**只在写入时触发**，只读不重映射（serverfault 共识），因此这个坏道是持久性的，每次读到必报错。

**证据 2 的关键含义**：Cisco 官方口径 —— **RAID 0 下控制器无法修复介质错误**（没有冗余数据可写回该 LBA）。这解释了输出中一个反常现象：`Media Error Count: 0` 而内核却报 Medium Error —— 控制器不做修复，直接把错误透传给操作系统。

**证据 3 的关键含义**：Red Hat 官方 KB `solutions/3010132` —— megaraid_sas 驱动在等待控制器响应 **175 秒**后强制 reset 控制器，症状为 `megasas: [ N]waiting for N commands to complete for scsi0` → `megaraid_sas: resetting fusion adapter scsi0`，后果是**所有磁盘 I/O 中断、文件系统只读或挂起、严重时系统宕机**。其受影响范围明文包含：

> all version of 7.2 prior to **kernel-3.10.0-327.49.2.el7**

本机 `3.10.0-327.el7` 即 .0 版，**正落在缺陷区间内**。

### 二、根因链条

1. 磁盘出现物理坏道（8-08 首现，LBA 539737368）
2. 读到该 LBA 时 SAS 盘进入错误恢复，控制器长时间不响应
3. **megaraid_sas 驱动（7.2 GA 内核）等 175 秒后强制 reset 控制器** ← 已确认缺陷
4. 控制器 reset → 全部磁盘 I/O 中断 → 系统 hang
5. 强制重启 → 控制器 NVRAM 与盘上 DDF 元数据不一致 → 盘标 **Foreign** → 无虚拟盘 → 无启动设备 → OS 未加载 → 网络协议栈不存在 → ping/SSH/telnet 全不通
6. **RAID 0 单盘零冗余放大了后果**：坏道无从修复，直透 OS

这与 Dell 官方立场一致 —— Foreign 是"SAS 连接重置"的**症状**，不是根因。

### 三、佐证与竞争假设

**支持本链条的佐证**：
- `last -F reboot` 中 7-23 与 9-01 两次启动的结束时间都算到"当前"，呈 still running 状 → **缺少配对 shutdown 记录**，是异常终止的典型特征
- `messages-20260816 / 20260823 / 20260830` 三个归档中磁盘类关键字**全空**（8-08 有）→ 崩溃前无前兆错误，符合"系统 hang 后日志戛然而止"

**尚未排除的竞争假设**：
- **机房异常掉电 / PSU 故障** —— 日志中断空档同样支持此假设，需 SEL 日志区分
- **人为抽插盘 / SAS 线松动** —— 需向机房核实

### 四、把结论钉死的四条命令

```bash
# 1. 硬件事件日志（决定性：区分掉电与链路重置）
yum install -y ipmitool OpenIPMI
modprobe ipmi_msghandler ipmi_devintf ipmi_si
ipmitool sel time get          # 先确认 BMC 时间可信
ipmitool sel elist | tail -60  # 看 Foreign 事件的前一条是什么

# 2. 物理盘 SMART —— 注意 MegaRAID 必须 -d megaraid,N
MegaCli64 -PdList -aAll | grep -i "Device Id"     # 先取 N（不是 Slot Number！）
smartctl -a -d megaraid,<N> /dev/sda | grep -iE \
  "Reallocated_Sector_Ct|Current_Pending_Sector|Offline_Uncorrectable|Power_On_Hours"

# 3. 日志中断点（能把"系统何时 hang"精确到分钟）
for f in /var/log/messages /var/log/messages-*; do echo "$f  $(tail -1 $f | cut -c1-15)"; done

# 4. 内核为何停在 7.2 GA
rpm -qa | grep -E "^kernel-[0-9]" | sort
grub2-editenv list
grep -i exclude /etc/yum.conf
modinfo megaraid_sas | grep -E "^version"
```

### 五、据此应推进的处置

| 优先级 | 事项 | 理由 |
|---|---|---|
| P0 | **立即备份 `/opt/app/src/`** | 单盘 RAID 0 + 已确认坏道，随时可能彻底不可读 |
| P0 | **更换硬盘并改为 RAID 1** | 消除零冗余，且坏道可随重建被重写修复 |
| P1 | **升级内核至 3.10.0-327.49.2.el7 以上**（建议直接上 7.9 的 3.10.0-1160 系列）| 摆脱已确认的驱动缺陷 |
| P1 | 补 `转发程序` 备份、iDRAC 接入监控 | 既有待办，风险未变 |
| P2 | 评估 27 单点风险 | 809 六链路全经此机，无冗余 |

---

## 根因定位补充二（2026-09-02 第二轮：SEL 硬件事件日志）

> 上一节留下两个竞争假设（机房掉电 / 人为抽盘）未排除。本节读到了 **iDRAC SEL**，两个假设**全部排除**，并拿到了硬件层的直接证据。

### 一、决定性证据：SEL 里的 Drive Fault

```
31 | 08/31/2026 | 21:09:00 | Drive Slot / Bay Drive 0 | Drive Fault () | Asserted
32 | 08/31/2026 | 21:12:45 | Drive Slot / Bay Drive 0 | Drive Fault () | Deasserted
```

**为什么这条是决定性的**：SEL 由 BMC（iDRAC）独立记录，**不依赖操作系统**。操作系统已经 hang 死、内核日志戛然而止，BMC 照样在记。它的证据等级高于内核日志。

**它说明了什么**：

| 观察 | 推论 |
|---|---|
| 故障对象是 **Drive 0** | 正是 RAID 0 里唯一那块盘（ST91000640SS），与前文证据 2 完全对上 |
| **只持续 3 分 45 秒就 Deassert** | 盘**没有物理报废**，而是"卡在深度错误恢复里被判死、随后又活过来" —— 这正是「坏道 + 控制器复位」的典型表现，不是掉电、不是被拔 |
| 事件类型只有 Drive Fault，无 Power / PSU 前置事件 | 排除电源侧诱因 |

### 二、【易踩坑】SEL 时间戳必须做时钟校准

同一批次取到的两个时间：

```
ipmitool sel time get   →  09/02/2026 01:21:04
/var/log/messages 末行  →  Sep  2 09:21:03
```

**BMC 跑 UTC，操作系统跑 UTC+8，整整差 8 小时。** 直接把 `08/31 21:09` 当北京时间读，会与内核日志完全对不上，进而怀疑整个分析。校准后：

> **Drive 0 Fault = 2026-09-01 05:09:00（北京时间）**；Deassert = 05:12:45

（v3 取证脚本已内置自动换算，见 `docs/源码/27远程硬件取证_readonly.sh` 第 5 节。）

### 三、被排除掉的假设（价值与证实同样大）

SEL 从 **2022-08-20 到 2026-08-31 整整四年零事件**：

| 假设 | 结论 | 依据 |
|---|---|---|
| 机房异常掉电 / PSU 故障 | **排除** | 无任何 `AC Power Loss` / `Power Supply` / `Power Unit` 事件 |
| 人为抽盘 / 清灰碰托架 / SAS 线松 | **排除** | 2026 年无 `Chassis Intrusion`、无盘移除插入事件 |
| 过热 | **排除** | 2026 年无 `Temperature` / `Fan` 告警（仅 2022-08-20 有三次进风口 42℃） |
| 内存不可纠正 ECC 引发本次 hang | **排除** | 内存事件全部集中在 2021-12 / 2022-02，事故期间无新事件，dmesg 无 MCE |

### 四、新挖出的两个隐患

**1. DIMMB1 有不可纠正 ECC（第二颗定时炸弹）**

```
19 | 12/16/2021 03:01:15 | Memory Mem ECC Warning | Transition to Non-critical from OK
1a | 12/16/2021 03:01:16 | Memory Mem ECC Warning | Transition to Critical from less severe
22 | 02/14/2022 00:43:16 | Memory ECC Uncorr Err  | Uncorrectable ECC (DIMMB1)
24 | 02/14/2022 00:43:16 | Memory ECC Uncorr Err  | Uncorrectable ECC (DIMMB1)
26 | 02/14/2022 01:27:01 | Memory ECC Uncorr Err  | Uncorrectable ECC (DIMMB1)
28 | 02/14/2022 01:27:01 | Memory ECC Uncorr Err  | Uncorrectable ECC (DIMMB1)
```

不可纠正 ECC 会直接触发 MCE → 内核 panic 或静默挂起。**它不是本次事故的原因**（事故期间无新事件），但 2022-02 之后再无开箱记录（无 Chassis Intrusion），意味着这块内存**很可能还在机器上**。必须查当前 EDAC 的 `ue_count` 决定是否更换。

**2. `/etc/yum.conf` 里写着 `exclude=kernel*` —— 这是内核卡在 2015 年的直接原因**

```
kernel-3.10.0-327.el7.x86_64        ← 只装了一个内核包
saved_entry=CentOS Linux (3.10.0-327.el7.x86_64) 7 (Core)
exclude=kernel*
```

用户态已经升到 7.9.2009，内核却被这条规则**挡了近 11 年**。这就是前文证据 3「跑 7.2 GA 内核」的成因 —— 不是不能升，是被人显式排除了。同时**系统里只有一个内核包**，意味着一旦新内核起不来，连 GRUB 回退项都没有。

⚠️ 处置顺序：**先换盘，后升内核**。盘有坏道时做内核升级，若升级过程中触发大量 I/O 撞上坏道，可能直接起不来。

### 五、修订后的完整时间线

```
2026-08-08 07:19   首次 Medium Error（LBA 539737368，内核日志）
                   —— 坏道已存在，当时未影响业务
        ↓
2026-09-01 05:09   Drive 0 Fault（SEL，BMC 硬件层记录）
        ↓          控制器停止响应 → 磁盘 I/O 全断
2026-09-01 05:12   Drive 0 Fault 清除（3m45s，控制器/盘复位）
        ↓          但虚拟盘已无法组装，NVRAM 与 DDF 元数据不一致
        ↓          系统 hang → 强制重启
        ↓
        POST: No boot device / Foreign Configuration(s) found on adapter
        ↓
        现场按 F 导入 → 虚拟盘 Optimal → 数据完好
```

### 六、还差最后一条数据就能把顺序钉死

`/var/log/messages` 只取了 `tail -1`，而各归档文件的末行其实是 **logrotate 轮转点**（Aug 30 03:48 是周日轮转时间），**不是故障点**。真正要的是「本次开机标记行」的前一条：

```bash
M=$(grep -nE "Linux version [0-9]|Command line:" /var/log/messages | tail -1 | cut -d: -f1)
echo "本次开机标记行: $M"
head -n $((M-1)) /var/log/messages | tail -8
```

| 若重启前最后一条日志是 | 则顺序为 | 结论 |
|---|---|---|
| **9-01 05:0x** | 盘故障 → 系统 hang | 链条完全闭合，如上时间线 |
| **8-30 03:5x** | 系统先 hang → 30 小时后盘才报 Fault | 仍是驱动缺陷，但顺序要改为「hang 在先，盘被拖死」 |

另有一条时间戳存疑：**2026-09-01 17:49:18 的 Medium Error** 来自 RAID 卡事件日志，而 **PERC 控制器自带时钟通常未同步**，不能与 OS 时间直接比。核对方法：

```bash
grep -n "17:49" /var/log/messages* 2>/dev/null | grep -iE "medium|error" | head
perccli /c0 show | grep -iE "Time|Date"      # 看控制器自己的时钟
```

### 七、SMART 读取失败的两个原因（本次踩坑）

```
-bash: MegaCli64: command not found
-bash: N: No such file or directory
```

1. **`MegaCli64` 本机根本没装** —— 之前的输出能用，说明当时走的是别的路径或别的工具（perccli / storcli）。
2. **`-d megaraid,<N>` 里的尖括号在 bash 中是输入重定向符** —— `<N>` 被 shell 解释成"从文件 N 读"，所以报 `N: No such file or directory`。必须写成纯数字。

正确姿势：

```bash
# 1) 先看有哪些 RAID 工具可用
ls /opt/MegaRAID/perccli/ 2>/dev/null; command -v perccli perccli64 storcli storcli64

# 2) 取 Device Id（注意是 DID 不是 Slot Number），顺带看控制器侧错误计数
perccli /c0 /eall /sall show | grep -iE "DID|State|Media Error|Predictive|S\.M\.A\.R\.T|Model"

# 3) 透传读 SMART（把 <DID> 换成上一步的实际数字）
smartctl -a -d megaraid,<DID> /dev/sda | grep -iE \
  "Reallocated_Sector_Ct|Current_Pending_Sector|Offline_Uncorrectable|Power_On_Hours"

# 4) 取不到 DID 就遍历探测（0~15，能返回型号即命中）
for i in 0 1 2 3 4 5 6 7; do
  echo "== $i"; smartctl -i -d megaraid,$i /dev/sda 2>/dev/null | grep -iE "Model Number|Product:"
done
```

### 八、据此更新的处置清单

| 优先级 | 事项 | 理由 |
|---|---|---|
| **P0** | **立即备份 `/opt/app/src/`**（尤其 `转发程序`，本地无备份）| SEL 已记录 Drive Fault，盘随时可能彻底不可读 |
| **P0** | **更换硬盘，并改为 RAID 1** | 消除零冗余；重建时坏道会被重写修复 |
| P1 | 换盘后**升内核**：去掉 `/etc/yum.conf` 的 `exclude=kernel*`，`yum update kernel`（目标 3.10.0-1160 系列），**保留旧内核作 GRUB 回退** | 摆脱 KB 3010132 缺陷；当前只有一个内核包，无回退 |
| P1 | 查 DIMMB1 当前 UE 计数：`grep -H "" /sys/devices/system/edac/mc/mc*/csrow*/ue_count`，非 0 则换内存 | 2022 年有 4 条不可纠正 ECC |
| P1 | 补 `转发程序` 备份、iDRAC 接入监控、RAID 状态纳入巡检 | 既有待办 |
| P2 | 评估 27 单点风险 | 809 六链路全经此机，无冗余 |
