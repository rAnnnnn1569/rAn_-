# MongoDB 三节点副本集部署与运维

> 拓扑：**PSS**（1 Primary + 2 Secondary），三节点分处不同主机。
> 版本基线：MongoDB 7.0（server）+ 同大版本命令行工具。
> 目标：理解副本集"选主—复制—切换—恢复"的完整闭环，并能自己排障。

---

## 一、先搞清三个层次（别混在一起排障）

副本集出问题，报错信息往往很吓人，但根因几乎总落在这三件事之一：

| 层次 | 关键点 | 排障入口 |
|---|---|---|
| **配置一致** | 三台的 `replSetName` 必须完全相同 | `/etc/mongod.conf` 的 `replication.replSetName` |
| **能互相连** | `bindIp` 要允许其他节点连，防火墙要放端口 | `db.hello().members[].health` |
| **能互相认** | keyFile 三台**内容完全一致**、权限 `400` | 日志里 `Authentication failed` |

---

## 二、安装与目录规划

```bash
# 1) 配置官方 yum 源（以 7.0 为例；实际按官方当前的仓库文件名与支持的发行版核对）
cat > /etc/yum.repos.d/mongodb-org-7.0.repo <<'EOF'
[mongodb-org-7.0]
name=MongoDB Repository
baseurl=https://repo.mongodb.org/yum/redhat/$releasever/mongodb-org/7.0/x86_64/
gpgcheck=1
enabled=1
gpgkey=https://www.mongodb.org/static/pgp/server-7.0.asc
EOF

yum install -y mongodb-org

# 2) 目录规划（数据、日志、密钥分开）
mkdir -p /data/mongodb/{db,log}
mkdir -p /etc/mongo
chown -R mongod:mongod /data/mongodb
```

> **版本管理粒度**：`mongod`（服务端）**必须严格一致**，三个节点版本必须相同；`mongosh` / `mongodump` / `mongorestore` 等**工具**只要求大版本一致即可，小版本可以不同。升级时先理解这条，能避免"工具版本和 server 不一致就慌了"。

**先在每台机器上确认时间同步**（副本集选举与 oplog 时间戳都依赖时钟）：

```bash
chronyc sources -v      # 期望看到 ^* 指向同步源
timedatectl             # 确认时区与同步状态
```

---

## 三、keyFile 认证

```bash
# 三台机器上用同一条命令生成"同一个"keyFile —— 更稳妥的做法是生成一次再分发
openssl rand -base64 756 > /etc/mongo/keyfile

# 权限必须严格（MongoDB 强制要求）
chmod 400 /etc/mongo/keyfile
chown mongod:mongod /etc/mongo/keyfile
```

**三个必须**：

1. **内容三台完全一致**（不一致 → 节点间互相认证失败）；
2. **权限只能是 `400`**（宽了 MongoDB 直接拒绝启动，日志会明确说 keyFile 权限问题）；
3. **属主是 `mongod`**（不是 root，否则服务读不到）。

---

## 四、`/etc/mongod.conf` 关键配置

```yaml
net:
  port: 27017
  bindIp: 0.0.0.0            # 生产建议显式列出三台的内网地址，而非 0.0.0.0

storage:
  dbPath: /data/mongodb/db

systemLog:
  destination: file
  path: /data/mongodb/log/mongod.log
  logAppend: true

processManagement:
  fork: true

replication:
  replSetName: rs0           # ⚠️ 三台必须完全一致
  oplogSizeMB: 10240         # 见下方"oplog 怎么定"

security:
  keyFile: /etc/mongo/keyfile
```

### 4.1 oplog 怎么定（副本集最容易配错的一项）

**oplog 是什么**：一个**固定大小的环形集合（capped collection）**，记录所有写操作。Secondary 靠它追数据；**如果你要从备份恢复新节点，它决定"能追多久的增量"**。

- 太小 → 节点短暂离线后回来，需要的 oplog 已经被覆盖，只能**全量重同步**（Initial Sync，代价极高）；
- 太大 → 白占磁盘。

**估算思路**：`oplog 容量 ≥ 预计最长离线时长 × 峰值写入速率`。给个直观参照：默认大小通常是可用磁盘的 5%，**低写入负载场景够用，高写入场景（如轨迹数据）必须手工放大**。

```bash
# 运行中查看 oplog 实际使用情况
mongosh --quiet --eval 'rs.printReplicationInfo()'
# 输出含：configured oplog size / log length start to end（可容纳多长时间）
```

**动态调整 oplog 大小**（7.0 支持在线调整）：

```javascript
db.adminCommand({ replSetResizeOplog: 1, size: 20480 })   // 单位 MB
```

> 判断"oplog 够不够"的一句话结论：`printReplicationInfo()` 输出的 **log length start to end** 如果小于"节点最长可能离线时长"，就必须放大。

---

## 五、初始化副本集

```bash
systemctl enable --now mongod

mongosh --host 192.0.2.11:27017
```

