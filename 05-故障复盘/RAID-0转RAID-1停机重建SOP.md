# RAID 0 → RAID 1 停机重建 SOP（Dell PERC / CentOS 7）

> 场景：单盘 RAID 0 业务机（Linux）需要升级为双盘 RAID 1，且**源盘已存在真实坏道**，不能走在线重构。
> 目标：**业务不宕机 / 故障可快速恢复**；本文是「停机重建」路径的完整操作手册。
> 配套阅读：同目录 [`RAID-Foreign配置故障复盘.md`](./RAID-Foreign配置故障复盘.md) —— 那份讲**事故根因怎么定位**（Foreign 配置、SEL 硬件日志、SMART 读取踩坑），本文讲**重建怎么执行**。

---

## 〇、方案选择：为什么不在线重构

| 路径 | 做法 | 评价 |
|---|---|---|
| A. 在线重构 RAID0→RAID1 | PERC Reconfigure / RLM，加一块盘在线迁移 | ❌ **不采用** |
| B. 停机重建（本 SOP） | 两块新盘建 RAID1 → 重装系统 → 还原业务 | ✅ **采用** |

**不采用 A 的三个理由**：

1. 源盘存在**真实未重映射坏道**（控制器日志有两次 Medium Error）→ 在线迁移需整盘读取，大概率在坏道处 hang、控制器复位，即上次宕机的同一条链条；
2. 迁移期间系统处于降级状态、I/O 性能下降，生产期持续数小时；
3. 一旦迁移失败，RAID 0 数据全丢；而收益仅仅是「免一次重装」。

**B 的额外收益**：顺带把内核版本缺陷、SELinux、软件源等历史问题一并清理。

---

## 一、操作步骤

### 阶段 0：窗口前准备（提前 2-3 天）

**硬件**

- 2 块**同型号同容量 SAS** 盘（不要复用那块带坏道的旧盘）；
- 8GB+ U 盘做安装盘；另备一个移动硬盘 / U 盘放恢复包；
- 确认机箱有空槽位（R630 一般 8 盘位）。

**软件**

- CentOS 7 ISO：用 **7.9.2009**（最后一版）；不要再用 7.2 GA（内核 3.10.0-327，即缺陷版本）；
- Ventoy / Rufus 写入 U 盘；
- 阵列卡驱动：PERC H330 的 `megaraid_sas` 一般已内置在 CentOS 7 安装介质中，安装时能直接识别 RAID 卷；识别不到再从 Dell 官网下载驱动加载；
- iDRAC 可用则远程操作（浏览器 → Virtual Console），不可用则现场接显示器键盘。

**恢复包（三份：本机、另一台、移动介质）**

```bash
# 1. 业务程序（排除日志/录像等大目录）
tar czf /tmp/app_src.tar.gz \
  --exclude='*/logfile/*' --exclude='*/Record/*' --exclude='*/Video/*' /cet/src

# 2. 防火墙规则
iptables-save > /root/iptables-backup-$(date +%F).rules

# 3. 系统与网络配置
tar czf /root/syscfg-backup.tar.gz \
  /etc/sysconfig/network-scripts /etc/fstab /etc/sysctl.conf \
  /etc/chrony.conf /etc/hosts /etc/resolv.conf /etc/yum.conf /etc/selinux/config
```

**当场校验（备份不校验等于没备份）**：

```bash
ls -lh /tmp/app_src.tar.gz
tar tzf /tmp/app_src.tar.gz | wc -l
tar tzf /tmp/app_src.tar.gz | grep -i <密钥文件名>      # 确认关键密钥/凭据文件确实在包内
```

> ⚠️ 业务程序目录中承载**平台通信密钥**的那个程序，往往是**本地零备份的唯一副本**，务必确认它已进包、且包在第二台机器上能正常解开。

### 阶段 1：停机

1. 通知相关方维护窗口（接入网关停止期间终端全体掉线重连，务必选凌晨低峰）；
2. 停业务服务，**逐个记录服务的启动命令**（重装后要照原样恢复）；
3. `shutdown -h now` 关机。

### 阶段 2：机房插盘

- 摸机箱金属放电（防静电）；
- 记录现有盘槽位并拍照（系统盘在哪一槽别认错）；
- 两块新盘插到空槽位，卡扣到位（关机插拔最稳）。

### 阶段 3：建 RAID 1

**入口一（推荐）：Lifecycle Controller 向导**
开机按 `F10` → Configuration Wizards → RAID Configuration → 选择 **RAID 1** → 勾选两块新盘 → Write Policy 选 **Write Through**（H330 无 BBU，选 Write Back 掉电会毁阵列元数据）→ Fast Initialize → 应用（向导会顺带把新 VD 设为引导盘）。

**入口二：PERC BIOS**
开机出现 PERC 提示时按 `Ctrl+R` → `VD Mgmt` 删除旧的 RAID 0 → `PD Mgmt` 确认两块新盘 Ready → 切到 VD 页 → `Create New VD` → RAID-1 → 选两块新盘 → Write Through → Fast Init。

**⚠️ Foreign 配置提示处理**

- 新盘若之前在别的阵列用过 → 提示 `Foreign Configuration Found`，本场景该盘是当空盘用的，**可以 Clear**；
- 若插入的是**带数据的旧盘** → 只能 **Import，绝不能 Clear**（Clear = 销毁数据）。

### 阶段 4：安装 CentOS 7

1. 开机按 `F11` 选 U 盘引导；
2. 分区建议（**业务程序路径基准不可改名**）：

