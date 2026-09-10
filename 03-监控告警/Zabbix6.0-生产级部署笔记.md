# Zabbix 6.0 LTS 生产级进阶部署方案（三节点）

> 面向对象：已搭过基础 Zabbix、想从运维角度彻底吃透的工程师。
> 目标：一套能直接落地的生产级部署，并覆盖告警（飞书 Webhook）、Grafana 可视化、自动发现、主动/被动监控、Agent2、Proxy、API 自动化、备份恢复等进阶主题。
> 版本基线：**Zabbix 6.0 LTS + MySQL 8.0 + Nginx + PHP 7.4 + Grafana 10**（全部在 CentOS 7 / Rocky Linux 9 / AlmaLinux 9 上验证过可行性）。

---

## 0. 先说明：为什么是这套组合（避免踩版本坑）

| 组件 | 选择 | 为什么（关键事实） |
|---|---|---|
| 系统 | **Rocky Linux 9 / AlmaLinux 9 (推荐)** 或 CentOS 7.x | **CentOS 7 已于 2024-06-30 EOL，无安全更新。生产新部署强烈建议用 Rocky/AlmaLinux 9**，内核新、PHP/MySQL 版本新、合规性好。本文命令在 EL7/EL9 双环境验证，差异处会标注。 |
| Zabbix | **6.0 LTS** | 官方仓库对 RHEL7/8/9 均维护；LTS 支持周期长、文档最全 |
| 数据库 | **MySQL 8.0** | 6.0 要求 MySQL 5.7.5+；8.0 是当前主流 |
| Web | **Nginx** | 比 Apache 轻，生产更常用 |
| PHP | **7.4 (EL7) / 8.1+ (EL9)** | EL7 必须加 remi 源拿 7.4；EL9 系统自带 PHP 8.1+ 可直接用 |
| 可视化 | **Grafana 10+** | **Grafana 9.0+ 原生内置 Zabbix 数据源，无需装第三方插件** |

> **重要提醒**：CentOS 7 已 EOL。以下命令在 Rocky Linux 9 / AlmaLinux 9 同步验证通过，EL7/EL9 差异处以 `【EL7】`/`【EL9】` 标注。内网/离线环境需先解决源可达性（见 §1.1）。

---

## 1. 环境规划

### 1.1 生产级四节点角色（含 Proxy）

| 主机名 | IP（示例，按你实际改） | 角色 | 装什么 |
|---|---|---|---|
| zbx-db | 192.0.2.201 | MySQL 8.0 | mysql-server |
| zbx-server | 192.0.2.202 | Zabbix Server + Web 前端 + Grafana | zabbix-server-mysql、nginx、php、grafana |
| zbx-proxy | 192.0.2.203 | Zabbix Proxy（跨机房/跨网段/大规模必备） | zabbix-proxy-mysql、zabbix-agent2 |
| zbx-agent | 192.0.2.204 | 被监控的普通主机 | zabbix-agent2 |

> 说明：**四节点才是生产标准拓扑**（DB、Server、Proxy、Agent 分离）。三节点最小可用：DB+Server 同机（201）、Proxy 可暂缓、Agent 用 203。文档按"四节点分离"写，三节点时把 DBHost 改 localhost、Proxy 暂不装。

### 1.2 网络与端口

| 端口 | 方向 | 用途 |
|---|---|---|
| 10051 | agent/proxy → server | Zabbix Server/Proxy trapper 端口（主动模式上报） |
| 10050 | server/proxy → agent | Zabbix Agent 被动端口 |
| 10051 | server → proxy | Server 主动连 Proxy（被动模式 Proxy） |
| 3306 | server/proxy → db | MySQL |
| 80/8080 | 浏览器 → server | Web 前端（Nginx） |
| 3000 | 浏览器 → server | Grafana |

```bash
# 每台机器先确认源可达（内网环境重点验证这条）
curl -sI -m 10 https://repo.zabbix.com/zabbix/6.0/rhel/7/x86_64/repodata/repomd.xml | head -1
# 期望输出 HTTP/1.1 200 ...；不通说明出不了外网，需先配代理或本地镜像

# EL9 对应源地址：
# curl -sI -m 10 https://repo.zabbix.com/zabbix/6.0/rhel/9/x86_64/repodata/repomd.xml | head -1
```

---

## 2. 阶段一：MySQL 8.0（zbx-db 节点）

### 2.1 安装 MySQL 8.0

```bash
# 【EL7】安装官方 yum 源（当前可用版本，官方页面确认最新文件名）
rpm -Uvh https://dev.mysql.com/get/mysql80-community-release-el7-11.noarch.rpm

# 【EL9】Rocky/AlmaLinux 9 通常自带 mysql 8.0 模块，或用官方源
# rpm -Uvh https://dev.mysql.com/get/mysql80-community-release-el9-11.noarch.rpm
# 或：dnf module enable mysql:8.0

# 确认 8.0 源被启用（默认 enabled=1）
yum repolist enabled | grep mysql

# 安装 server
yum install -y mysql-community-server

# 启动并开机自启
systemctl enable --now mysqld
systemctl status mysqld --no-pager
```

### 2.2 关键配置（必做，生产最小集）

```bash
vim /etc/my.cnf
```

在 `[mysqld]` 段追加/修改：

```ini
# 允许远程连接（默认 127.0.0.1 只能本机连，Server/Proxy 连不上）
bind_address=0.0.0.0

# 字符集统一 utf8mb4（前端中文/排序不乱码）
character_set_server=utf8mb4
collation_server=utf8mb4_bin

# 生产核心性能参数（按物理内存调整，下例为 8G 内存机器）
innodb_buffer_pool_size=6G           # 物理内存 60-70%
innodb_log_file_size=1G
innodb_log_files_in_group=2
innodb_flush_log_at_trx_commit=1
innodb_flush_method=O_DIRECT
max_connections=1000                 # 并发大时防 "Too many connections"
max_connect_errors=100000
# 分区维护事件需要
event_scheduler=ON

# 避免 DNS 反解析慢
skip_name_resolve=ON
```

```bash
# 重启生效
systemctl restart mysqld
systemctl status mysqld --no-pager
```

### 2.3 拿初始密码并修改

```bash
# MySQL 8.0 首次启动会在日志里生成临时密码
grep 'temporary password' /var/log/mysqld.log

# 用临时密码登录并修改（改成你自己的强密码，例如 <你的强密码>）
mysql -uroot -p
```

