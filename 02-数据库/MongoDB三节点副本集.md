# MongoDB 三节点副本集部署与运维（CentOS 7）

> 拓扑：**PSS**（1 Primary + 2 Secondary），三节点分处不同主机，端口统一 `27017`，副本集名统一 `rs0`。
> 目标：理解副本集"选主—复制—切换—恢复"的完整闭环，并能自己排障、自己重建。
> 版本基线：MongoDB **7.0**（server）+ 同大版本命令行工具。

本文档所有版本事实基于以下**实测核实**（2026-09 查询官方仓库元数据）：

- 官方 8.0 仓库对 RHEL7 返回 **404** → **CentOS 7 能装的最高版本是 MongoDB 7.0**（rhel7 的 7.0 仓库仍在更新）
- rhel7 的 7.0 仓库包齐全：`mongodb-org-server` **7.0.9**、`mongodb-mongosh` **2.9.2**、`mongodb-database-tools` **100.9.5**
- 仓库里**没有 `mongodb-org-shell`**（老 `mongo` 命令行 6.0 起移除）→ 一律用 **mongosh**
- `mongodb-org-server` 的依赖（openssl 1.0.2、libcurl、glibc≥2.14、systemd、tzdata）**全部是 CentOS 7 自带**，装完即可运行

---

## 〇、先搞清三个层次（别混在一起排障）

副本集出问题，报错信息往往很吓人，但根因几乎总落在这三件事之一：

| 层次 | 关键点 | 排障入口 |
|---|---|---|
| **配置一致** | 三台的 `replSetName` 必须完全相同 | `/etc/mongod.conf` 的 `replication.replSetName` |
| **能互相连** | `bindIp` 要允许其他节点连，防火墙要放端口 | `db.hello().members[].health` |
| **能互相认** | keyFile 三台**内容完全一致**、权限 `400` | 日志里 `Authentication failed` |

> **版本管理粒度（重要）**：`mongod`（服务端）**三个节点必须完全一致**；`mongosh` / `mongodump` / `mongorestore` 等**工具**只要求**大版本一致**即可，小版本可以不同。先理解这条，能避免"工具版本和 server 不一致就慌"。

---

## 一、示例环境（执行前先全局替换）

本文用以下示例地址，**执行任何命令前先把这三个 IP / 主机名替换成实际值**：

| 主机 | 示例 IP | 主机名（别名） | 角色 |
|---|---|---|---|
| 主机1 | 192.0.2.61 | mongo1 | 副本集成员（初始主） |
| 主机2 | 192.0.2.62 | mongo2 | 副本集成员 |
| 主机3 | 192.0.2.63 | mongo3 | 副本集成员 |

- mongo1 / mongo2 / mongo3 不是系统主机名，只是写在 `/etc/hosts` 里的别名，改 IP 不用改机器名
- 所有【三台都执行】的步骤，三台各跑一遍；【仅 mongo1】的只跑一次

---

## 二、系统准备与核心配置

### 2.1 前置自检【三台都执行】

```bash
# ① CPU 支持 AVX（MongoDB 5.0+ 硬性要求，没有则 mongod 起不来报 Illegal instruction）
lscpu | grep -o 'avx[^ ]*' | sort -u
# 期望：有输出（avx / avx2 都算支持）。虚拟机尤其要查，没有 AVX 需要改用 4.4（不要求 AVX）

# ② 时间同步（副本集对时钟敏感，偏差过大会踢成员）
chronyc tracking | grep -E 'Stratum|System time'
# 期望：System time 偏差在秒级以内。chronyd 没起就先 systemctl enable --now chronyd

# ③ 磁盘空间（数据目录在 /var/lib/mongo）
df -h /var/lib/mongo
# 期望：可用空间 > 20GB

# ④ SELinux 当前状态（后面要按结果处理）
getenforce
```

### 2.2 配置 hosts【三台都执行】

