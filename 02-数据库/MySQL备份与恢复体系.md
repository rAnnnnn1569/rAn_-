# MySQL 自动化备份与一键恢复体系

> 目标：**每日自动全量 + binlog 增量归档 + 一键时间点恢复（PITR）**，且脚本自身带并发锁、空间护栏与完整性校验——**不把"备份"变成新的故障源**。
> 脚本实现见 [`../06-脚本工具/mysql备份恢复/`](../06-脚本工具/mysql备份恢复/)。

---

## 一、先把"备份"要解决的问题拆开

很多人做备份只想到"每天 dump 一份"，真出事时会发现四个问题一个都答不上来：

| 问题 | 如果没有准备 | 本次设计里的对应机制 |
|---|---|---|
| 备份**能不能恢复**？ | 文件在，导入报错 | 完整性三重校验 + 恢复前先校验 |
| 能恢复到**哪个时间点**？ | 只能回到昨晚 | binlog 归档 + `.pos` 位点 + PITR |
| 备份会不会**把盘写满**？ | 备份撑爆磁盘，反而压垮业务 | 空间阈值 + 保留期自动清理 |
| 会不会**同时跑两份**？ | cron 重入 / 上次没跑完 | `flock` 并发锁 |

下面按这四条展开。

---

## 二、分级保留矩阵

```
日备  每日 00:30   保留 7 天    —— 高频短期，覆盖"最近一周任意一天"
周备  每周日 01:30 保留 21 天   —— 低频长期，覆盖"最近三周"
```

- 两个 cron 走**同一支脚本的不同模式**（`full` / `week`），避免逻辑分叉。
- 除业务库外**同步覆盖账号目录（LDAP）**，保证账号数据与业务数据能一并还原——只备份业务库是常见疏漏，恢复后账号没了照样登不进去。

---

## 三、全量备份：一致性快照 + 位点提取

```bash
mysqldump --defaults-extra-file=/etc/mysql-backup/client.cnf \
  --single-transaction --quick --routines --events --triggers \
  --master-data=2 --set-gtid-purged=OFF \
  --default-character-set=utf8mb4 \
  --databases clbs \
  --ignore-table=clbs.big_media --ignore-table=clbs.big_log \
  > backup.sql
```

**每个参数都在解决一个具体问题**：

| 参数 | 解决什么 |
|---|---|
| `--single-transaction` | InnoDB 走一致性读事务，**备份期间不锁表**，业务持续写入时导出的仍是同一时间点快照 |
| `--quick` | 边查边写，不把整个结果集灌进内存（大表必须） |
| `--routines --events --triggers` | 带上存储过程/事件/触发器，否则恢复后这些全丢 |
| `--master-data=2` | 在 dump 里以**注释**形式记录 binlog 位点，供增量衔接 |
| `--set-gtid-purged=OFF` | 未启用 GTID 时避免导出 GTID 语句导致导入报错 |
| `--ignore-table` | 排除可重新生成的大表（媒体/日志），显著降低体积 |
| `--defaults-extra-file` | **凭据走配置文件，不写在命令行**（命令行会被 `ps aux` 看到） |

### 3.1 为什么要"先落 SQL 明文，再压缩删原文"

顺序是：`dump → 明文 .sql → 校验结尾 → 提取位点 → gzip → 删 .sql`。

- 明文阶段才能低成本地 `tail` 校验结尾、`grep` 提取位点；
- 压缩后立刻删原文，避免中间大文件长期占盘。

### 3.2 位点自动衔接——`.pos` 元数据

每次全量顺手写一份元数据，恢复脚本据此**自动**对上增量起点，不需要人记：

```
backup_file=clbs_date_20260910_003001.sql.gz
schema_file=clbs_date_20260910_003001_ignored_schema.sql.gz
backup_type=date
db_names=clbs
binlog_file=mysql-bin.000012
binlog_pos=12345678
created_at=2026-09-10 00:30:05
host=db-node
```

> 注意版本差异：MySQL 5.7 写成 `CHANGE MASTER TO MASTER_LOG_FILE/MASTER_LOG_POS`，8.0 可能写成 `SOURCE_LOG_FILE/SOURCE_LOG_POS`，解析要兼容两者。