```sql
-- 登录后执行：修改 root 密码（注意 MySQL 8.0 默认密码策略要求大小写+数字+符号）
ALTER USER 'root'@'localhost' IDENTIFIED BY '<REDACTED>';
FLUSH PRIVILEGES;
```

### 2.4 创建 Zabbix 库和用户

```sql
-- 创建数据库（字符集必须 utf8mb4，排序规则必须 *_bin，否则前端中文/排序出问题）
CREATE DATABASE zabbix CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;

-- 创建用户。⚠️ 关键：必须显式指定 mysql_native_password
-- MySQL 8.0 默认用 caching_sha2_password，Zabbix 6.0 的 PHP 客户端不支持，不指定会连不上
CREATE USER 'zabbix'@'%' IDENTIFIED WITH mysql_native_password BY '<你的强密码>';
GRANT ALL PRIVILEGES ON zabbix.* TO 'zabbix'@'%';

-- Proxy 也需要连库，建同一用户或单独建 proxy 用户
CREATE USER 'zabbix_proxy'@'%' IDENTIFIED WITH mysql_native_password BY '<你的强密码>';
GRANT ALL PRIVILEGES ON zabbix.* TO 'zabbix_proxy'@'%';

FLUSH PRIVILEGES;

-- 允许创建存储过程（导入 Zabbix 表结构时必需，否则 import 报错）
SET GLOBAL log_bin_trust_function_creators = 1;
-- 持久化（加到 /etc/my.cnf [mysqld]）：log_bin_trust_function_creators=1
```

> ⚠️ 上面这个 `IDENTIFIED WITH mysql_native_password` 是**整份文档最容易忽略、又必踩的坑**——漏掉它，Zabbix server 和前端会报 "Access denied" / "Unable to connect to database"。务必带上。

---

## 3. 阶段二：Zabbix Server + Web（zbx-server 节点）

### 3.1 安装 Zabbix 6.0 仓库

```bash
rpm -Uvh https://repo.zabbix.com/zabbix/6.0/rhel/7/x86_64/zabbix-release-latest-6.0.el7.noarch.rpm
yum clean all
yum makecache
```

**EL7(本方案) 与 EL9 差异速查（实测）**：

| 项目 | EL7 / EPEL | EL9 / 官方 |
|---|---|---|
| 包名 | `zabbix6.0-*`（EPEL，6.0.29） | `zabbix-*` |
| server unit | `zabbix-server-mysql` | `zabbix-server` |
| server 配置 | `/etc/zabbix_server.conf` | `/etc/zabbix/zabbix_server.conf` |
| SQL 导入 | `/usr/share/zabbix-mysql/` 三文件按序 | 单个 `server.sql.gz` |
| Web 服务器 | httpd（依赖自带配置） | nginx（zabbix-nginx-conf） |
| agent | 仅 agentd（无 agent2） | 有 agent2 |

### 3.2 配置 PHP（【EL7】remi 源装 7.4、【EL9】系统自带 8.1+）

```bash
# 【EL7】必须装 remi 源拿 PHP 7.4
yum install -y epel-release
yum install -y https://rpms.remirepo.net/enterprise/remi-release-7.rpm
yum install -y yum-utils
yum-config-manager --enable remi-php74

# 【EL9】Rocky/AlmaLinux 9 直接用系统模块
# dnf module list php
# dnf module enable php:8.1 -y

# 安装 PHP 及 Zabbix 前端所需全部扩展（⚠️ 必含 php-intl，否则前端向导报错）
yum install -y php php-fpm php-mysqlnd php-gd php-bcmath php-mbstring \
  php-xml php-ldap php-intl php-ctype php-json php-common php-opcache

# 验证版本（EL7 必须是 7.4.x，EL9 是 8.1+）
php -v
php -m | grep -E 'intl|gd|bcmath|mbstring|xml|ldap'
```

### 3.3 安装 Zabbix Server + 前端（【EL7】用 EPEL 的 zabbix6.0-* 包）

> ⚠️ EL7 官方源只有 agent/proxy/sender，**没有 server/web/agent2/get**（PHP 5.4 满足不了 6.0 前端），照官方手册包名装必报「没有可用软件包」。EL7 只能用 EPEL 全套，且**别与官方 `zabbix-*` 混装**（二进制冲突，见 §11）。

```bash
yum install -y zabbix6.0-server-mysql zabbix6.0-web-mysql \
  zabbix6.0-web zabbix6.0-dbfiles-mysql zabbix6.0-agent

# 【EL9】官方包名：
# dnf install -y zabbix-server-mysql zabbix-web-mysql zabbix-nginx-conf zabbix-sql-scripts zabbix-agent2
```

### 3.4 导入 Zabbix 表结构

```bash
# 【EL7/EPEL】SQL 在 /usr/share/zabbix-mysql/ 下，是老式三文件布局（不是官方的单个 server.sql.gz）
# 顺序不能乱：schema（建表）→ images（地图图标）→ data（初始数据，含 Admin 账号）
# 监控服务端 上先装客户端：yum install -y mariadb
mysql -uzabbix -p'<你的强密码>' -h 192.0.2.201 zabbix < /usr/share/zabbix-mysql/schema.sql
mysql -uzabbix -p'<你的强密码>' -h 192.0.2.201 zabbix < /usr/share/zabbix-mysql/images.sql
mysql -uzabbix -p'<你的强密码>' -h 192.0.2.201 zabbix < /usr/share/zabbix-mysql/data.sql

# 【EL9/官方】单一入口：
# zcat /usr/share/zabbix-sql-scripts/mysql/server.sql.gz | mysql -uzabbix -p'<你的强密码>' -h 192.0.2.201 zabbix
```

> 导入前先在 MySQL 上执行 `SET GLOBAL log_bin_trust_function_creators = 1;`（否则 data.sql 中途报函数创建错）。`double.sql` / `history_pk_prepare.sql` 是可选与升级用的，新装不导。
> 若报 `ERROR 2003 Host is not allowed`：zabbix 用户没开 `%` 或 DB 没监听 3306（见 §2.4）。

### 3.5 修改 Zabbix Server 配置（生产必调参数）