先改 IP 再粘贴（**三台文件内容完全相同**）：

```bash
cat >> /etc/hosts <<'EOF'
192.0.2.61 mongo1
192.0.2.62 mongo2
192.0.2.63 mongo3
EOF

# ✔ 验证：三个都能解析到内网 IP，绝不能解析到 127.0.0.1
getent hosts mongo1 mongo2 mongo3
# 期望输出三行，分别指向 192.0.2.61/62/63

# ✔ 验证互通（每台都跑）
ping -c2 mongo2
```

### 2.3 配置 yum 仓库并安装【三台都执行】

用 MongoDB 官方源（实测 repomd.xml 正常返回、仍在更新；阿里云镜像的 MongoDB 目录已弃用，不再使用）：

```bash
cat > /etc/yum.repos.d/mongodb-org-7.0.repo <<'EOF'
[mongodb-org-7.0]
name=MongoDB Repository
baseurl=https://repo.mongodb.org/yum/redhat/7/mongodb-org/7.0/x86_64/
gpgcheck=0
enabled=1
EOF
```

> `gpgcheck=0` 说明：官方 GPG 密钥在 `pgp.mongodb.com`，国内网络不可达时 yum 安装会因取不到密钥而失败。内网环境先关校验保证能装。若服务器能访问 `pgp.mongodb.com`，可改为：
> `rpm --import https://pgp.mongodb.com/server-7.0.asc` 后把 repo 里 `gpgcheck=0` 改成 `gpgcheck=1`。
>
> 直连提示：`repo.mongodb.org` 走 Cloudflare/AWS，国内多数网络可直连；若 `yum makecache` 超时，先确认可达性：
> `curl -sI -m 10 https://repo.mongodb.org/yum/redhat/7/mongodb-org/7.0/x86_64/repodata/repomd.xml`（期望 `HTTP 200`）

```bash
# 刷新缓存并确认仓库可用
yum makecache
yum repolist | grep -i mongodb
# 期望：mongodb-org-7.0 出现且包数量 > 0

# 只装副本集需要的三个包（mongos 是分片集群用的，不装）
yum install -y mongodb-org-server mongodb-mongosh mongodb-database-tools

# ✔ 验证
mongod --version | head -2      # 期望：db version v7.0.9
mongosh --version               # 期望：2.9.2
which mongodump mongorestore mongostat mongotop
# 期望：四个路径都在 /usr/bin/ 下（来自 mongodb-database-tools）
```

> 注意：不要执行 `yum install mongodb-org`——它会连带装 mongos 等分片组件；更不要装 `mongodb-org-shell`——7.0 仓库里没有这个包，装了报"找不到"。

### 2.4 内核与系统调优【三台都执行】

```bash
# ① 关闭 THP（Transparent Huge Pages），WiredTiger 要求
cat > /etc/systemd/system/disable-thp.service <<'EOF'
[Unit]
Description=Disable Transparent Huge Pages (THP)
DefaultDependencies=no
After=sysinit.target local-fs.target
Before=mongod.service

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo never > /sys/kernel/mm/transparent_hugepage/enabled; echo never > /sys/kernel/mm/transparent_hugepage/defrag'

[Install]
WantedBy=basic.target
EOF
systemctl daemon-reload
systemctl enable --now disable-thp

# ✔ 验证：方括号必须套在 never 上
cat /sys/kernel/mm/transparent_hugepage/enabled
# 期望：always madvise [never]

# ② swappiness 与 TCP keepalive
cat > /etc/sysctl.d/99-mongo.conf <<'EOF'
vm.swappiness = 1
net.ipv4.tcp_keepalive_time = 120
EOF
sysctl --system

# ✔ 验证
sysctl vm.swappiness      # 期望：vm.swappiness = 1
```

### 2.5 防火墙与 SELinux【三台都执行】

