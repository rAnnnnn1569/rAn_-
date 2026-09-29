# 源码部署的 Nginx "没起来" —— 从访问被拒到服务化

> 场景：一台用**源码 / 二进制包**方式部署 Nginx 的服务器，对外端口访问被拒。
> 本文记录一次真实排查：两次直觉猜测都是错的，真正的定位动作只有一个 —— **先看端口有没有人监听**。

---

## 一、现象与两次跑偏的直觉

访问 `备案域名:8800` 直接被拒。第一反应有两个，**两个都错了**：

1. **猜测一：反代到后端的证书过期** → 查过，早已续签，排除。
2. **猜测二：防火墙没放行 8800** → 正准备去开端口。

**关键动作**：在动防火墙**之前**，先看 8800 到底有没有人在听：

```bash
ss -tulnp | grep 8800
```

→ **没有任何输出**。

⇒ 端口**根本没被监听**。问题不在防火墙：
- 防火墙拦的是"包进不来"（能连上但被拒）；
- 监听都没起，是"**没人接**"（连不上）。

**这一步做错方向，后面全是白工。**

---

## 二、真正的原因：服务装了，但没有服务单元

接着查服务：

```bash
systemctl status nginx
# Unit nginx.service could not be found.
```

看到这句，很容易得出**错误结论**："这台机器没装 Nginx"。

**实际上是：装了，只是没有注册成 systemd 服务。**

**判断依据：安装路径本身就在提示部署方式。**

| 部署方式 | 二进制位置 | 配置文件 | 自带 systemd unit？ |
|---|---|---|---|
| `yum` / `rpm` 安装 | `/usr/sbin/nginx` | `/etc/nginx/nginx.conf` | **有** |
| **源码 / 二进制包** | 自定义前缀，如 `/opt/nginx/sbin/nginx` | 如 `/opt/nginx/conf/*.conf` | **没有** |

本例的配置就在 `/opt/nginx/conf/port_8800.conf` —— 这个路径**已经明示**是源码部署，不能用"`systemctl` 找不到 = 没装"来下结论。

---

## 三、先恢复业务，再规范化

### 第 1 步：用绝对路径直接起（应急）

```bash
/opt/nginx/sbin/nginx -t     # 先做配置语法自检，语法错会在这里报出来
/opt/nginx/sbin/nginx        # 启动
ss -tulnp | grep 8800        # 复验：监听应已出现
```

⚠️ **顺序不能反**：`-t` 通过再启动。直接起一个配置有错的实例，会出现"进程在、端口不通"的假象。

### 第 2 步：补一个 systemd 单元（本次的实际产出）

```ini
# /etc/systemd/system/nginx.service
[Unit]
Description=nginx (source build)
After=network.target

[Service]
Type=forking
PIDFile=/opt/nginx/logs/nginx.pid
ExecStartPre=/opt/nginx/sbin/nginx -t -c /opt/nginx/conf/nginx.conf
ExecStart=/opt/nginx/sbin/nginx -c /opt/nginx/conf/nginx.conf
ExecReload=/opt/nginx/sbin/nginx -s reload
ExecStop=/opt/nginx/sbin/nginx -s quit
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload     # 新增 / 改动 unit 后必须执行，否则 systemd 不认
systemctl start nginx
systemctl enable nginx      # 开机自启 —— 不设的话下次重启又会"消失"
systemctl status nginx
```

> **两个注意点（源码部署的高发坑）**
> 1. `PIDFile` 与 `ExecStart` 里的路径必须与**实际安装前缀**一致。`nginx.conf` 里通常还有一个 `pid` 指令，**两处不一致时 systemd 找不到主进程**，`status` 会显示 failed 但进程其实起来了。
> 2. 单元语法上线前建议自检：`systemd-analyze verify /etc/systemd/system/nginx.service`。
> （上例为按源码部署惯例整理的**模板**，路径需按实际安装前缀替换。）

完成后再次访问端口 → 转发正常。

---

## 四、顺带纠正一个端口可达性的误区

曾经遇到一个"反直觉"现象：**这台机器的 `iptables` 里没有任何针对 8800 的放行规则，外网却照样能访问 8800。**

原因不是"漏配"，而是：

- `iptables` 的 **INPUT 链默认策略是 `ACCEPT`** —— 没匹配到 `DROP` 的就放行；
- Nginx 的 `listen 8800` 等价于 `0.0.0.0:8800`，**监听在所有地址**上，不是只监听回环。

⇒ **结论有两面：**

1. **"能访问" ≠ "防火墙配对了"** —— 不要把"当前能通"当作规则正确的证据；
2. **想封端口，只靠"不加放行规则"是没用的**，必须**显式 `DROP`/`REJECT`**，或把默认策略改为 `DROP`。

---

## 五、沉淀

1. **排查网络可达性，固定按这个次序**：
   ```
   进程 → 端口监听 → 本机防火墙 → 云安全组 → 运营商 / 路由
   ```
   顺序的意义在于：**前面一层没确认，后面全是在猜**。本例若不先 `ss` 就去开防火墙端口，会白折腾一轮，而且开了也没用。
2. **看到"服务不存在"先分辨是"没装"还是"没注册"** —— 看配置/二进制路径就能判断部署方式，别被 `systemctl` 的报错带偏。
3. **源码部署的服务，交付时顺手补一个 systemd unit** —— 否则一次重启就失联。

---

## 附：常用定位命令

| 目的 | 命令 |
|---|---|
| 谁在监听端口 | `ss -tulnp \| grep <port>` |
| 进程在不在 | `ps -ef \| grep <进程名>` |
| 服务是否注册到 systemd | `systemctl status <服务名>` |
| 本机防火墙规则 | `iptables -S` / `iptables -t nat -S` |
| Nginx 配置语法自检 | `/opt/nginx/sbin/nginx -t` |
| 单元文件语法自检 | `systemd-analyze verify /etc/systemd/system/<name>.service` |
