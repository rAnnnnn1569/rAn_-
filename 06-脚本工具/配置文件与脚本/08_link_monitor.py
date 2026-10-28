#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
链路自动巡检：代理网关 + 多链路转发 → 飞书机器人
================================================
巡检对象（来源：交接文档《数据接入与809转发.docx》）：
  1. 代理网关   http://192.0.2.2:7822/api/monitor          （德阳27，众易通程序，JSON）
  2. 多链路转发 http://192.0.2.4:8000/api/monitor/info  （数据中心转出节点，文本行）

健康判据：
  网关：code==200；发送通道 connected 全 True；cacheCnt 不超阈值；
        accessCnt>0；下行 recvTime 距今不超过 RECV_STALE_MINUTES 分钟
  多链路：两次采样（间隔 SAMPLE_INTERVAL 秒）sendCnt 总和必须增长
        （对应文档"多刷新几次看变化"）；clientConnectedCnt>0；queueCnt 不超阈值

运行方式：跑一次就退，调度交给 Windows 任务计划程序（08:30 / 17:30 各一次）。
依赖：无（纯标准库，任何 Python 3.8+ 直接跑）。
日志：本目录 logs/link_monitor.log
"""

import json
import logging
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# ======================= 配置区（改这里） =======================
GATEWAY_URL = "http://192.0.2.2:7822/api/monitor"          # 代理网关检测链接
MULTI_URL = "http://192.0.2.4:8000/api/monitor/info"    # 多链路转发检测链接
FEISHU_WEBHOOK = ("https://open.feishu.cn/open-apis/bot/v2/hook/"
                  "00000000-0000-0000-0000-000000000000")     # 飞书机器人 webhook

TIMEOUT = 15                  # 单次 HTTP 超时（秒）
SAMPLE_INTERVAL = 5           # 多链路两次采样间隔（秒）
CACHE_ALERT_THRESHOLD = 500   # 网关发送通道缓存堆积告警阈值（实测正常 <100）
QUEUE_ALERT_THRESHOLD = 5000  # 多链路单线程队列堆积告警阈值（实测正常 <20）
RECV_STALE_MINUTES = 10       # 网关下行 recvTime 超过该分钟数视为停更
# ================================================================

LOG_DIR = Path(__file__).resolve().parent / "logs"


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_DIR / "link_monitor.log"),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        encoding="utf-8",
    )


def http_get(url):
    """GET 请求，返回 (status, text)。urllib 零依赖版 requests.get。"""
    req = urllib.request.Request(url, headers={"User-Agent": "ops-link-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


# ---------------- 1. 代理网关巡检 ----------------
def check_gateway():
    """返回 (ok: bool, lines: list[str])。lines 是给人看的摘要。"""
    problems, lines = [], []
    status, text = http_get(GATEWAY_URL)
    if status != 200:
        return False, [f"HTTP {status}，接口不可达"]
    payload = json.loads(text)
    if payload.get("code") != 200:
        return False, [f"接口返回异常 code={payload.get('code')} message={payload.get('message')}"]
    data = payload.get("data") or {}

    # 1a. 接入端口：设备在线数 + 剩余容量
    access = data.get("accessPortList") or []
    total_access = sum(p.get("accessCnt", 0) for p in access)
    total_enable = sum(p.get("enableCnt", 0) for p in access)
    lines.append(f"接入端口 {len(access)} 个：在线接入 {total_access} 台，剩余容量 {total_enable}")
    if total_access == 0:
        problems.append("代理网关接入数为 0（没有设备连上来）")

    # 1b. 发送通道（→ 数据中心 192.0.2.5）：必须 connected，cacheCnt 不许堆积
    sends = data.get("sendPortList") or []
    max_cache = max((s.get("cacheCnt", 0) for s in sends), default=0)
    lines.append(
        f"发送通道 {len(sends)} 条："
        f"{'全部已连接' if all(s.get('connected') for s in sends) else '存在断开'}，"
        f"缓存峰值 {max_cache}"
    )
    for s in sends:
        tag = f"{s.get('sendIp')}:{s.get('sendPort')}"
        if not s.get("connected"):
            problems.append(f"发送通道 {tag} 已断开（connected=false）")
        elif s.get("cacheCnt", 0) > CACHE_ALERT_THRESHOLD:
            problems.append(f"发送通道 {tag} 缓存堆积 {s.get('cacheCnt')}（阈值 {CACHE_ALERT_THRESHOLD}）")

    # 1c. 下行 recvTime 新鲜度：太久没更新 = 下行可能停了
    for d in data.get("downLinkInfoList") or []:
        recv_time = d.get("recvTime", "")
        try:
            age_min = (datetime.now() - datetime.strptime(recv_time, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
        except ValueError:
            problems.append(f"下行 recvTime 格式异常: {recv_time!r}")
            continue
        lines.append(f"下行链路最近收包：{recv_time}（{age_min:.1f} 分钟前）")
        if age_min > RECV_STALE_MINUTES:
            problems.append(f"下行数据 {age_min:.0f} 分钟未更新（recvTime={recv_time}）")

    return not problems, lines


# ---------------- 2. 多链路转发巡检 ----------------
LINE_RE = re.compile(
    r"thread_(\d+) clients:(\d+) queueCnt:(\d+) clientConnectedCnt:(\d+) sendCnt:(\d+)"
)


def fetch_multi_snapshot():
    """拉一次多链路状态，解析成线程列表；解析不出有效行返回空列表。"""
    status, text = http_get(MULTI_URL)
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    threads = []
    for m in LINE_RE.finditer(text):
        threads.append({
            "id": int(m.group(1)),
            "clients": int(m.group(2)),
            "queue": int(m.group(3)),
            "conn": int(m.group(4)),
            "send": int(m.group(5)),
        })
    return threads


def check_multi():
    """两次采样比对 sendCnt 增长 —— 自动化文档里的'多刷新几次看变化'。"""
    t1 = fetch_multi_snapshot()
    time.sleep(SAMPLE_INTERVAL)
    t2 = fetch_multi_snapshot()

    problems, lines = [], []
    if not t1 or not t2:
        return False, ["多链路接口无有效数据（响应为空或格式不符）"]

    send1 = sum(t["send"] for t in t1)
    send2 = sum(t["send"] for t in t2)
    conn_total = sum(t["conn"] for t in t2)
    q_max = max(t["queue"] for t in t2)
    lines.append(f"转发线程 {len(t2)} 个：客户端连接 {conn_total}，队列峰值 {q_max}")
    lines.append(f"{SAMPLE_INTERVAL} 秒内累计转发 {send2 - send1:,} 包（{send1:,} → {send2:,}）")

    if send2 <= send1:
        problems.append(f"sendCnt 未增长（{send1} → {send2}），转发可能停滞")
    if conn_total == 0:
        problems.append("多链路客户端连接数为 0")
    if q_max > QUEUE_ALERT_THRESHOLD:
        problems.append(f"队列堆积异常：单线程 queueCnt 峰值 {q_max}（阈值 {QUEUE_ALERT_THRESHOLD}）")

    return not problems, lines


# ---------------- 3. 飞书推送 ----------------
def send_feishu(is_alert, title_time, body_lines):
    """发飞书 interactive 卡片。异常=红色，正常=绿色。失败时重试 1 次。"""
    if is_alert:
        header = {"template": "red", "title": {"tag": "plain_text", "content": f"🚨 链路巡检异常 {title_time}"}}
    else:
        header = {"template": "green", "title": {"tag": "plain_text", "content": f"✅ 链路巡检正常 {title_time}"}}
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": header,
            "elements": [{
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(body_lines)},
            }],
        },
    }
    body = json.dumps(card).encode("utf-8")
    last_err = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                FEISHU_WEBHOOK, data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                result = json.loads(resp.read().decode("utf-8", "replace"))
            if result.get("code") == 0 or result.get("StatusCode") == 0:
                return True
            last_err = f"飞书返回异常: {result}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(1)
    logging.error("飞书推送失败（重试后）: %s", last_err)
    return False


# ---------------- 主流程 ----------------
def main():
    setup_logging()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    checks = (("代理网关(27)", check_gateway), ("多链路转发(150)", check_multi))

    all_lines, failed = [], []
    for name, fn in checks:
        try:
            ok, lines = fn()
            all_lines.append(f"【{name}】")
            all_lines.extend("　" + ln for ln in lines)
            if not ok:
                failed.append(name)
        except Exception as e:
            failed.append(name)
            all_lines.append(f"【{name}】巡检执行失败：{type(e).__name__}: {e}")
        all_lines.append("")

    is_alert = bool(failed)
    if is_alert:
        all_lines.insert(0, f"异常项：{'、'.join(failed)}")
    all_lines.append(f"巡检时间：{now}")

    sent = send_feishu(is_alert, now[5:], all_lines)
    logging.info("巡检完成 alert=%s feishu_sent=%s failed=%s", is_alert, sent, failed or "无")
    print("alert =", is_alert, "| feishu_sent =", sent)
    sys.exit(1 if not sent else 0)   # 推送失败时给任务计划留非零退出码


if __name__ == "__main__":
    main()