```bash
# 防火墙：先看状态，running 才需要放行
firewall-cmd --state 2>/dev/null
# 若输出 running：
firewall-cmd --permanent --add-port=27017/tcp
firewall-cmd --reload
# 若不是 running：跳过

# SELinux：Enforcing 会拦 mongod 的部分操作
getenforce
# 若输出 Enforcing：
setenforce 0
sed -i 's/^SELINUX=enforcing$/SELINUX=permissive/' /etc/selinux/config
# 若本来就是 Permissive/Disabled：跳过
```

### 2.6 mongod.conf（核心配置逐项讲解）【三台都执行】

整份替换 `/etc/mongod.conf`（**只有 bindIp 一行三台不同**）：

```bash
cat > /etc/mongod.conf <<'EOF'
storage:
  dbPath: /var/lib/mongo
  journal:
    enabled: true
  wiredTiger:
    engineConfig:
      cacheSizeGB: 4

systemLog:
  destination: file
  logAppend: true
  path: /var/log/mongodb/mongod.log

net:
  port: 27017
  bindIp: 127.0.0.1,192.0.2.61

processManagement:
  timeZoneInfo: /usr/share/zoneinfo

replication:
  replSetName: rs0
  oplogSizeMB: 2048

# —— 安全认证，后面再启用，现在保持注释 ——
#security:
#  keyFile: /etc/mongo-keyfile
#  authorization: enabled
EOF
```

**逐项说明（排查与面试都问这个）**：

| 配置 | 作用 | 怎么定 |
|---|---|---|
| `dbPath` | 数据目录 | 用 RPM 默认 `/var/lib/mongo`（属主已配好 mongod:mongod，零权限工作） |
| `cacheSizeGB` | WiredTiger 缓存上限，**默认约为 (内存-1GB)/2，是最大坑点：默认值会把内存吃掉一半** | 专用机 = 物理内存的 40~50%；8GB 内存示例给 4 |
| `bindIp` | 监听地址。**只写 127.0.0.1 是新装后"远程连不上"的第一大原因** | 本机回环 + 本机内网 IP，逗号分隔无空格 |
| `replSetName` | 副本集名，三台必须一致 | rs0 |
| `oplogSizeMB` | oplog 上限；太小 → 从节点追不上被淘汰；太大 → 占磁盘 | 建议可承受 24h+ 写量 |
| `keyFile` | 副本集内部通信 + 登录认证的共享密钥（启用了它，authorization 自动开启） | 下一节生成 |
| `authorization` | 登录鉴权开关 | 与 keyFile 一起启用 |

**每台改自己的 bindIp**（只改 IP 数字）：

```bash
# mongo1 上执行：
sed -i 's/^  bindIp:.*/  bindIp: 127.0.0.1,192.0.2.61/' /etc/mongod.conf
# mongo2 上执行：
sed -i 's/^  bindIp:.*/  bindIp: 127.0.0.1,192.0.2.62/' /etc/mongod.conf
# mongo3 上执行：
sed -i 's/^  bindIp:.*/  bindIp: 127.0.0.1,192.0.2.63/' /etc/mongod.conf

# ✔ 回读确认（每台）
grep -E 'bindIp|replSetName|oplogSize' /etc/mongod.conf
```

**首次启动【三台都执行】**：

```bash
systemctl enable --now mongod
systemctl status mongod --no-pager -l | head -12
# 期望：Active: active (running)

ss -tlnp | grep 27017
# 期望：监听 127.0.0.1:27017 和 本机内网IP:27017

# 文件句柄已由 systemd 单元默认设为 64000，回读确认
cat /proc/$(pidof mongod)/limits | grep 'open files'
# 期望：64000

tail -5 /var/log/mongodb/mongod.log
# 期望：看到 "Waiting for connections" 且无 ERROR
```

> mongosh 首次交互式进入时可能询问是否开启遥测，输入 `no` 回车即可。

---

## 三、安全认证（keyFile + 用户权限）