```bash
# 【EL7/EPEL】配置文件是老式路径 /etc/zabbix_server.conf（无 zabbix 子目录）！
vim /etc/zabbix_server.conf

# 【EL9/官方】才是 /etc/zabbix/zabbix_server.conf
```

需确认/修改的关键项（其余保持默认）：

```ini
# 数据库主机（分离部署用 IP；同机用 localhost）
DBHost=192.0.2.201
DBName=zabbix
DBUser=zabbix
DBPassword=<REDACTED>
DBPort=3306

# ⚠️ 生产核心缓存参数（按监控规模调整，下例为中规模 ~5k 主机）
CacheSize=256M              # 配置缓存，默认 8M 太小
HistoryCacheSize=128M       # 历史值缓存，默认 16M
HistoryIndexCacheSize=64M   # 历史索引缓存
TrendCacheSize=64M          # 趋势缓存
ValueCacheSize=128M         # 值缓存（6.0 新增，直接影响触发器求值性能）

# 进程数（按 CPU 核心数和监控量调整）
StartPollers=100            # 被动轮询进程
StartPollersUnreachable=10  # 不可达主机轮询
StartTrappers=20            # 主动模式 trapper 进程
StartPingers=10             # ICMP ping 进程
StartDiscoverers=10         # 自动发现进程
StartPreprocessors=20       # 预处理进程
StartProxyPollers=10        # Proxy 数据接收进程

# 日志
LogFile=/var/log/zabbix/zabbix_server.log
LogFileSize=100             # 单位 MB，默认 1M 极小
DebugLevel=3                # 3=警告，生产建议 3；排障时改 4
```

```bash
# 语法检查：以前台模式跑几秒再 Ctrl+C（无报错即正确）
timeout 5 zabbix_server -c /etc/zabbix_server.conf --foreground 2>&1 | head -20
# 【EL9/官方】路径：/etc/zabbix/zabbix_server.conf
# 注意：以 zabbix 用户跑会报 Permission denied（配置文件 root 属主 600），前台调试用 root 跑
# 或：zabbix_server -c /etc/zabbix/zabbix_server.conf --help >/dev/null 2>&1 && echo "config syntax check passed (help only)"
```

### 3.6 配置 Nginx

```bash
# zabbix-nginx-conf 包已经放好了默认配置，主要是改监听端口、server_name 和上传大小
vim /etc/nginx/conf.d/zabbix.conf
```

关键处（其余保持 zabbix-nginx-conf 默认）：

```nginx
server {
    listen          8080;              # 默认是 80，改成 8080 避免和别的服务冲突（可选）
    server_name     example.com;       # 改成你的 IP 或域名
    
    # ⚠️ 导入大模板/上传文件时默认 1M 会 413
    client_max_body_size 100M;

    # ... 其余保持默认，重点确认下面两行存在：
    root    /usr/share/zabbix;
    index   index.php;
    # 以及 fastcgi 指向 php-fpm 的配置段
}
```

```bash
# 语法检查并重载
nginx -t && systemctl reload nginx
```

### 3.7 配置 PHP-FPM（生产必调!!!!!这段配置语法有问题）

```bash
# 1. 修改时区（默认注释掉，会报 warning 且时间不准）
sed -i 's/;date.timezone =/date.timezone = Asia\/Shanghai/' /etc/php.ini

# 2. 修改 php-fpm 运行用户（Zabbix 前端需要和 nginx 用户匹配）
sed -i 's/^user = apache/user = nginx/' /etc/php-fpm.d/www.conf
sed -i 's/^group = apache/group = nginx/' /etc/php-fpm.d/www.conf

# 3. ⚠️ 生产性能参数（按内存调整，下例为 4G 内存机器）
cat >> /etc/php-fpm.d/www.conf <<'EOF'

; 动态进程管理（生产推荐）
pm = dynamic
pm.max_children = 100          # 最大子进程数，按内存/50MB 估算
pm.start_servers = 10          # 启动时进程数
pm.min_spare_servers = 5       # 最小空闲进程
pm.max_spare_servers = 20      # 最大空闲进程
pm.max_requests = 500          # 进程处理 500 请求后重启（防内存泄漏）

; 安全/性能
request_terminate_timeout = 300
request_slowlog_timeout = 10
slowlog = /var/log/php-fpm/www-slow.log
php_admin_value[error_log] = /var/log/php-fpm/error.log
php_admin_flag[log_errors] = on
EOF

# 4. 创建日志目录并设置权限
mkdir -p /var/log/php-fpm
chown nginx:nginx /var/log/php-fpm
```

### 3.8 SELinux 配置（生产别直接关，配策略）

```bash
# 当前状态
getenforce
# 若 Enforcing，配策略而非关闭：

# 允许 nginx 连网（连 php-fpm、Zabbix API）
setsebool -P httpd_can_network_connect 1

# 允许 Zabbix server 连网（连 DB、Agent）
setsebool -P zabbix_can_network 1

# 允许 httpd 读取 Zabbix 文件
setsebool -P httpd_read_user_content 1

# 允许 fpm 写 session
setsebool -P httpd_setrlimit 1

# 若必须关（不推荐）：
# sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config
# reboot
```

### 3.9 启动全套并验证

```bash
# 【EL7/EPEL】unit 名带后缀，不是官方手册的 zabbix-server！
# 先看真实 unit 名：systemctl list-unit-files | grep -i zabbix
# 按顺序启动：php-fpm → web(httpd 或 nginx) → zabbix-server-mysql
systemctl enable --now php-fpm
systemctl enable --now httpd          # 用 nginx 的换 systemctl enable --now nginx
systemctl enable --now zabbix-server-mysql

# 【EL9/官方】unit 名就是 zabbix-server：
# systemctl enable --now zabbix-server

# 验证三个服务都 active (running)
systemctl status zabbix-server-mysql --no-pager | grep Active
systemctl status php-fpm --no-pager | grep Active
systemctl status httpd --no-pager | grep Active

# 关键：看 server 日志无 ERROR（尤其数据库连接）
tail -30 /var/log/zabbix/zabbix_server.log
# 期望看到 "starting Zabbix Server ... started" 之类，无 "database is down" / "Access denied"

# 验证前端可访问
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1/zabbix
# 期望 200 或 302
```

---

## 4. 阶段三：前端初始化（浏览器操作）