| 挂载点 | 大小 | 说明 |
|---|---|---|
| /boot | 1G | 默认 |
| swap | 16G | 与原有习惯保持一致 |
| / | 100G | 系统与程序 |
| **/cet** | 剩余 | 业务程序路径基准，**不可改名** |

3. 文件系统 XFS（CentOS 7 默认）；
4. **网络配成原静态 IP**，掩码 / 网关 / DNS / 主机名与原机一致（公网 NAT、上级平台白名单、服务发现地址配置都绑着它）；
5. SELinux 状态与原机一致（见备份中的 `/etc/selinux/config`）；
6. 最小化安装。

**🔴 装完后必做：切换 yum 源到 vault（CentOS 7 已 EOL，不做这步 `yum install` 全部报错）**

```bash
sed -i -e 's/mirror.centos.org/vault.centos.org/g' \
       -e 's/^#baseurl=/baseurl=/g' \
       -e 's/^mirrorlist=/#mirrorlist=/g' /etc/yum.repos.d/CentOS-*.repo
yum clean all && yum makecache
```

国内网络可改用镜像站（速度更好，同理替换域名）：`mirrors.ustc.edu.cn/centos-vault`、`mirrors.aliyun.com/centos-vault`。
若 `$releasever` 解析异常，直接把 repo 文件里的 `$releasever` 全部替换为 `7.9.2009`。

**安装基础工具 + 切换防火墙**

```bash
yum install -y net-tools iptables-services chrony vim wget
systemctl disable firewalld
systemctl stop firewalld
systemctl start iptables
systemctl enable iptables
```

### 阶段 5：恢复业务

```bash
# 1. 转发核心参数（DNAT/SNAT 依赖，必须开）
sysctl -w net.ipv4.ip_forward=1
# 并确认 /etc/sysctl.conf 里有 net.ipv4.ip_forward = 1（从备份恢复该文件更稳）

# 2. 恢复防火墙规则（iptables 服务的配置就存在这个文件里）
iptables-save  < /root/iptables-backup-<日期>.rules
iptables-save  > /etc/sysconfig/iptables
systemctl restart iptables

# 3. 恢复业务程序（-p 保留权限，密钥文件权限别丢）
tar xzf /tmp/app_src.tar.gz -C /

# 4. 按记录的启动方式启动各业务服务
```

> 🔴 恢复过程**绝不要执行 `iptables -F`**（清空所有规则）。远程操作时清空规则＝把自己关在门外，且中断所有转发业务。

### 阶段 6：验证清单

```bash
ip a && ip r
cat /proc/sys/net/ipv4/ip_forward        # 应为 1
iptables -t nat -L -n -v | head -40      # 看 DNAT/SNAT 规则与计数
ss -lntp | grep -E ':(业务端口)'
```

业务侧确认：**终端重连上报恢复** → **转发链路正常** → **监控（Zabbix / 链路监控）恢复** → `ipmitool sel elist` 无新告警。

### 阶段 7：善后

```bash
# 1. 解除内核锁定（原来 /etc/yum.conf 有 exclude=kernel*，导致内核停在旧版本）
sed -i '/exclude=kernel/d' /etc/yum.conf
yum install -y kernel        # 保留旧内核作为 GRUB 回退项，不要 rpm -e

# 2. 内存 ECC 检查
cat /sys/devices/system/edac/mc/mc*/csrow*/ue_count
```

3. 有条件时装 Dell OMSA 或 `perccli`，以后可在线查看阵列健康；再加 SMART 巡检与监控告警；
4. 把本次全过程整理成可复用的重建 SOP（本文即成果），下次故障恢复目标：**30 分钟内业务恢复**。

---

## 二、坏掉的旧盘怎么处理

**结论：不再使用，但不要销毁、不要丢弃。**

| 处理项 | 说明 |
|---|---|
| ❌ 不能插回阵列 | 带坏道的盘放进新 RAID 会被控制器标记 Failed 导致降级，或拖慢 / 卡住重建；也不可当热备盘 |
| ✅ 保留作应急回退 | 它上面是原 RAID 0 的完整元数据与数据。万一新系统彻底失败，可关机插回、在 PERC 里对提示的 Foreign 配置选 **Import**（不是 Clear），可能把原系统起回来——**仅作最后手段**，因为它随时可能彻底失效 |
| ✅ 走保修更换 | 在保的话拿 Service Tag 找 Dell 更换；不在保则报废 / 退库 |
| ⚠️ 插回时注意 | 若日后插回作应急，务必检查启动顺序，别让引导指向旧盘，不要与新 RAID1 混淆 |

---

## 三、风险与注意事项

1. **停机窗口**：接入网关停止 = 终端全体掉线重连，务必选低峰期；预计半天。
2. **IP / 主机名 / 路径三者都不能变**：IP 被 NAT 与上级平台白名单依赖；主机名被程序绑定依赖；业务程序路径被相对路径配置依赖。
3. **密钥与配置的恢复优先级最高**：平台通信密钥、iptables 规则、服务发现地址配置——丢了业务起不来。
4. **iDRAC / PERC 菜单名随固件版本略有差异**，以屏幕实际显示为准。
5. 全部命令已对照 RHEL 7 官方文档核对语法，并**已在 CentOS 7.4.1708 实测机上逐条实跑通过**；**尚未在目标生产机执行**（需停机窗口）——执行中任何一步报错先停下，截图留证再继续。

---

## 附：本机操作前 5 秒确认

```bash
hostname; ip a | grep <业务IP>; cat /etc/redhat-release
```

确认是目标业务机且为 `CentOS Linux release 7.x` 再动手——**本方案不适用于 Windows 主机**。