---

## 四、增量归档：为什么不能用 `--stop-never`

**常见错误做法**：把 `mysqlbinlog --stop-never` 放进 cron 做"持续归档"。

`--stop-never` 的语义是 *"传到日志末尾不要停，继续等服务器产生新数据"*——本质是 **`tail -f`**。放进 cron 的后果：

- 每次触发都起一个新进程，**永久挂住**；
- 进程越堆越多，最终把机器拖垮。

**正确做法（三步）**：

```bash
# 1) 把当前正在写的日志"封口"，切出一个新的
mysql -e "FLUSH BINARY LOGS;"
# 2) 列出所有 binlog，跳过最后一个（那是正在写的、内容还会变）
SHOW BINARY LOGS;
# 3) 只拷贝"已闭合"的日志文件；已存在且大小一致的跳过（断点续传语义）
```

**成本收益**：代价是归档延迟最长一个 cron 周期（例如 30 分钟）；换来的是**永远只拷贝不可变文件**——不会拷到半截文件，重跑即续传。

> 归档目录**不要**和 MySQL 数据目录设成同一个，否则脚本会认为"已归档"而全部跳过（这个坑实测踩过）。

---

## 五、`--ignore-table` 的隐性缺陷（本项目最有价值的一处修复）

`mysqldump --ignore-table=db.tbl` 会跳过这张表的**数据**——但很多人不知道，它连**表结构**也一起跳过。

**后果**：恢复之后，这些表**根本不存在**。业务代码一访问就报表不存在，而备份"看起来是成功的"。这是典型的"恢复了但比没恢复更糟"。

**修复方式**：额外用 `--no-data` 单独导出被排除表的**结构**，随全量一并归档；恢复脚本导入全量后自动补回结构。

```bash
mysqldump --defaults-extra-file=... --no-data --skip-add-drop-table \
  --default-character-set=utf8mb4 clbs big_media >> ignored_schema.sql
```

- `--no-data`：只要结构不要数据；
- `--skip-add-drop-table`：**不生成 DROP TABLE**，避免恢复时误删已存在的表。

> 被排除表的数据本来就不在备份里，恢复后需要由业务回填或从历史附件恢复——**这一点必须提前和业务方说清楚**，否则"恢复了但数据是空的"会被当成新故障。

---

## 六、完整性三重校验

| 校验 | 防什么 |
|---|---|
| **退出码** | dump 中途失败（连接断、权限不足、磁盘满） |
| **SQL 结尾含 `Dump completed`** | 进程被杀导致的**截断**（文件存在、大小也不小，但内容是半截） |
| **`sha256`** | 落盘后静默损坏、传输不完整 |

```bash
# 结尾校验（dump 正常结束时 mysqldump 会写这一行）
tail -c 300 backup.sql | tr -d '\r' | tail -1 | grep -q "Dump completed" || 判失败

# 校验和落盘
sha256sum backup.sql.gz > backup.sql.gz.sha256
# 恢复前先校验，不过就拒绝恢复
sha256sum -c backup.sql.gz.sha256
```

**失败即清理**：三次校验任一不过，**删除该文件并告警**，绝不留下"半截备份"误导人。
> `sha256` 只能证明"与生成时一致"，**不能防篡改**——防篡改靠权限（`chmod 600`、属主 root）与副本隔离。

---

## 七、不让备份自己变成故障源

```bash
# 1) 并发锁（防止 cron 重入 / 上次没跑完）
exec 9>/var/run/mysql_backup.lock
flock -n 9 || { echo "已有备份任务在运行"; exit 0; }

# 2) 空间护栏（不足就中止，别把盘写满拖垮数据库）
avail=$(df -Pm "$BACKUP_ROOT" | awk 'NR==2{print $4}')
[ "$avail" -lt 2048 ] && { echo "剩余空间不足"; exit 1; }

# 3) 降优先级，避免抢占业务 IO
nice -n 10 mysqldump ...

# 4) 按保留期自动清理
find "$TARGET_DIR" -maxdepth 1 -type f -mtime +7 -print -delete
```