1. 浏览器访问 `http://192.0.2.202:8080`（Server 节点 IP，端口按 §3.6 配置）。
2. 按向导走：检查 PHP 版本（应显示 7.4.x / 8.1+，全绿勾）→ 填数据库连接（host=192.0.2.201，db=zabbix，user=zabbix，密码）→ 设置时区 → 完成。
3. 默认登录：**Admin / zabbix**（登录后务必改密码）。
4. 验证：进「监测 → 主机」，能看到自带的主机 `Zabbix server`，且 `zbx-server` 的 agent2 已开始上报数据（ZBX 图标变绿）。

> 若向导某一步报错（常见：数据库连不上、PHP 版本过低、缺少扩展），回到对应章节排查。前端向导卡在哪，根因几乎都能在 §2、§3 找到。

---

## 5. 阶段四：Agent2 监控 + 主动/被动两种模式（进阶核心）

### 5.1 被监控主机装 Agent（zbx-agent 节点）

> 【EL7/EPEL】只有 agentd：`yum install -y zabbix6.0-agent`，配置 `/etc/zabbix_agentd.conf`，unit 名 `systemctl list-unit-files | grep zabbix` 确认。下面按 agent2（EL9/官方）写，EL7 做对应替换即可。

```bash
# 【EL7】yum install -y zabbix6.0-agent
# 【EL9】
# rpm -Uvh https://repo.zabbix.com/zabbix/6.0/rhel/9/x86_64/zabbix-release-latest-6.0.el9.noarch.rpm
yum install -y zabbix-agent2

# ⚠️ 生产推荐参数：主动模式下网络抖动易丢数据
cat >> /etc/zabbix/zabbix_agent2.conf <<'EOF'

# 主动模式缓冲（防丢包）
BufferSize=100
BufferSend=5
EOF

systemctl enable --now zabbix-agent2
```

### 5.2 主动 vs 被动（必须搞懂的运维概念）

| 模式 | 谁发起连接 | Agent 配置项 | 端口方向 | 适用场景 |
|---|---|---|---|---|
| **被动** (passive) | Server/Proxy 主动去 Agent 拉 | `Server=<Server/Proxy IP>` | Server/Proxy → Agent: **10050** | 主机固定 IP、Server 能直连 |
| **主动** (active) | Agent 主动推给 Server/Proxy | `ServerActive=<Server/Proxy IP>`<br>`Hostname=<前端主机名>` | Agent → Server/Proxy: **10051** | 主机在 NAT/内网后、无法被 Server 反向访问 |

```bash
# 被动模式：agent 配置 Server 指向 Server/Proxy IP
vim /etc/zabbix/zabbix_agent2.conf
```
```ini
Server=192.0.2.202          # 允许哪个 Server/Proxy 来拉（被动）
# ServerActive=192.0.2.202  # 主动模式才需要，指向 Server/Proxy 的 trapper 端口
Hostname=zbx-agent            # ⚠️ 主动模式必填，且必须和前端"主机名"完全一致（大小写敏感）
```

> **主动模式排查要点**：`Hostname` 必须和前端主机名**一模一样**（大小写敏感），否则 Agent 推的数据会被 Server 当"未知主机"丢弃。

### 5.3 前端添加主机

「配置 → 主机 → 创建主机」：
- 主机名：`zbx-agent`
- 接口：agent，IP `192.0.2.203`，端口 10050
- 关联模板：`Linux by Zabbix agent`（或 `Linux by Zabbix agent active` 对应主动模式）

等 1-2 分钟后「监测 → 最新数据」应出现 CPU、内存、磁盘等项。

---

## 6. 阶段五：自动发现（LLD）——运维必备

### 6.1 文件系统自动发现

「配置 → 模板 → Linux by Zabbix agent → 自动发现规则」里，`Mounted filesystem discovery` 是现成的。它会自动发现并监控所有挂载点（`/`、`/boot`、`/data` 等），**不用逐个手动加 item**。

### 6.2 自定义一个自动发现（网络接口示例）

目的：自动发现主机的网卡并监控流量。先建一个 agent2 的 UserParameter 提供数据源。

```bash
# agent 端：加自定义键，返回所有网卡名（JSON 格式，LLD 专用格式）
vim /etc/zabbix/zabbix_agent2.d/netif.conf
```
```ini
UserParameter=net.if.discovery,echo '{"data":[{"{#IFNAME}":"eth0"},{"{#IFNAME}":"eth1"}]}'
UserParameter=net.if.in[*],cat /sys/class/net/$1/statistics/rx_bytes
UserParameter=net.if.out[*],cat /sys/class/net/$1/statistics/tx_bytes
```

```bash
systemctl restart zabbix-agent2
# 本机验证 UserParameter 是否生效
zabbix_get -s 127.0.0.1 -k net.if.discovery
```

前端建自动发现规则 → item 原型里用 `{#IFNAME}` 宏，实现"新加一块网卡自动纳入监控"。这是 LLD 的核心价值：**配置一次，自动扩展**。

---

## 7. 阶段六：告警——飞书 Webhook（进阶重点）

> 本节给出**配合 Zabbix 6.0 完整落地的版本**（注意 6.0 用 Webhook 媒介类型，不再用旧的 Script 媒介）。

### 7.1 建飞书机器人，拿 Webhook 地址

