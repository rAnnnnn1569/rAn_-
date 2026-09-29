# 接口"传参契约"变更导致批量任务全失败 —— `json=` 改 `data=` 修复实录

> 一次真实故障：**服务能启动、重启也不报错、日志不喊救命**，但批量任务 **2700 多条一条都没成功**。
> 根因不在本机，而在**对端平台升级后收紧了接口传参格式**。
> 本文是代码级实录（已脱敏）；方法论层面的归纳见同目录 [`服务重启无效类故障排查方法论.md`](服务重启无效类故障排查方法论.md)。

---

## 一、现象

某"车辆续费"批量任务，页面汇总：

> **共 2712 辆，成功 0 辆，失败 2712 辆** —— 页面上的"**已处理数**"始终为 **0**。

---

## 二、排查过程（五步定位）

### ① 重启 —— 无效

程序是某台服务器上部署的 Python 应用。按惯例先重启：

```
kill 掉 python 进程 → 跑启动脚本拉起 → 再次执行续费
```

结果：已处理数**依旧为 0**。

⇒ **不是进程状态问题**，要往**代码 / 接口层**走。

（顺带发现：对端平台的**接口地址已变更**，先在源码里改了地址。https 因缺证书不可用，最终走 http。）

### ② SQL 按状态字段分组 —— 确认"全挂"还是"部分挂"

程序逻辑：未处理车辆 `flag=0`，处理成功置 `flag=1`；页面"已处理数"统计的就是 `flag=1` 的数量。

```sql
SELECT flag, COUNT(*) FROM <续费记录表>
WHERE create_time >= '<起始日期>' GROUP BY flag;
```

结果：**2712 辆全部 `flag=0`**。

⇒ 说明续费函数**一辆都没处理成功**（若部分成功会看到 `flag=1` 的行）。**故障范围锁定 = 全失败**。

> 这一步的价值：**用一条 SQL 把"故障范围"钉死**，避免后面在"是不是某几辆车数据有问题"上绕弯路。

### ③ 认准"业务模块自己"的日志

门户 / 调度器的日志只写"**任务调用完成**" —— **它不报错**。

真正的错误在被调用模块**自己的日志**里（本例：`recharge.log`，每次只刷一条）：

```
ERROR: 车辆数据获取失败，可尝试重新填写数据中心token后重新运行此脚本！！
```

⇒ 日志把嫌疑指向了**"取车辆数据"这一步**。

### ④ 代码定位 —— 找到"一票否决"的那一行

续费脚本里取车辆数据的函数：

```python
carinfos = bd.getCarInfoByBrand(...)   # 调对端"查车"接口
if carinfos == False:                  # 查车失败
    logger.error("车辆数据获取失败...")
    return                             # ← 直接退出整个续费流程，后面 2712 辆全被跳过
```

⚠️ **一个前置失败就把整批任务短路了** —— 没有继续、没有重试、没有逐辆降级。

### ⑤ 用 `curl` 手动复现 —— 把"地址不通"和"传参不对"分开

| 测试写法 | 结果 |
|---|---|
| 旧登录格式（URL 查询串里拼**未编码的 JSON**，`GET`） | **HTTP 400** |
| 按接口文档规范登录（**POST + 表单 `reqParam`**） | `Result:1`，正常拿到令牌 `_sid` |
| 按规范查车（**POST + 表单 `reqParam` + `_sid` 作为独立参数**） | `Result:1`，查到车辆 |

⇒ **地址是通的、账号是对的。问题出在"怎么把参数发出去"。**

---

## 三、根因

**对端平台升级迁移后，接口规范变严格**：只认 **POST + 表单传参**，且**业务参数与令牌必须是两个独立的表单字段**。

而旧代码用的是**两种都不合规**的写法：

1. 把整个 body **当 JSON 发**（`json=`）；
2. 甚至把 JSON **拼在 URL 查询串里**发。

⇒ 新服务解析不了 → 返回 **400 / 失败** → 查车失败 → 续费流程中止 → **2712 辆全被跳过**。

---

## 四、修复：共 6 处，统一口径

统一改为：**POST + 表单体**（`reqParam=<JSON>` 与 `_sid=<令牌>` 为两个独立表单字段）。