> **备份目录与数据库数据目录必须分开**，否则互相挤爆。这条听起来废话，但"备份盘和 data 盘同一块"是很常见的现场。

---

## 八、一键恢复 / 时间点恢复（PITR）

```bash
# 1) 纯全量（默认只做预检、不写库）
./restore.sh --full /opt/databk/date/mysql/clbs_date_20260910_003001.sql.gz

# 2) 确认无误后真正执行
./restore.sh --full <全量.sql.gz> --yes

# 3) 全量 + 增量，恢复到指定时间点
./restore.sh --full <全量.sql.gz> \
  --binlog /opt/databk/binlog/mysql-bin.000012 /opt/databk/binlog/mysql-bin.000013 \
  --stop-datetime "2026-09-10 09:25:00" --yes

# 4) 只打印将执行的命令
./restore.sh --full <全量.sql.gz> --dry-run
```

**恢复链路**：`sha256 校验 → 解压导入全量 → 补回被排除表结构 → 按位点回放 binlog`。

**设计原则：破坏性操作默认不执行**。不加 `--yes` 时只打印恢复计划（目标库、全量文件、增量文件、起始位点），确认后才写库。

### 8.1 binlog 回放的起始位点怎么定

- 全量备份从位点 X 开始，那么**第一个 binlog 从 X 开始回放**（`--start-position=X`），后续文件从头回放；
- 想恢复到"误删数据之前"，用 `--stop-datetime` 卡在误操作时刻**之前**。

### 8.2 一个真实踩坑：`mysqlbinlog` 会读全局 my.cnf

**报错**：`unknown variable 'default-character-set=utf8mb4'`

**原因**：`mysqlbinlog` 启动时会读全局配置（`/etc/my.cnf`），遇到它不认识的 `default-character-set` 直接报错退出。这个报错很容易被误判成"binlog 文件损坏"。

**解法**：回放时加 `--no-defaults`，绕开全局配置。

```bash
mysqlbinlog --no-defaults --start-position=12345678 mysql-bin.000012 | mysql -u... -p...
```

---

## 九、踩坑速查表（全部为实测复现）

| 坑 | 现象 | 正确做法 |
|---|---|---|
| `mysqlbinlog --stop-never` 放 cron | 永久挂住、进程堆积 | `FLUSH BINARY LOGS` 封口后只归档**已闭合**日志 |
| `tar -zcf x.tar.gz $DIR $FILE` | 把整个目录递归打包，体积滚动膨胀 | `tar -zcf x.tar.gz -C $DIR $FILE` |
| `--ignore-table` | 被排除表的**结构**也丢，恢复后表"消失" | 额外 `--no-data` 导出结构随全量归档 |
| `mysqlbinlog` 读全局 my.cnf | 报 `unknown variable` 直接退出 | 回放加 `--no-defaults` |
| 解析 `SHOW MASTER STATUS` 取位点 | `grep File` 后按字段取值为空 | 取 `Position` 列，或用 `--master-data=2` 的 `CHANGE MASTER TO` 行 |
| `[ 条件 ] && 动作` 在 `set -e` 下 | 条件为假时脚本被中断 | 一律写 `if ...; then ...; fi` |
| 归档目录 = 数据目录 | 脚本认为"已归档"，全部跳过 | 归档目录独立 |
| 口令写在命令行 | `ps aux` 可见 | `--defaults-extra-file` + `chmod 600` |
| `find -mtime +N` 只删 `.gz` | 留下 `.sha256` / `.pos` 孤儿文件 | 用 `-name '*备份名前缀*'` 一并清理 |

---

## 十、还没做但应该做的

- **异地/对象存储副本**：目前只有本地备份，本地盘损坏＝备份一起没（3-2-1 原则：3 份副本、2 种介质、1 份异地）。
- **定期恢复演练**：备份不做恢复演练＝没有备份。建议每月在隔离实例上跑一次完整"删库 → 恢复 → 核对行数"。
- **监控备份结果**：备份脚本的退出码要接入监控（没备份成功，和没备份是两回事）。