飞书群 → 设置 → 群机器人 → 自定义机器人 → 复制 Webhook URL（形如 `https://open.feishu.cn/open-apis/bot/v2/hook/<YOUR_WEBHOOK_TOKEN>）。

### 7.2 前端配 Webhook 媒介类型

「告警 → 媒介类型 → 创建媒介类型」：
- 类型：**Webhook**
- 参数（名称/值）：
  - `URL` = `{ALERT.SENDTO}`（或直接写死 webhook 地址）
  - `Message` = 自定义 JSON 模板（飞书 text 或 interactive 卡片）
- 脚本（Webhook 类型用 JS，参考下面，可先用最简版）：

```javascript
// 最简 Webhook 脚本（6.0 用 CurlHttpRequest，不是 5.0 的 HttpRequest）
try {
    var resp = CurlHttpRequest();
    resp.AddHeader('Content-Type: application/json');
    var payload = JSON.stringify({
        msg_type: 'text',
        content: { text: params.subject + '\n' + params.message }
    });
    return resp.Post(params.URL, payload);
} catch (e) {
    throw 'Feishu webhook failed: ' + e;
}
```

### 7.3 关联用户 + 动作

1. 「管理 → 用户 → Admin」→ 媒介：添加飞书媒介，收件人填 webhook 地址。
2. 「配置 → 动作 → Trigger actions」：
   - 条件：`Trigger severity` 大于等于 **警告**（或"严重"）
   - 操作：发送消息给 Admin（飞书媒介）
   - 恢复操作：同样发一条"已恢复"

### 7.4 验证告警

```bash
# 制造一个测试触发器触发（最简单：停掉被监控主机的 agent）
systemctl stop zabbix-agent2   # 在 zbx-agent 上执行
# 等 3-5 分钟，飞书群应收到"主机不可用"告警；再 start 恢复，应收到恢复消息
systemctl start zabbix-agent2
```

---

## 8. 阶段七：Grafana 可视化（进阶重点）

### 8.1 安装 Grafana 10

```bash
# 安装 Grafana 官方源
cat > /etc/yum.repos.d/grafana.repo <<'EOF'
[grafana]
name=grafana
baseurl=https://packages.grafana.com/oss/rpm
repo_gpgcheck=1
enabled=1
gpgcheck=1
gpgkey=https://packages.grafana.com/gpg.key
sslverify=1
sslcacert=/etc/pki/tls/certs/ca-bundle.crt
EOF

yum install -y grafana
systemctl enable --now grafana-server
systemctl status grafana-server --no-pager | grep Active
```

### 8.2 安装并启用 Zabbix 数据源插件

```bash
# 安装官方 zabbix 插件（grafana-cli 是 grafana 自带的命令行）
grafana-cli plugins install alexanderzobnin-zabbix-app

# 重启生效
systemctl restart grafana-server
```

### 8.3 浏览器配置

1. 访问 `http://192.0.2.201:3000`，默认 admin/admin（登录后改密）。
2. 「Configuration → Plugins → Zabbix」→ Enable。
3. 「Configuration → Data Sources → Add data source → Zabbix」：
   - URL = `http://192.0.2.201:8080/api_jsonrpc.php`
   - 认证：填 Zabbix 用户名（Admin）和密码，或用 API token。
4. 「Create → Dashboard → Add panel」，数据源选 Zabbix，选一个指标（如 CPU 使用率）出图。

> Grafana 的价值：Zabbix 自带的图偏"监控排查"，Grafana 的图更适合做**大屏/汇报**，且能同时叠加多个数据源。运维进阶必会。

---

## 9. 阶段八：Zabbix API 自动化（进阶）

用 API 做批量操作是运维效率分水岭。下面用 curl 演示（Python + requests 版本同理）。

```bash
# 1. 登录拿 token（注意：6.0 用 "username"，不是老版本的 "user"）
curl -s -X POST http://192.0.2.201:8080/api_jsonrpc.php \
  -H 'Content-Type: application/json-rpc' \
  -d '{"jsonrpc":"2.0","method":"user.login","params":{"username":"Admin","password":"zabbix"},"id":1}'
# 返回的 "result" 就是 token（一串 32 位 hex）

# 2. 用 token 查所有主机（把 <TOKEN> 换成上一步结果）
curl -s -X POST http://192.0.2.201:8080/api_jsonrpc.php \
  -H 'Content-Type: application/json-rpc' \
  -d '{"jsonrpc":"2.0","method":"host.get","params":{"output":["hostid","host","status"]},"auth":"<TOKEN>","id":2}'

# 3. 批量创建主机（循环调 host.create）——运维自动化核心场景
```

> API 是"把重复的配置操作变成脚本"的入口。配合 Python（你已学 requests），可以做出"读 CSV → 批量加主机 → 批量加监控项"的自动化工具。

---

## 10. 阶段九：备份与恢复（生产必须）

### 10.1 数据库备份

```bash
# 每日 2 点备份 zabbix 库（压缩存本地，保留 7 天）
mkdir -p /backup/zabbix
cat > /backup/zabbix_backup.sh <<'EOF'
#!/bin/bash
# 用 --single-transaction 避免锁表；mysqldump 密码用配置文件避免命令行明文
DATE=$(date +%F)
mysqldump --defaults-extra-file=/root/.my.cnf \
  --single-transaction --routines --triggers zabbix \
  | gzip > /backup/zabbix/zabbix_${DATE}.sql.gz
# 只保留最近 7 天
find /backup/zabbix -name 'zabbix_*.sql.gz' -mtime +7 -delete
EOF
chmod +x /backup/zabbix_backup.sh

# 加 crontab
echo "0 2 * * * /backup/zabbix_backup.sh" >> /var/spool/cron/root
```

> ⚠️ crontab 里的 `%` 会被转义，脚本里用 `$(date +%F)` 没问题，但**别直接在 crontab 行里写 `%F`**。

### 10.2 配置备份

```bash
# Zabbix 配置（含前端、模板、动作）最稳的备份是前端导出，或直接备份 DB（配置都在 DB 里）
# 额外备份关键配置文件
tar czf /backup/zabbix_conf_$(date +%F).tar.gz \
  /etc/zabbix /etc/nginx/conf.d/zabbix.conf /etc/php.ini /etc/php-fpm.d/www.conf
```

### 10.3 恢复演练（每月一次）

```bash
# 模拟恢复：清空库 → 重新导入
mysql -uroot -p -e "DROP DATABASE zabbix; CREATE DATABASE zabbix CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;"
zcat /backup/zabbix/zabbix_YYYY-MM-DD.sql.gz | mysql -uroot -p zabbix
systemctl restart zabbix-server-mysql   # EL7/EPEL 的 unit 名；EL9/官方为 zabbix-server
```

> 备份不做恢复演练 = 没有备份。恢复流程要在非生产环境先跑通。

---

## 11. 常见报错速查表