```javascript
// 只在其中一台执行一次
rs.initiate({
  _id: "rs0",
  members: [
    { _id: 0, host: "192.0.2.11:27017", priority: 2 },
    { _id: 1, host: "192.0.2.12:27017", priority: 1 },
    { _id: 2, host: "192.0.2.13:27017", priority: 1 }
  ]
});
```

- `_id` 必须与 `mongod.conf` 里的 `replSetName` **完全一致**；
- `host` 用**其他节点能访问到的地址**（写 `127.0.0.1` 会导致其他节点连不上自己——高频错误）；
- `priority` 高的更可能被选为 Primary。

**验证**：

```javascript
rs.status()                 // members[].stateStr 应为 PRIMARY / SECONDARY
db.hello()                  // 7.0 推荐用 hello（isMaster 已废弃）
rs.conf()                   // 看配置是否三台都在
```

**在 Secondary 上读数据**（默认不允许）：

```javascript
rs.secondaryOk()            // 旧写法
db.getMongo().setReadPref("secondaryPreferred")   // 更推荐：按读偏好
```

---

## 六、主从切换演练（必须真做一次）

**手动切换**（把 Primary 降级，触发重新选举）：

```javascript
rs.stepDown(60)             // 降级并在 60 秒内不参与选举
```

**观察**：

```bash
# 切换前记录谁是 Primary
mongosh --quiet --eval 'db.hello().primary'
# 降级后立刻再查，应在数秒内指向另一节点
```

**验收判据**：

| 项目 | 期望 |
|---|---|
| 新 Primary 产生时间 | 秒级（通常 < 10s） |
| 应用是否可继续写 | 驱动开启重试写（retryWrites）后可自动恢复 |
| 原 Primary 回来后 | 变为 SECONDARY 并追平数据 |

**故障切换（模拟节点宕机）**：

```bash
systemctl stop mongod        # 在 Primary 上执行
# 观察另外两台是否选出新 Primary
# 恢复后启动原节点，应自动以 SECONDARY 身份加入并追数据
```

> **演练的意义**：不演练你永远不知道"节点离线多久之后会追不上 oplog 而需要全量重同步"。这正是 §4.1 要放大 oplog 的原因。

---

## 七、备份：`mongodump --oplog` 为什么必要

```bash
mongodump --host rs0/192.0.2.11:27017,192.0.2.12:27017,192.0.2.13:27017 \
  --oplog --gzip --out /backup/mongo/$(date +%F)
```

- `--host rs0/...`：**用副本集连接串**，让驱动自动找 Primary；
- **`--oplog`**：在 dump 期间同时抓取 oplog，让备份文件具备**某个一致时间点**的语义。没有它，dump 多个集合时可能"这个集合是 10:00 的快照、那个是 10:05 的"，跨集合数据对不上；
- 恢复时配合 `mongorestore --oplogReplay` 把 oplog 部分重放，得到一致状态。

> **和 MySQL 的对照**：`mysqldump --single-transaction` 靠事务拿一致性快照；MongoDB 没有跨集合事务语义时，就靠 `--oplog` 拿一致点。两个数据库"一致性备份"的手段不同，但**要解决的问题是同一个**。

---

## 八、踩坑速查表

| 现象 | 根因 | 处理 |
|---|---|---|
| 节点启动失败，日志报 keyFile 权限 | 权限不是 `400` | `chmod 400` + `chown mongod:mongod` |
| 节点间 `Authentication failed` | keyFile 内容不一致 | 三台统一为同一份 keyFile |
| `rs.initiate` 后只有一台是 PRIMARY，另两台不可达 | `host` 填了 `127.0.0.1` 或 firewall 未放 27017 | 填内网可达地址 + 放行端口 |
| 节点离线回来触发 Initial Sync | oplog 太小，增量已被覆盖 | 放大 `oplogSizeMB`（可在线 `replSetResizeOplog`） |
| 三台 `replSetName` 不一致 | 配置文件没统一 | 统一后重启（不一致会直接起不来或无法加入） |
| 从 Secondary 读报 `not primary` | 未开启从节点读 | `setReadPref("secondaryPreferred")` |
| 时间漂移导致选举异常 | chrony 未同步 | `chronyc sources -v` 确认 `^*` |
| 工具版本与 server 不一致 | 混装了不同大版本工具 | 工具与 server 保持**大版本一致**即可，server 之间必须**完全一致** |

---

## 九、运维日常检查清单

```javascript
rs.status().members.forEach(m => print(m.name, m.stateStr, m.health));
rs.printReplicationInfo();                 // oplog 容量与可覆盖时长
rs.printSecondaryReplicationInfo();        // 各从节点延迟
db.serverStatus().opcounters;              // 各类操作计数（判断写入压力）
```

- Primary 是否只有一个（出现两个 = 脑裂，优先处理）；
- 从节点 `health = 1`、延迟在可接受范围；
- oplog `log length start to end` 是否明显大于"最长可能离线时长"；
- 磁盘剩余空间（oplog 与数据同盘时尤其要看）。