分三阶段走：**先无认证把副本集搭通（方便排错）→ 建用户 → 再启用认证**。空集群无业务数据，这个顺序最稳，每一步出问题都能定位是复制问题还是认证问题。

### 3.1 阶段 A：初始化副本集（无认证）【仅 mongo1】

```bash
mongosh --host 127.0.0.1
```

进入提示符后执行：

```javascript
rs.initiate({
  _id: "rs0",
  members: [
    { _id: 0, host: "mongo1:27017" },
    { _id: 1, host: "mongo2:27017" },
    { _id: 2, host: "mongo3:27017" }
  ]
})
// 期望返回：{ ok: 1 }

// 等几秒后查看状态
rs.status().members.map(m => ({ name: m.name, state: m.stateStr }))
// 期望：mongo1 = PRIMARY，mongo2 / mongo3 = SECONDARY
// 若显示 STARTUP2 / RECOVERING，再等几秒重跑；退出用 exit
```

> 若三个成员全是 `(not reachable/healthy)`：九成是 hosts 把 mongoX 解析到了 127.0.0.1、bindIp 没加本机内网 IP、或防火墙没放行。按这个顺序排查。

**`host` 字段的唯一高频错误**：写 `127.0.0.1` 会导致**其他节点连不上自己**。必须写其他节点能访问到的地址。

### 3.2 阶段 B：验证复制链路【mongo1 写 / mongo2 读】

```javascript
// mongo1 上（提示符已变为 rs0 [direct: primary] test> ）：
use testdb
db.t1.insertOne({ ok: 1, note: "replication test" })
// 期望：{ acknowledged: true, ... }
```

```javascript
// mongo2 上：
db.getMongo().setReadPref("secondaryPreferred")
use testdb
db.t1.find()
// 期望：能读到 mongo1 写入的那条 → 复制链路通了
```

### 3.3 阶段 C：创建用户【仅 mongo1，趁认证还没开】

```javascript
// 超级管理员（建在 admin 库，role=root）
use admin
db.createUser({
  user: "admin",
  pwd: passwordPrompt(),          // 回车后提示输入密码，密码不落 shell 历史
  roles: [ { role: "root", db: "admin" } ]
})
// 期望：{ ok: 1 }

// 应用账号：读写 appdb（用户实体存 admin 库，所以连接串 authSource=admin）
db.createUser({
  user: "appuser",
  pwd: passwordPrompt(),
  roles: [ { role: "readWrite", db: "appdb" } ]
})
// 期望：{ ok: 1 }
exit
```

### 3.4 阶段 D：启用 keyFile 认证【三台都执行】

```bash
# ① 生成密钥（仅 mongo1 执行）
openssl rand -base64 756 > /etc/mongo-keyfile
chmod 400 /etc/mongo-keyfile
chown mongod:mongod /etc/mongo-keyfile

# ② 分发到另外两台（在 mongo1 上执行）
scp /etc/mongo-keyfile root@mongo2:/etc/mongo-keyfile
scp /etc/mongo-keyfile root@mongo3:/etc/mongo-keyfile
```

```bash
# ③ mongo2 / mongo3 上修正属主与权限（scp 过去属主是 root，必须修）
chmod 400 /etc/mongo-keyfile
chown mongod:mongod /etc/mongo-keyfile
ls -l /etc/mongo-keyfile
# 期望：三台都是 -r--------  mongod mongod

# ④ 三台把 mongod.conf 里的 security 段取消注释
sed -i 's/^#security:/security:/; s/^#  keyFile:/  keyFile:/; s/^#  authorization:/  authorization:/' /etc/mongod.conf

# ✔ 回读（每台）
grep -A3 '^security:' /etc/mongod.conf
# 期望：security: / keyFile: /etc/mongo-keyfile / authorization: enabled 三行无 #

# ⑤ 空集群无业务，直接三台依次重启
systemctl restart mongod
systemctl status mongod --no-pager | head -5
```

