# Linux 运维学习笔记

> 面向"能用、能排障"整理，不是命令手册。
> 内容来源：CentOS 7 / Rocky Linux 9 上的实际部署与故障处理记录。

---

## 一、网络与防火墙

### 1.1 iptables 的四个表和五条链（先建立框架）

iptables 的规则不是平铺的，而是挂在「表 → 链」上：

| 表 | 作用 | 常见链 |
|---|---|---|
| `filter` | 过滤（放行/拒绝），**默认表** | INPUT / FORWARD / OUTPUT |
| `nat` | 地址转换 | PREROUTING / POSTROUTING / OUTPUT |
| `mangle` | 改报文头（TTL、TOS 等） | 全部五链 |
| `raw` | 连接跟踪豁免 | PREROUTING / OUTPUT |

五条链的执行顺序（数据包进来）：

```
PREROUTING(raw→mangle→nat) → 路由判断 → FORWARD(filter→mangle) → POSTROUTING(mangle→nat) → 出去
                                      ↘ INPUT(filter→mangle) → 本机进程
```

**排障含义**：一个包被拒，要先看它在哪条链被处理。DNAT 在 PREROUTING，SNAT/MASQUERADE 在 POSTROUTING——这就是为什么"端口映射不生效"要分别查这两处。

### 1.2 DNAT / SNAT / MASQUERADE 怎么选

| 类型 | 改什么 | 典型场景 |
|---|---|---|
| `DNAT` | 改**目的**地址端口 | 外网访问内网服务（端口映射、发布服务） |
| `SNAT` | 改**源**地址端口 | 内网机器借网关出网，且**网关 IP 固定** |
| `MASQUERADE` | 改源地址为「出口网卡当前 IP」 | 同上，但网关 IP **不固定**（如拨号/动态 IP） |

```bash
# 端口映射：把到达本机 8080 的流量转给内网 192.0.2.100:80
iptables -t nat -A PREROUTING -p tcp --dport 8080 -j DNAT --to-destination 192.0.2.100:80
# 转出去的包还要做源地址伪装，否则回包回不来
iptables -t nat -A POSTROUTING -d 192.0.2.100 -p tcp --dport 80 -j MASQUERADE
# 放行转发链（这一步最容易漏）
iptables -A FORWARD -d 192.0.2.100 -p tcp --dport 80 -j ACCEPT
```

**三个高频陷阱**

1. **只加 DNAT 不加 FORWARD 放行** → 包能进来但被 FORWARD 链默认策略 DROP，现象是"连接超时"。
2. **只加 DNAT 不做源地址伪装**（目标机不在同一网段时）→ 回包走默认网关，不回本机，现象是"三次握手不完整"。
3. **忘记开 `ip_forward`** → 转发功能根本没启用：

```bash
sysctl -w net.ipv4.ip_forward=1                    # 临时
echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf   # 持久
```

### 1.3 规则要"幂等"，否则重跑必出问题

定时任务/脚本重复执行 `iptables -A` 会**不断追加重复规则**，规则数越滚越多，最终拖慢转发。正确做法是**先删后加**或**先查再插**：

```bash
# 写法一：先删同规则再插入（幂等）
iptables -t nat -D PREROUTING -p tcp --dport 8080 -j DNAT --to-destination 192.0.2.100:80 2>/dev/null || true
iptables -t nat -A PREROUTING -p tcp --dport 8080 -j DNAT --to-destination 192.0.2.100:80

# 写法二：用 -C 检查存在性（存在即跳过）
iptables -t nat -C PREROUTING -p tcp --dport 8080 -j DNAT --to-destination 192.0.2.100:80 2>/dev/null \
  || iptables -t nat -A PREROUTING -p tcp --dport 8080 -j DNAT --to-destination 192.0.2.100:80
```

**变更要留痕**：删除旧规则前先给它加注释（`-m comment --comment "old-20260910"`）而不是直接 `-D`，出问题时能一眼看出"谁被替换了、什么时候"。

> ⚠️ **绝对不要在生产上执行 `iptables -F`**（清空所有规则）。远程操作时清空规则＝把自己关在门外，且可能中断所有转发业务。真要重建规则，先 `iptables-save > 备份文件`。

### 1.4 规则持久化

```bash
iptables-save > /etc/sysconfig/iptables     # 保存（CentOS 7 习惯路径）
service iptables save                        # 或（需 iptables-services）
```

> 容器环境里 `iptables -F`/重载会**同时影响宿主机与其他容器**，改动前务必确认影响面。

### 1.5 网络排障的顺序感

一个"服务不通"的问题，按层往下走，比乱试快得多：

```
① 进程在不在      ss -tlnp | grep :端口      /  systemctl status 服务
② 本机通不通      telnet 127.0.0.1 端口      /  curl -v http://127.0.0.1:端口
③ 本机防火墙      iptables -L -n --line-numbers  /  firewall-cmd --list-all
④ 云安全组        云控制台（注意：规则变更后有几十秒传播延迟）
⑤ 路由/对端        traceroute / 抓包 tcpdump
```

**关键判据**：`ping / telnet / ssh` **三者全不通**时，不要只往网络拦截上想——**主机根本没启动**（如 RAID 未组装、无启动设备）也会是这个现象。这时应该去带外管理（iDRAC / IPMI）或机房现场看 POST 画面，而不是继续在网络层打转。

---

## 二、SELinux

### 2.1 三种模式

| 模式 | 行为 |
|---|---|
| `enforcing` | 强制生效，违规操作被拒绝并记审计日志 |
| `permissive` | 只记录不拒绝（**验证"SELinux 是不是元凶"的最佳姿势**） |
| `disabled` | 完全关闭 |