| # | 位置 | 改动 | 必要性 |
|---|---|---|---|
| 1 | 登录地址常量 | 把拼在 URL 里的 JSON 拿掉，账号 / 密码抽成常量 | 必要（旧网址 400） |
| 2 | `token_getter()` 登录 | 参数从 **URL 查询串** → **POST 表单** | 必要 |
| 3 | `getCarInfoByBrand()` 查车 | `json=data` → `data=data`，去掉 JSON 头 | **核心修复** |
| 4 | `updateCarLife()` 改服务日期 | `json=data` → `data=data` | 必要（同写法必挂） |
| 5 | `moveGroup()` 移组 | 同上 | 必要 |
| 6 | `updateCarSim()` 改 SIM 卡 | 同上 | 必要 |

> **核心改动一句话：`json=`（发 JSON body）→ `data=`（发表单），参数内容一个都没动。**

---

## 五、改动前后对比（脱敏示例）

### 改动 1：登录地址常量

改前把参数**写死在网址里**（网址里带空格、双引号、花括号），新系统解析网址直接 400：

```python
# 改前
base_url  = 'http://<对端平台>:8088/<接口根路径>/'
login_url = base_url + 'User_Login?reqParam={"UserId": "<账号>", "Pwd": "<密码MD5>"}'
```

改后网址只留接口名，参数放表单里发：

```python
# 改后
base_url  = 'http://<对端平台>:8088/<接口根路径>/'

# 登录账号（接口标准要求：Pwd 为分配的密码经 MD5 加密，32 位大写）
USER_ID   = '<账号>'
USER_PWD  = '<密码MD5>'

login_url = base_url + 'User_Login'    # 参数由下面的 token_getter() 通过表单填
```

### 改动 2：登录函数 `token_getter()`

改前什么都不带（参数全在网址里）：

```python
# 改前
def token_getter():
    token = req.post(login_url, cookies=req.cookies)
    return token.json()
```

改后把账号密码放进表单体：

```python
# 改后
def token_getter():
    data = {"reqParam": json.dumps({"UserId": USER_ID, "Pwd": USER_PWD})}
    token = req.post(login_url, data=data, cookies=req.cookies)
    return token.json()
```

### 改动 3：查车函数 `getCarInfoByBrand()`（本次卡住的直接元凶）

**只改了一行**：`json=data` → `data=data`，并去掉 JSON 头。`data` 字典里的内容**一字未改**。

```python
# 改前
def getCarInfoByBrand(brand, color_code):
    param  = {"OrgId": 1, "PlateNum": brand, "ColorCode": color_code}
    data   = {"reqParam": json.dumps(param), "_sid": token}
    result = req.post(get_car, json=data, cookies=req.cookies, headers=json_headers)

# 改后
def getCarInfoByBrand(brand, color_code):
    param  = {"OrgId": 1, "PlateNum": brand, "ColorCode": color_code}
    data   = {"reqParam": json.dumps(param), "_sid": token}
    result = req.post(get_car, data=data, cookies=req.cookies)
```

### 改动 4 / 5 / 6：其余三处同源调用

写法完全一样，**一起改**（见上表）。参数内容不变，仅发送方式变化：

```python
# 改前
result = req.post(<接口>, json=data, cookies=req.cookies, headers=json_headers)
# 改后
result = req.post(<接口>, data=data, cookies=req.cookies)
```

---

## 六、验证

⚠️ **必须重启进程**：程序在 `import` 阶段就登录换取了 `_sid`，**不重启拿不到新令牌**。

重启后再次执行续费：**成功**，页面"已处理数"正常；日志逐辆打印成功记录（形如 `车辆已续费完成 <车牌> <到期日>`）。

---

## 七、沉淀（通用规律）

1. **"重启无效 + 日志无异常 + 业务成果为零" → 查接口传参格式**，尤其是**对端刚升级过**的接口。
2. **对端升级后，把同一份代码里所有调用点一起排查**，不要只修被报出来的那一处 —— 本例共 6 处同源调用，**只修 1 处等于没修**。
3. **HTTP 400 往往意味着请求里有非法字符**（本例是 URL 里带了空格、双引号、花括号），而**不是"地址不通"**。
4. **看日志要认对模块**：门户 / 调度日志写"调用完成"，会**掩盖真实失败**；一定要看**执行业务逻辑那个模块自己的日志**。
5. **前置依赖失败不要 `return` 掉整批任务**：应逐条降级并记录失败原因，否则一个接口报错就"全灭"，排查时连失败样本都看不到。
6. **遗留隐患**：同一份源码里仍有函数在用"**URL 查询串拼参数**"的旧写法（不在本次主链路上，当时未改），后续相关功能异常时**优先检查它**。