**三个必须**：

1. **内容三台完全一致**（不一致 → 节点间互相认证失败）；
2. **权限只能是 `400`**（宽了 MongoDB 直接拒绝启动，日志会明确提示）；
3. **属主是 `mongod`**（不是 root，否则服务读不到）。

### 3.5 阶段 E：验证认证

```bash
# ① 不带凭据 → 应该被拒绝（这一步报错才是正确的）
mongosh --host 127.0.0.1 --eval 'show dbs'
# 期望：MongoServerError: command listDatabases requires authentication

# ② 带凭据 → 正常
mongosh --host 127.0.0.1 -u admin -p --authenticationDatabase admin
```

```javascript
rs.status().members.map(m => ({ name: m.name, state: m.stateStr }))
// 期望：仍是 1 PRIMARY + 2 SECONDARY（认证后复制正常）
exit
```

**应用连接串**：

```
mongodb://appuser:<密码>@mongo1:27017,mongo2:27017,mongo3:27017/appdb?replicaSet=rs0&authSource=admin
```

> 三个主机都写上 + `replicaSet=rs0`，驱动会自动发现主从并做故障转移；`authSource=admin` 是因为用户建在 admin 库。

---

## 四、备份与恢复

### 4.1 手工全量备份

```bash
mkdir -p /data/backup

mongodump --host "rs0/mongo1:27017,mongo2:27017,mongo3:27017" \
  -u admin -p --authenticationDatabase admin \
  --oplog --gzip --out /data/backup/$(date +%F)

# ✔ 验证
ls /data/backup/$(date +%F)/
# 期望：admin  appdb  oplog.bson 等目录/文件
```

**参数含义**：

- `--host rs0/成员列表`：**用副本集连接串**，让驱动自动找 Primary；
- **`--oplog`**：dump 期间同时抓取 oplog，让备份具备**某个一致时间点**的语义。没有它，dump 多个集合时可能"这个集合是 10:00 的快照、那个是 10:05 的"，跨集合数据对不上；
- `--gzip`：压缩。

> **和 MySQL 的对照**：`mysqldump --single-transaction` 靠事务拿一致性快照；MongoDB 没有跨集合事务语义时，就靠 `--oplog` 拿一致点。两个数据库"一致性备份"的手段不同，但**要解决的问题是同一个**。

### 4.2 每日 02:00 自动备份 + 保留 7 天

```cron
0 2 * * * /usr/bin/mongodump --host "rs0/mongo1:27017,mongo2:27017,mongo3:27017" -u admin -p'<密码>' --authenticationDatabase admin --oplog --gzip --out /data/backup/$(date +\%F) && find /data/backup -maxdepth 1 -mindepth 1 -type d -mtime +7 -exec rm -rf {} \;
```

> 注意 `date +\%F` 里的 **`\%` 必须带反斜杠** —— crontab 中 `%` 有特殊含义。
> 生产提示：密码写 cron 有泄露面（crontab 文件本身在 `/var/spool/cron/root`，默认 600），条件允许改用密钥文件或独立备份机。

### 4.3 恢复演练

```bash
# 覆盖式恢复（--drop：先删同名集合再灌入，避免主键冲突）
mongorestore --host "rs0/mongo1:27017,mongo2:27017,mongo3:27017" \
  -u admin -p --authenticationDatabase admin \
  --gzip --oplogReplay --drop \
  --dir /data/backup/<备份目录日期>

# ✔ 验证
mongosh --host 127.0.0.1 -u admin -p --authenticationDatabase admin --quiet \
  --eval 'db.getSiblingDB("testdb").t1.find().toArray()'
```

> 恢复会直接写主库，**演练前确认这是学习库**；生产恢复前先停应用写入。

---

## 五、监控

### 5.1 mongosh 诊断命令（每天都要看的几个）