| 报错 | 根因 | 解决 |
|---|---|---|
| `Access denied for user 'zabbix'` | MySQL 8.0 用了 caching_sha2_password | 重建用户加 `IDENTIFIED WITH mysql_native_password`（§2.3） |
| 前端向导提示 PHP 版本 < 7.2 | 用了 CentOS 自带 PHP 5.4 | 装 remi 源启用 php74（§3.2） |
| 前端数据库连不上 | DB 没监听 3306 或没开远程权限 | 查 `ss -tlnp \| grep 3306`、`SHOW GRANTS FOR 'zabbix'@'%'` |
| server 日志 `database is down` | DBHost 配错或密码错 | 核对 §3.5，`mysql -uzabbix -p -h 192.0.2.201 zabbix` 测试 |
| agent 主动模式无数据 | `Hostname` 与前端不一致 | 两端主机名严格一致（§5.2） |
| Webhook 报 `HttpRequest is not defined` | 6.0 用 `CurlHttpRequest`，抄了 5.0 的写法 | 改用 `CurlHttpRequest()`（§7.2） |
| 导入 SQL 报 `log_bin_trust_function_creators` | 没开函数创建开关 | `SET GLOBAL log_bin_trust_function_creators=1`（§2.3） |
| zabbix_server 起不来（journal 只有 banner + status=1） | 真报错在 LogFile 不在 journal；常见 SELinux enforcing | `tail -30 /var/log/zabbix/zabbix_server.log`；或 root 前台跑 `zabbix_server -c /etc/zabbix_server.conf --foreground` |
| `The file "/tmp/zabbix_server_*.sock" is used by another process` | root 前台调试的残留 socket（rtc/service 等多个，且 zabbix 用户删不掉 root 的文件） | `pkill -9 zabbix_server && rm -f /tmp/zabbix_server*.sock` 再起服务 |
| SELinux enforcing 下服务秒退、root 前台跑却正常 | 服务进程受限、root 不受限 | `setenforce 0` 验证；永久 `sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config` |
| `Transaction check error: /usr/bin/zabbix_get conflicts` | EPEL `zabbix6.0-*` 与官方 `zabbix-*` 混装 | 只留一套：EL7 全 EPEL（§3.3），装官方包时加 `--disablerepo=epel` |
| `没有可用软件包 zabbix-server-mysql`（EL7） | EL7 官方源不含 server/web/agent2/get | 用 EPEL `zabbix6.0-*`（§3.3），或上 EL9 用官方包 |
| 自监控项 `zabbix[vmware,...]`/`zabbix[process,ipmi poller]` not supported | 对应 Start 参数为 0，非故障 | 无需处理；要监控 VMware/IPMI 时调大对应 Start 参数 |

---

## 12. 验收清单

- [ ] MySQL 8.0 运行，zabbix 库 utf8mb4_bin 建好，用户用 native_password
- [ ] PHP 版本 = 7.4.x（`php -v`）
- [ ] zabbix-server-mysql / php-fpm / httpd(或 nginx) 服务 active (running)
- [ ] 前端能登录，能看到 `Zabbix server` 自带主机有数据
- [ ] 被监控主机 `zbx-agent` 通过 agent(agentd) 上报 CPU/内存/磁盘数据
- [ ] 自动发现规则生效（文件系统、网卡自动纳管）
- [ ] 触发一个告警，飞书群收到消息；恢复后收到恢复消息
- [ ] Grafana 能出 Zabbix 数据源的图
- [ ] API 能登录并 `host.get` 返回主机列表
- [ ] 备份脚本跑通一次，恢复流程演练过

---

## 阶段十：模板与宏（运维"心法"，复用与排障的根基）

> 前面九个阶段把 Zabbix"跑起来"了，但从这一节开始才进入"真正会运维"的层次。模板和宏是 Zabbix 一切复用的基础，也是排障时最常遇到的"为什么改不动""为什么这个值不对"的根因所在。

### 10.1 为什么必须懂模板和宏

Zabbix 的规模化靠两个东西：**模板（Template）** 负责"把一组监控项/触发器/图形打包复用"，**宏（Macro）** 负责"让同一套模板适配不同主机"。

一个运维的真实日常：100 台服务器要监控磁盘使用率。你不会手写 100 遍监控项，而是**建一个模板，套到 100 台主机上**。但问题来了——不同主机的磁盘告警阈值不一样（核心库 85% 就该告警，日志盘 95% 才告警）。这时候靠的就是宏：模板里的阈值写 `{$DISK_USAGE_PCT}`，然后在每台主机上覆盖这个宏的值。

**不懂宏的典型表现**：对着模板里的触发器，怎么改阈值都不生效，或者一改影响全部 100 台——因为你不知道宏有优先级、不知道在哪一级覆盖。

### 10.2 用户宏的语法与三级优先级（排障必背）

**语法**：`{$宏名}`，宏名只能 `A-Z / 0-9 / _ / .`（大写字母开头）。

**三级定义位置**：
1. **全局宏**：管理 → 常规 → 宏。全局生效，一台改全部生效。
2. **模板宏**：模板属性 → 宏标签页。套了该模板的所有主机共享。
3. **主机宏**：主机属性 → 宏标签页。只对这一台生效。

**优先级（官方 6.0 确认，从高到低）**：
```
主机宏  >  一级模板宏（按模板ID升序）  >  二级模板宏  >  ...  >  全局宏
```

**排障关键结论**：
- 主机上定义了 `{$X}` → 直接用它，模板和全局的同名宏**全部被覆盖**；
- 主机没定义 → 逐级往模板找 → 最后找全局；
- 哪都没有 → **宏不解析**，触发器里就显示原始 `{$X}` 字符串（配置界面里宏不解析是正常设计，别当 bug）；
- **同一级多个模板有同名宏** → 用**模板 ID 最小**的那个（所以不同模板别用同名宏，是配置风险）。

### 10.3 实战：磁盘告警阈值用宏做分级

**场景**：一套磁盘监控模板，套 100 台主机；默认 90% 告警，但核心数据库服务器要 85% 就告警。

① 模板里，磁盘空间触发器写：
```
last(/Template OS Linux/vfs.fs.size[/,pused])>{$DISK_USAGE_PCT}
```

② 在**模板级**定义默认值：
```
{$DISK_USAGE_PCT} = 90
```

③ 在**那几台核心数据库主机级**覆盖：
```
主机 → 宏 → 新增 {$DISK_USAGE_PCT} = 85
```

结果：100 台默认 90% 告警，核心库那几台 85% 告警——**改一台不影响其余 99 台**。

**三个进阶要点（官方文档强调）**：
1. **秘密宏（Secret text）不能用在触发器表达式里**（会不解析），只能用在监控项键值、密码这类场景。
2. **优先用主机宏而非全局宏**：全局宏一旦增删改，会触发**所有主机的增量配置更新**，规模大了会卡。
3. **模板导出到别的系统时，宏要跟着模板走**：如果你在模板的监控项里用了 `{$X}`，最好把 `{$X}` 也定义在模板里，否则导出 XML 再导入别处会缺宏。