```bash
getenforce                                  # 查当前
setenforce 0                                # 临时切 permissive（重启后失效）
sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config   # 永久
```

### 2.2 一个典型的 SELinux 误判

**现象**：服务 `systemctl start` 秒退，`journalctl` 里只有 banner 和 `status=1`，看不到真实错误；但**用 root 手动前台跑同一个程序却是正常的**。

**为什么**：服务进程受 SELinux 域限制，root 手动执行不受限。
**怎么定位**：

```bash
journalctl -u 服务名 -n 50 --no-pager          # 常只有 banner
tail -50 /var/log/服务自己的日志               # 真实报错常只写在这里
sealert -a /var/log/audit/audit.log            # 读审计日志，它会直接告诉你哪条规则被拒
getsebool -a | grep 相关布尔值                  # 或直接 setsebool
```

**结论**：**不要一上来就 `setenforce 0` 收工**。先 `setenforce 0` 验证归因，确认是 SELinux 后再用 `setsebool -P` 精确放行（`-P` 才是持久化），最后切回 enforcing。

### 2.3 常用布尔值

```bash
setsebool -P httpd_can_network_connect 1      # 允许 httpd/nginx 主动发网络连接
setsebool -P zabbix_can_network 1             # 允许 zabbix_server 发网络连接
```

---

## 三、时间同步（chrony）

时间不对会引发**证书校验失败、日志时间错乱、集群脑裂误判**，是隐蔽但高频的故障源。

```bash
timedatectl                                          # 一眼看时区/是否同步
chronyc sources -v                                   # 看时间源，^^* 表示当前选中的同步源
chronyc tracking                                     # 看偏移量与同步状态
```

**读 `chronyc sources` 的关键标志**：

| 标志 | 含义 |
|---|---|
| `^*` | 当前**正在同步**的源（最理想） |
| `^+` | 备选源（与当前源一致，可切换） |
| `^-` | 可用但与当前源不一致 |
| `^?` | 不可达/尚未同步（**排障重点**） |

```bash
systemctl enable --now chronyd
chronyc makestep        # 立即强制校时（大幅偏差时用，避免"慢慢追"）
```

> **BMC / IPMI 的时间陷阱**：带外管理卡的时间常常跑 **UTC**，而操作系统跑本地时区（如 CST +8）。读取 BMC 事件日志（SEL）时若直接当本地时间读，**会把事故时间读错整整 8 小时**，与内核日志完全对不上。做法是先 `ipmitool sel time get` 取 BMC 时间，与 `date` 对比算出偏差，再把 SEL 时间换算到系统时区。

---

## 四、yum / dnf 源管理

### 4.1 换源与缓存

```bash
yum clean all && yum makecache          # 清缓存重建
yum repolist enabled                     # 看启用了哪些源（排"没有可用软件包"第一步）
yum --disablerepo=xxx install 包名        # 临时禁用某个源（解决"包冲突"）
```

### 4.2 包冲突的典型形态

**报错**：`Transaction check error: file /usr/bin/xxx from install of A conflicts with file from package B`

**含义**：两个源提供了**同一个文件路径**（典型是同一个软件被打包成不同名字，如 `zabbix6.0-*` 与 `zabbix-*` 都提供 `/usr/bin/zabbix_get`）。

**处理原则**：**同类软件只留一套源**。要么全用 A 源，要么全用 B 源；混装时用 `--disablerepo=` 显式排除。
**代价提示**：先装的那套如果已经写进了服务单元和配置文件，换源后**路径和单元名可能一起变**（例如配置文件从 `/etc/xxx.conf` 变成 `/etc/xxx/xxx.conf`），要连带修正。

### 4.3 别让 `exclude` 悄悄挡住升级

`/etc/yum.conf` 里的 `exclude=kernel*` 会让内核**永远升不上去**，`yum update` 看着"跑成功了"其实内核没动。

```bash
grep -n "exclude" /etc/yum.conf          # 排查第一步
uname -r                                  # 当前内核
rpm -qa | grep ^kernel | sort             # 已装内核列表
```

> 内核长期停留在老版本，可能命中已知缺陷（如存储驱动在控制器无响应后被强制复位，引发整机 I/O 中断）。**锁定内核是有意为之还是历史遗留，要问清楚再动。**

---

## 五、日志与排障工具速查

| 目的 | 命令 |
|---|---|
| 服务日志（systemd） | `journalctl -u 服务名 -n 100 --no-pager` |
| 服务日志（自己写的文件） | `tail -f /var/log/xxx.log` |
| 本次开机以来的内核消息 | `dmesg -T \| tail -50`（`-T` 转可读时间） |
| 磁盘健康 | `smartctl -a /dev/sdX` |
| RAID 状态（LSI/Broadcom） | `perccli /c0 /vall show`（**只读**） |
| 带外硬件日志 | `ipmitool sel list` / `ipmitool sdr` |
| 端口占用 | `ss -tlnp` |
| 抓包 | `tcpdump -i eth0 -nn host 192.0.2.100 -w cap.pcap` |
| 日志按时间找断点 | 在日志里找"本次开机标记行"，**它前一条就是系统停止写日志的最后时刻** |

> **日志轮转陷阱**：看 `tail -1` 判断"服务什么时候停的"很容易被 `logrotate` 的轮转时间骗到（文件最后修改时间是轮转时间，不是业务停止时间）。要按内容里的时间戳定位，而不是文件系统时间。