```javascript
// ① 成员角色与健康（最重要，看 stateStr 和 health）
rs.status().members.map(m => ({ name: m.name, state: m.stateStr, health: m.health }))

// ② 主从延迟（optimeDate 差值就是落后秒数）
rs.status().members.map(m => ({ name: m.name, optime: m.optimeDate }))

// ③ oplog 窗口：能覆盖多久的写入（决定从库宕机多久内能追上）
db.getSiblingDB("local").printReplicationInfo()

// ④ 各从节点同步滞后明细
db.printSecondaryReplicationInfo()

// ⑤ 连接数（逼近 maxConnections=500 默认值要扩容或排查连接泄漏）
db.serverStatus().connections

// ⑥ 内存与操作计数
db.serverStatus().mem
db.serverStatus().opcounters
```

**怎么看 oplog 够不够**：`printReplicationInfo()` 输出的 **log length start to end** 若小于"节点最长可能离线时长"，就必须放大。

```javascript
// 在线调整 oplog 大小（7.0 支持）
db.adminCommand({ replSetResizeOplog: 1, size: 20480 })   // 单位 MB
```

### 5.2 mongostat / mongotop（实时负载）

```bash
# 每秒刷新，打 5 行退出；关注 qr|qw（排队）和 dirty%（脏页>20% 说明缓存吃紧）
mongostat --host "rs0/mongo1:27017,mongo2:27017,mongo3:27017" \
  -u admin -p --authenticationDatabase admin --rowcount 5

# 看哪些集合最吃读写时间
mongotop --host "rs0/mongo1:27017,mongo2:27017,mongo3:27017" \
  -u admin -p --authenticationDatabase admin 5
```

### 5.3 可选：mongodb_exporter（Prometheus 体系）

```javascript
// 给 exporter 一个只读监控账号
use admin
db.createUser({
  user: "exporter",
  pwd: passwordPrompt(),
  roles: [ { role: "clusterMonitor", db: "admin" }, { role: "read", db: "local" } ]
})
```

```bash
docker run -d --name mongodb-exporter --restart unless-stopped -p 9216:9216 \
  percona/mongodb_exporter:0.20 \
  --mongodb.uri="mongodb://exporter:<密码>@mongo1:27017,mongo2:27017,mongo3:27017/admin?replicaSet=rs0"

# ✔ 验证
curl -s localhost:9216/metrics | grep -m3 mongodb_up
```

> 用 Zabbix 的话，也可用 agent 用户参数包一条 `mongosh --quiet --eval` 取 PRIMARY 数量 / oplog 窗口做触发器，命令同上。

---

## 六、故障演练（把概念变成手感）

必须真做一次 —— 不演练你永远不知道"节点离线多久之后会追不上 oplog 而需要全量重同步"。

```bash
# 演练 1：主主动让位（不杀进程，业务无感切换）
mongosh --host 127.0.0.1 -u admin -p --authenticationDatabase admin --eval 'rs.stepDown(60)'
# 约 10 秒内另一台变 PRIMARY，原主变 SECONDARY

# 演练 2：直接停主（模拟宕机）
systemctl stop mongod        # 在当前 PRIMARY 那台上
# 去任意存活节点看 rs.status()：新 PRIMARY 当选，停掉的成员显示 (not reachable/healthy)

# 演练 3：恢复重加入
systemctl start mongod       # 原主节点上
# 该成员以 SECONDARY 身份重加入，通过 oplog 追平增量，不用手工同步数据
```

**验收判据**：

| 项目 | 期望 |
|---|---|
| 新 Primary 产生时间 | 秒级（通常 < 10s） |
| 应用是否可继续写 | 驱动开启重试写（retryWrites）后可自动恢复 |
| 原 Primary 回来后 | 变为 SECONDARY 并追平数据 |

---

## 七、常见报错速查表