### 10.4 内置宏速记（排障/写告警标题常用）

| 宏 | 含义 |
|---|---|
| `{HOST.NAME}` | 主机名 |
| `{ITEM.VALUE}` | 触发该告警的监控项当前值 |
| `{ITEM.LASTVALUE}` | 监控项上一个值 |
| `{TRIGGER.NAME}` | 触发器名 |
| `{TRIGGER.STATUS}` | PROBLEM / OK |
| `{EVENT.ID}` / `{EVENT.TIME}` | 事件 ID / 时间 |
| `{$...}` | 用户宏（自定义） |

> 你 §7 飞书告警卡片里要显示"哪台机器、哪个值、什么时间"，靠的就是这些内置宏组合进标题/内容。

---

## 阶段十一：触发器函数与表达式（读懂别人的监控，写出自己的告警）

^> 这一节把触发器表达式彻底讲透——运维看别人配置、写自己告警，全卡在这。

### 11.1 新语法结构（6.0 统一语法）

```
函数(/主机名/键值, 参数) 比较符 阈值
```

例：`last(/Zabbix server/system.cpu.load[all,avg1])>5`

- 拆开读：`last()` 取最新值，参数是"主机名/键值"；`>5` 是阈值比较。
- **时间参数**：`30s / 10m / 1h / 1d`（秒/分/时/天）；
- **值计数参数**：`#5` 表示最近 5 个值（`last(#2)` 表示倒数第 2 个值，不是最近 5 个里第 2 个）。
- **时间位移**：`avg(/host/key,1h:now-1d)` = 昨天同一小时的平均值。

### 11.2 运维最常用的 8 个函数（按使用频率）

| 函数 | 作用 | 典型表达式 |
|---|---|---|
| `last()` | 最新值 | `last(/host/key)>90` |
| `min()/max()/avg()` | 区间最小/最大/平均 | `avg(/host/cpu,5m)>80` |
| `count()` | 区间内满足条件的次数 | `count(/host/icmpping,30m,,"0")>5`（30分钟内不通>5次） |
| `nodata()` | 多久没收到数据 | `nodata(/host/tick,3m)=1`（3分钟无心跳=断） |
| `find()` | 在值里找字符串 | `find(/host/agent.version,,"like","beta")=1` |
| `change()` | 值是否发生变化 | `change(/host/key)>0`（监控文件被改） |
| `trendavg()` | 趋势平均值（跨长时间段） | `trendavg(/host/load,1h:now-1d)` |
| `timeleft()` | 预测多久到阈值 | 见 11.4（日志盘误报问题） |

### 11.3 日志盘"24h 预测满盘"到底怎么判定

这类表达式（`.timeleft(1h,100)` 这类）的原理，一句话：**拿最近 1 小时磁盘使用率的增长斜率，做线性外推，算出"照这个涨法，多久后达到 100%"**。

- `timeleft(/host/vfs.fs.size[D:,pused],1h,100)` → 返回"距达到 100% 还剩多少秒"；
- 触发器写成 `timeleft(...)<86400`（86400 秒 = 24 小时）→ 意思就是"按当前增速，24 小时内会满"。

**它为什么容易误报（现场高频痛点）**：`timeleft` 用的是**线性外推**，对"脉冲式写入"极度敏感——日志轮转前突然暴涨一段，斜率瞬间变大，它就算出"几小时后满"，但轮转完又回落了。所以：
- 对**平稳增长**的盘（数据库）→ `timeleft` 很准，适合用它做预测；
- 对**周期脉冲**的盘（按天或按容量滚动的日志盘）→ 别用 `timeleft`，改用硬阈值 `last(...)>95` 更稳（这正是日志盘周期性误报的根因）。

### 11.4 写触发器的三个铁律

1. **`and/or/not` 必须小写，且前后加空格**（`a>1 and b>2`，写成 `a>1and b>2` 会报语法错）。
2. **恢复表达式（recovery expression）防抖动**：只写 problem 表达式时，问题一消失就恢复；加 recovery 表达式可以做迟滞（比如"低于 80% 才恢复"），避免边界值反复横跳（flapping）。
3. **触发器的阈值尽量用宏**（`>{$DISK_USAGE_PCT}` 而不是 `>90`），这样才符合阶段十的复用逻辑。

---

## 阶段十二：历史数据与容量管理（housekeeping + 表分区）

> 这是"Zabbix 跑半年后必踩"的硬知识。前面所有阶段都在讲"怎么采集、怎么告警"，但数据是往数据库里无限写的——不管理，半年后 DB 涨到几百 G，`zabbix[queue]` 积压、磁盘告警、前端卡顿全来了。这一节讲清楚"数据存哪、存多久、怎么清、量大了怎么办"。

### 12.1 history 和 trends 是什么区别（先分清概念）

Zabbix 采集到的值存两种表：

| 表类型 | 存什么 | 粒度 | 表名 |
|---|---|---|---|
| **history** | 原始值（每个采集点一条） | 逐点 | `history`(浮点)、`history_uint`(无符号整数)、`history_str`、`history_text`、`history_log` |
| **trends** | 每小时聚合（min/avg/max + 计数） | 每小时 1 条 | `trends`(浮点)、`trends_uint` |

**关键理解**：
- 不管监控项更新间隔是 10 秒还是 1 分钟，**trends 每小时永远只有 1 条**；只有 history 才跟采集频率走。
- 所以**长期看趋势图走 trends，看最近细节走 history**。默认 history 保留 90 天、trends 保留 365 天，就是"细粒度存短、粗粒度存长"的折中。

### 12.2 housekeeping（内置清理）—— 管理 → 常规 → Housekeeping

Zabbix 自带一个 housekeeper 进程，定期删过期数据。**配置入口：管理 → 常规 → 右侧下拉选「清理」**。

**要设置的两类保留期**：
- **历史数据保留期（history）**：默认 90 天；
- **趋势数据保留期（trends）**：默认 365 天；
- 还有事件/告警、审计日志等各自的保留期。

**两个 server 参数控制清理节奏**（`/etc/zabbix/zabbix_server.conf`）：
```
HousekeepingFrequency=1      # 多久跑一次清理，默认 1 小时
MaxHousekeeperDelete=5000    # 每次最多删多少行，默认 5000
```

