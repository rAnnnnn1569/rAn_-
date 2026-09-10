# MySQL 生产级备份 / 一键恢复脚本

> 目标：**每日自动全量 + binlog 增量归档 + 一键时间点恢复（PITR）**，
> 且脚本自身带并发锁、空间护栏与完整性校验，不把"备份"变成新的故障源。

## 1. 文件说明

```
mysql备份恢复/
├── conf/
│   ├── backup.env       # 统一配置（路径、保留天数、备份对象、空间阈值、二进制路径）
│   └── client.cnf       # 数据库凭据（[client] 段，供 --defaults-extra-file 使用）
├── full_backup.sh       # 全量备份（日备 full / 周备 week）
├── binlog_backup.sh     # binlog 增量归档
├── restore.sh           # 一键恢复（全量 + binlog 回放 / 时间点恢复）
└── README.md
```

## 2. 部署步骤

```bash
# 1) 放置目录（示例）
mkdir -p /opt/script/mysql备份恢复/conf
cp -r mysql备份恢复/* /opt/script/mysql备份恢复/

# 2) 填写数据库凭据与备份对象
vi /opt/script/mysql备份恢复/conf/client.cnf.example     # user / password
vi /opt/script/mysql备份恢复/conf/backup.env     # DB_NAMES / IGNORE_TABLES / 保留天数

# 3) 凭据文件必须收紧权限（脚本会检查并告警）
chmod 600 /opt/script/mysql备份恢复/conf/client.cnf.example
chmod 600 /opt/script/mysql备份恢复/conf/backup.env
chown root:root /opt/script/mysql备份恢复/conf/client.cnf.example

# 4) 赋可执行权限
chmod +x /opt/script/mysql备份恢复/*.sh
```

> `backup.env` 中的 `MYSQLDUMP_BIN` / `MYSQL_BIN` / `MYSQLBINLOG_BIN` 等**必须按本机实际路径改**
> （`which mysqldump mysql mysqlbinlog` 确认），不要照抄默认值。

## 3. 定时任务

```cron
# 日备：每天 00:30，保留 7 天
30 0 * * * /opt/script/mysql备份恢复/full_backup.sh full  >> /opt/databk/logs/cron.log 2>&1

# 周备：每周日 01:30，保留 21 天
30 1 * * 0 /opt/script/mysql备份恢复/full_backup.sh week  >> /opt/databk/logs/cron.log 2>&1

# 增量：每 30 分钟归档一次已关闭的 binlog
*/30 * * * * /opt/script/mysql备份恢复/binlog_backup.sh    >> /opt/databk/logs/cron.log 2>&1
```

## 4. 恢复用法

```bash
# 纯全量恢复（先预检，不写库）
./restore.sh --full /opt/databk/date/mysql/clbs_date_20260910_003001.sql.gz

# 确认无误后执行
./restore.sh --full /opt/databk/date/mysql/clbs_date_20260910_003001.sql.gz --yes

# 全量 + 增量，恢复到指定时间点（PITR）
./restore.sh --full <全量.sql.gz> \
  --binlog /opt/databk/binlog/mysql-bin.000012 /opt/databk/binlog/mysql-bin.000013 \
  --stop-datetime "2026-09-10 09:25:00" --yes

# 只打印将执行的命令，不动数据
./restore.sh --full <全量.sql.gz> --dry-run
```

- 起始位点默认从全量的 `.pos` 元数据自动读取；也可用 `--start-position N` 显式指定。
- 被排除表（`IGNORE_TABLES`）的**结构**会随全量单独归档，恢复时自动补回——
  因为这些表的数据本就不在备份里，恢复后需由业务回填或从历史附件恢复。

## 5. 关键设计取舍（踩过的坑）

| 坑 | 现象 | 正确做法 |
|---|---|---|
| `mysqlbinlog --stop-never` 放 cron | 永久挂住、每次触发堆一个进程 | `FLUSH BINARY LOGS` 封口后只归档**已关闭**的 binlog |
| `tar -zcf x.tar.gz $DIR $FILE` | 把整个目录递归打包，体积暴涨 | `tar -zcf x.tar.gz -C $DIR $FILE` |
| `--ignore-table` | 被排除表的**结构**也一起丢，恢复后表"消失" | 额外 `--no-data` 导出结构随全量归档 |
| `mysqlbinlog` 读全局 `my.cnf` | 遇到 `default-character-set` 直接报错退出 | 回放时加 `--no-defaults` |
| `[ 条件 ] && 动作` 于 `set -e` | 条件为假时可能中断脚本 | 统一写成 `if ...; then ...; fi` |
| 解析 `SHOW MASTER STATUS` 取位点 | `grep File` 后字段数不对，取值为空 | 取 `Position` 列，或直接用 `--master-data=2` 的 `CHANGE MASTER TO` 行 |

## 6. 注意事项

- 备份**不做异地副本**只是本地保护；重要库建议再把备份目录同步到对象存储 / 异地。
- binlog 归档目录与服务器 binlog 目录**不要设成同一个**，否则脚本会全部跳过。
- 恢复是**覆盖式**导入（`--databases` 带建库语句），操作前务必确认目标实例。