| 报错/现象 | 根因 | 处理 |
|---|---|---|
| mongod 起不来，日志 `Illegal instruction (core dumped)` | CPU 无 AVX（5.0+ 硬要求） | `lscpu \| grep avx`；虚拟机开 AVX 透传；不行降级 4.4 |
| 远程连不上，本机正常 | bindIp 只写了 127.0.0.1 | 按节点 sed 加内网 IP 后重启 |
| rs.initiate 后成员全 `(not reachable/healthy)` | hosts 把 mongoX 解析到 127.0.0.1 / 防火墙没放行 / bindIp 不对 | `getent hosts mongo1`、`firewall-cmd --list-ports` 逐个查 |
| `Authentication failed` | authSource 不对 | 用户建在 admin 库 → 一律 `--authenticationDatabase admin` |
| `permissions on /etc/mongo-keyfile are too open` | keyFile 权限过宽 | `chmod 400` + `chown mongod:mongod`，三台都要 |
| 成员被踢 `clock skew ... too far` | 时间不同步 | chronyd 起来并对齐三台 |
| yum 报 404 / 找不到包 | repo 版本写错（8.0 在 RHEL7 不存在）或包名写错（没有 mongodb-org-shell） | 按本文 repo 配置与包名 |
| 重启后短暂 `(not primary/secondary)` | 正常选举窗口 | 等 10 秒再看 |
| `Failed to unlink socket file /tmp/mongodb-27017.sock` | 曾用 root 手跑过 mongod 留下属主错的 sock | `rm -f /tmp/mongodb-27017.sock` 再 `systemctl start`（以后只用 systemctl 管 mongod） |
| 节点离线回来触发 Initial Sync | oplog 太小，增量已被覆盖 | 放大 `oplogSizeMB`（可在线 `replSetResizeOplog`） |
| 从 Secondary 读报 `not primary` | 未开启从节点读 | `setReadPref("secondaryPreferred")` |
| 工具版本与 server 不一致 | 混装了不同大版本工具 | 工具与 server **大版本一致**即可；server 之间必须**完全一致** |

---

## 八、验收清单（全勾才算部署完成）

- [ ] 三台 `mongod --version` 均为 7.0.9；`systemctl status mongod` 均 active
- [ ] `rs.status()` = 1 PRIMARY + 2 SECONDARY，health 全 1
- [ ] mongo1 写入 testdb，mongo2 secondaryPreferred 可读（复制通）
- [ ] 无凭据访问被拒（`requires authentication`）；admin / appuser 均可登录
- [ ] 应用连接串（含 `replicaSet=rs0` & `authSource=admin`）能跑通读写
- [ ] mongodump 手工跑一次成功，`oplog.bson` 存在
- [ ] cron 备份任务在列，次日 `/data/backup` 出现日期目录
- [ ] mongorestore --drop 演练成功，数据可查
- [ ] THP = `[never]`；swappiness = 1；`cat /proc/$(pidof mongod)/limits` 句柄 64000
- [ ] rs.stepDown 演练通过：主从切换 ≤ 10 秒，业务连接串不断写

---

## 九、日常巡检清单

```bash
rs.status().members.forEach(m => print(m.name, m.stateStr, m.health));
rs.printReplicationInfo();                 // oplog 容量与可覆盖时长
rs.printSecondaryReplicationInfo();        // 各从节点延迟
db.serverStatus().opcounters;              // 各类操作计数（判断写入压力）
```

- Primary 是否只有一个（出现两个 = 脑裂，优先处理）；
- 从节点 `health = 1`、延迟在可接受范围；
- oplog `log length start to end` 是否明显大于"最长可能离线时长"；
- 磁盘剩余空间（oplog 与数据同盘时尤其要看）。

---

*版本事实核实日期：2026-09-04（官方仓库元数据实测；CentOS 7 上限 7.0，rhel7 最新构建 7.0.9）。若将来在 Rocky / Alma 8/9 上部署，流程完全一致，仅 repo 版本号换 8.0。*