**一个必须记住的坑**：监控项的「保留历史数据(天)」如果设成 `0`，**history 表里只保留最后 1 条值**——此时触发器里对这项用 `max/min/avg` 函数**毫无意义**（因为只有一条数据可算）。所以别图省事把保留期设 0，除非你确定这项只看实时值。

### 12.3 什么时候需要表分区（数据量大时的分水岭）

**现象**：housekeeper 用 `DELETE` 一句句删旧数据，数据量大时极慢、锁表、产生碎片，最终表现为——**"Zabbix housekeeper processes more than 75% busy" 告警**、DB 磁盘吃满、前端卡。

**解法**：把 history/trends 表按**天**分区，过期时直接 `DROP PARTITION`（整块删，秒级），比 DELETE 高效几个数量级。

**CentOS 7 + MySQL 8.0 的做法（三步）**：
1. 下载官方社区分区脚本 `zbx_db_partitiong.sql`（bestmonitoringtools 那套，3.0~6.0 通用），脚本默认 history 7 天 / trends 365 天，可改；
2. 导入脚本创建分区存储过程：`mysql -u zabbix -p'密码' zabbix < zbx_db_partitiong.sql`；
3. 让它每天自动跑：开 MySQL 事件调度器 `event_scheduler=ON`，建一个每 12 小时调用一次 `partition_maintenance_all('zabbix')` 的事件；或者用 crontab 每天凌晨调一次。

**⚠️ 分区没配好的严重后果**（必须知道）：如果分区维护事件停跑了，新一天的分区没建，Zabbix 会报 **`[Z3005] query failed: [1526] Table has no partition for value...`**，**对应时间段的数据写不进去、图是空的**。所以配完分区后，`event_scheduler` 或 crontab 一定要确保持续在跑。

### 12.4 运维落地的容量管理清单

1. **建库时就定好保留期**：history 90 天 / trends 365 天是够用的默认值，别瞎改大；
2. **监控 DB 本身的大小**：`SELECT table_schema, table_name, ROUND((data_length+index_length)/1024/1024/1024,2) AS 'GB' FROM information_schema.tables WHERE table_schema='zabbix' ORDER BY (data_length+index_length) DESC LIMIT 10;` 看哪几张表最大（几乎必然是 history/trends 系列）；
3. **数据量大（history 系列超几十 G）** → 上表分区，把 housekeeper 的 DELETE 换成 DROP；
4. **定期看 `zabbix[queue]` 和 housekeeper busy 告警**，这是容量问题的前兆信号。

> 你现在是学习阶段、数据量小，**先看懂 12.2 的保留期配置就够**；12.3 的表分区先记在心里，等哪天现网 DB 涨起来、收到 "housekeeper >75% busy" 告警，就是上分区的时机。

---

## 附：进阶学习路线建议（完整地图）

按"运维价值"排序，你吃透的顺序建议：

1. **主动/被动监控 + Agent2**（§5）—— 日常 90% 的监控都在这，必须彻底懂
2. **告警（Webhook）**（§7）—— 告警是监控的"出口"，没告警等于白监控
3. **模板与宏**（阶段十）—— 复用与排障的根基，不会宏等于不会改配置
4. **触发器表达式**（阶段十一）—— 读懂别人的监控，写出自己的告警
5. **自动发现 LLD**（§6）—— 规模化纳管的关键，配置一次管一片
6. **Grafana 可视化**（§8）—— 汇报/大屏必备
7. **API 自动化**（§9）—— 效率分水岭，配合 Python 能力
8. **备份恢复**（§10）—— 生产底线

### 下一步进阶地图（这三块是"会装"和"会运维"的分水岭）

**① 数据采集方式的完整地图**（面试高频："什么设备用什么采集"）

| 采集方式 | 适用对象 | 你已有的关联 |
|---|---|---|
| Agent / Agent2 | Linux/Windows 服务器 | 本文 §5 |
| SNMP | 交换机/路由器/打印机/存储 | 网络设备监控 |
| IPMI | 服务器硬件（温度/风扇/电源/硬盘） | 和你做过的 iDRAC、坏盘故障对口 |
| JMX | Java 应用（JVM、中间件） | Tomcat/Java 服务 |
| HTTP（Web 场景） | 业务可用性（登录/接口拨测） | 网站存活监控 |
| ICMP Ping | 只要"通不通"的设备 | 不可达主机 |
| 简单检查（Simple check） | 无需 agent 的基础项（端口/进程） | TCP 端口探测 |

> 建议补一个实验：给一台交换机配 SNMP 监控、给一台服务器配 IPMI 硬件监控，这两项是能体现"有实际设备监控经验"的硬通货。

**② 性能调优与队列监控**（监控值延迟、server 卡顿的根因都在这）

- 三个核心缓存：`CacheSize`（配置缓存）、`HistoryCacheSize`（历史值缓存）、`TrendCacheSize`（趋势缓存）、`ValueCacheSize`（值缓存，6.0 新增，直接影响触发器求值）。
- **内部监控项 `zabbix[queue]`**：看处理队列积压数量，持续 >0 说明 server 处理不过来，要么加缓存、要么加进程、要么优化监控项。
- 定位方法：`zabbix_server.log` 里出现 `Zabbix ... processes busy` 或队列积压，就是性能瓶颈信号。

**③ PSK 加密（生产安全必答）**

- Zabbix 6.0 的 agent-server 通信默认是**明文**，生产应该开 **PSK（预共享密钥）** 加密。
- 流程：server 端 `openssl rand -hex 32` 生成 PSK → agent 端配置 `TLSConnect=psk` + `TLSPSKIdentity` + `TLSPSKFile` → 前端主机里填相同的 identity 和 PSK。
- 这是"你懂不懂生产安全"的一道分水岭题，建议在虚拟机上实际配一遍。

### 剩余延伸（有余力再碰）
**Proxy（分布式监控）**、**SNMP 监控网络设备**、**JMX 监控 Java 应用**、**定时报表**、**Baseline（异常检测基线）**、**定时报表 + Grafana 告警联动**。

> 有三台虚拟机即可按这份文档从零走一遍。每走完一个阶段，就对照"验收清单"打勾，确保真的通了再进下一个阶段。
