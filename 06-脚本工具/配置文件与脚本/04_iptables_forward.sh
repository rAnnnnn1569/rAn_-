#!/bin/bash
# 20240130
# 主防平台数据转发
# 主链路：星冠->转发器（192.0.2.2:7835）->四川主防（192.0.2.6:9098）
# 从链路：四川主防->转发器（192.0.2.2:7836）->星冠（192.0.2.3:32002）
# 添加主链路DNAT和SNAT
# 数10秒，让docker先创建iptables相关规则,防止规则被顶替
sleep 10
# ===== 幂等封装：iptables -C 查重，已存在则提示跳过，最后统计新增条数 =====
# 用法: add NAT_PREROUTING|NAT_POSTROUTING|FWD <规则参数...>  参数与原 iptables 命令完全一致
#"$1"是判断添加的是哪个规则"NAT_PREROUTING"或者"NAT_POSTROUTING"或者"FWD"
#"$@"指代传入的所有参数,比如"-p tcp -s xxx --dport xxx等等后面的参数"，只要有一个参数不对，-C就会判断与原规则不同
ADDED=0
CHANGED=0
# ===== 变更检测: 链中已有"同链路但端口/IP已改"的旧规则时, 打备注保留不删除 =====
# 原理: 把规则里的端口数字模糊化再比对——IP地址相同、只有端口不同 => 判定为同一链路改版
check_change() {
  local t="$1" c="$2"; shift 2
  # norm: 只模糊端口(--dport 和目标端口), IP地址保留, 防止不同链路被误判
  local norm; norm() { echo "$1" | sed -E 's/--dport [0-9]+/--dport <P>/g; s/--to ([0-9.]+):[0-9]+/--to \1:<P>/g'; }
  local fp=$(norm "$*") num rule
  while read -r num rule; do
    echo "$rule" | grep -q -- '--comment "OLD-' && continue
    if [ "$(norm "$rule")" = "$fp" ]; then
      if iptables -t "$t" -R "$c" "$num" $rule -m comment --comment "OLD-$(date +%Y%m%d)" 2>/dev/null; then
        echo "[变更提示] $c 第${num}行 旧规则已备注保留(未删除): $rule"
        CHANGED=$((CHANGED+1))
      else
        echo "[警告] $c 第${num}行 旧规则备注失败,请人工核对: $rule"
      fi
    fi
  done < <(iptables -t "$t" -S "$c" | nl -v 1 | grep -v '^-P')
}
add() {
  local scope="$1"; shift
  case "$scope" in
    NAT_PREROUTING)
      if iptables -t nat -C PREROUTING "$@" 2>/dev/null; then
        echo "[跳过] PREROUTING 已存在 $*"
      else
        check_change nat PREROUTING "$@"
        iptables -t nat -I PREROUTING "$@"
        echo "[新增] PREROUTING  $*"
        ADDED=$((ADDED+1))
      fi ;;
    NAT_POSTROUTING)
      if iptables -t nat -C POSTROUTING "$@" 2>/dev/null; then
        echo "[跳过] POSTROUTING 已存在 $*"
      else
        check_change nat POSTROUTING "$@"
        iptables -t nat -A POSTROUTING "$@"
        echo "[新增] POSTROUTING $*"
        ADDED=$((ADDED+1))
      fi ;;
    FWD)
      if iptables -t filter -C FORWARD "$@" 2>/dev/null; then
        echo "[跳过] FORWARD 已存在 $*"
      else
        check_change filter FORWARD "$@"
        iptables -t filter -I FORWARD "$@"
        echo "[新增] FORWARD     $*"
        ADDED=$((ADDED+1))
      fi ;;
  esac
}
add NAT_PREROUTING -p tcp -s 192.0.2.3 --dport 7835 -j DNAT --to 192.0.2.6:9098
add NAT_POSTROUTING -p tcp -d 192.0.2.6 --dport 9098 -j MASQUERADE
# 添加从链路DNAT和SNAT
add NAT_PREROUTING -p tcp --dport 7836 -j DNAT --to 192.0.2.3:32002
add NAT_POSTROUTING -p tcp -d 192.0.2.3 --dport 32002 -j MASQUERADE
#开启星冠ip转发，防止转发走到docker的DOCKER-USER链中
add FWD -p tcp -d 192.0.2.3 -j ACCEPT
add FWD -p tcp -s 192.0.2.3 -j ACCEPT
# 20240130
# 四川联网联控数据转发
# 添加主链路DNAT和SNAT
add NAT_PREROUTING -p tcp -s 192.0.2.3 --dport 7855 -j DNAT --to 192.0.2.7:10100
add NAT_POSTROUTING -p tcp -d 192.0.2.7 --dport 10100 -j MASQUERADE
# 添加从链路DNAT和SNAT
add NAT_PREROUTING -p tcp --dport 7856 -j DNAT --to 192.0.2.3:32003
add NAT_POSTROUTING -p tcp -d 192.0.2.3 --dport 32003 -j MASQUERADE
# 20240203
# 广西联网联控数据转发
# 添加主链路DNAT和SNAT
add NAT_PREROUTING -p tcp -s  192.0.2.3 --dport 7865 -j DNAT --to 192.0.2.8:8055
add NAT_POSTROUTING -p tcp -d 192.0.2.8 --dport 8055 -j MASQUERADE
# 添加从链路DNAT和SNAT
add NAT_PREROUTING -p tcp -s 192.0.2.8 --dport 7866 -j DNAT --to 192.0.2.3:32021
add NAT_POSTROUTING -p tcp -d 192.0.2.3 --dport 32021 -j MASQUERADE
# 20250925
# 四川新主防数据转发正式
# 添加主链路DNAT和SNAT
add NAT_PREROUTING -p tcp -s  192.0.2.3 --dport 7841 -j DNAT --to 192.0.2.6:20181
add NAT_POSTROUTING -p tcp -d  192.0.2.6 --dport 20181 -j MASQUERADE
# 添加从链路DNAT和SNAT
add NAT_PREROUTING -p tcp --dport 7842 -j DNAT --to 192.0.2.3:32063
add NAT_POSTROUTING -p tcp -d 192.0.2.3 --dport 32063 -j MASQUERADE

# 广西联网联控(广西行业服务平台)
# 添加主链路DNAT和SNAT
add NAT_PREROUTING -p tcp -s 192.0.2.3 --dport 7838 -j DNAT --to 192.0.2.9:11019
add NAT_POSTROUTING -p tcp -d 192.0.2.9 --dport 11019 -j MASQUERADE
# 添加从链路DNAT和SNAT
add NAT_PREROUTING -p tcp --dport 7837 -j DNAT --to 192.0.2.3:32052
add NAT_POSTROUTING -p tcp -d 192.0.2.3 --dport 32052 -j MASQUERADE


# 20260506
# 广西南宁 link
# 添加主链路DNAT和SNAT
add NAT_PREROUTING -p tcp -s  192.0.2.3 --dport 7833 -j DNAT --to 192.0.2.1:21000
add NAT_POSTROUTING -p tcp -d  192.0.2.1 --dport 21000 -j MASQUERADE
# 添加从链路DNAT和SNAT
add NAT_PREROUTING -p tcp --dport 7834 -j DNAT --to 192.0.2.3:32037
add NAT_POSTROUTING -p tcp -d 192.0.2.3 --dport 32037 -j MASQUERADE
echo "---------------------------------"
echo "本次新增规则: ${ADDED} 条"
echo "检测到端口/IP变更并备注旧规则: ${CHANGED} 条"