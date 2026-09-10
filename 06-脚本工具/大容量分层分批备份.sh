#!/bin/bash
# ============================================================
# 大容量目录"分层 + 分批 + 断点续传"备份脚本（通用版）
#
# 【适用场景】
#   源机器磁盘有坏道 / 上行带宽有限 / 数据量大（几十 GB 起），
#   要求：尽快拿到"能恢复业务"的最小集合，全量慢慢传，中途断了不用重来。
#
# 【设计要点】
#   1. 分层：先 T0 秒级保住"配置 + 程序"（业务恢复靠它），再 T1 传全量数据
#   2. 分批：按一级子目录切成独立包，网络中断只需重传失败的那个包
#   3. 断点：已存在且非空的包自动跳过（重跑脚本 = 续传）
#   4. 源端压缩：压缩在源机器上做，本地只负责接收落盘，减少网络传输量
#   5. 不落盘：不在源机器上生成中间大文件（坏道盘要尽量少写）
#
# 【用法】
#   bash layered_backup.sh <目标目录> [t0|full|all]
#     t0   只备配置 + 程序（默认，最快）
#     full 全量分批
#     all  先 t0 再 full
#
# 【可调环境变量】
#   HOST=user@host      源机器（必填，或用 ssh 别名）
#   SRC_BASE=/path      源目录（必填）
#   T0_PATHS="a b c"    "分层"里优先抢救的路径（相对 SRC_BASE）
#   PARALLEL=1          并行度。**源盘有坏道时请保持 1**（顺序读更安全，也不打满坏盘 IO）
#   LEVEL=1             压缩级别 1~9，1 最快。上行是瓶颈时用 6
# ============================================================
set -uo pipefail

HOST="${HOST:-}"
SRC_BASE="${SRC_BASE:-}"
T0_PATHS="${T0_PATHS:-conf config bin scripts}"
DEST="${1:-}"
MODE="${2:-t0}"
PARALLEL="${PARALLEL:-1}"
LEVEL="${LEVEL:-1}"
DATE="$(date +%Y%m%d_%H%M)"

if [ -z "$HOST" ] || [ -z "$SRC_BASE" ] || [ -z "$DEST" ]; then
  echo "用法: HOST=user@host SRC_BASE=/path bash $0 <目标目录> [t0|full|all]" >&2
  exit 2
fi
command -v ssh >/dev/null 2>&1 || { echo "缺少 ssh 命令" >&2; exit 1; }

mkdir -p "$DEST"

# 源端压缩命令：有 pigz 用并行版，否则退回 gzip
COMP_CMD='(command -v pigz >/dev/null 2>&1 && pigz -'"$LEVEL"' || gzip -'"$LEVEL"')'

echo "=============================================="
echo " 分层分批备份   源=$HOST:$SRC_BASE"
echo "               目标=$DEST   模式=$MODE   并行=$PARALLEL 压缩=-$LEVEL"
echo "=============================================="

# ---------- 0. 先摸清体积构成，再决定备什么 ----------
analyze() {
  echo
  echo "===== [0] 空间构成分析（先看这个，别盲目拉全量） ====="
  ssh "$HOST" "du -sh $SRC_BASE/* 2>/dev/null | sort -rh | head -20"
}

# ---------- 1. T0：秒级抢救"配置 + 程序" ----------
backup_t0() {
  echo
  echo "===== [1] T0 分层：优先保住配置与程序 ====="
  local out="$DEST/t0_${DATE}.tar.gz"
  local args=""
  for p in $T0_PATHS; do
    args="$args ./$p"
  done
  # shellcheck disable=SC2029
  ssh "$HOST" "cd $SRC_BASE 2>/dev/null && tar $args -cf - 2>/dev/null | $COMP_CMD" > "$out" \
    || { echo "[ERROR] T0 备份失败"; rm -f "$out"; return 1; }
  if [ -s "$out" ]; then
    echo "[OK] T0 完成：$out（$(du -h "$out" | cut -f1)）"
  else
    echo "[WARN] T0 产出为空，检查 T0_PATHS 是否匹配实际目录"
    rm -f "$out"
  fi
}

# ---------- 2. 全量：按一级子目录切包 ----------
backup_full() {
  echo
  echo "===== [2] 全量分批 ====="
  local names
  names=$(ssh "$HOST" "cd $SRC_BASE 2>/dev/null && ls -1d */ 2>/dev/null | sed 's#/\$##'")
  if [ -z "$names" ]; then
    echo "[WARN] 未取到子目录清单，退化为整目录打包"
    names="__WHOLE__"
  fi

  local total=0 ok=0 skip=0 fail=0
  for n in $names; do
    total=$((total + 1))
    local safe pk out
    safe=$(printf '%s' "$n" | tr '/ ' '__')
    pk="$DEST/${safe}_${DATE}.tar.gz"
    out="$pk"

    # 断点续传：已存在且非空 → 跳过
    if [ -s "$out" ]; then
      echo "[SKIP] 已存在：$(basename "$out")"
      skip=$((skip + 1))
      continue
    fi

    echo "  -> 传输：$n"
    local tmp="${out}.part"
    if [ "$n" = "__WHOLE__" ]; then
      ssh "$HOST" "cd $SRC_BASE && tar ./ -cf - 2>/dev/null | $COMP_CMD" > "$tmp"
    else
      ssh "$HOST" "cd $SRC_BASE && tar ./$n -cf - 2>/dev/null | $COMP_CMD" > "$tmp"
    fi
    if [ $? -eq 0 ] && [ -s "$tmp" ]; then
      mv -f "$tmp" "$out"
      echo "     [OK] $(basename "$out")（$(du -h "$out" | cut -f1)）"
      ok=$((ok + 1))
    else
      echo "     [FAIL] $n —— 保留 .part 便于排查，重跑脚本会重试"
      fail=$((fail + 1))
    fi
  done

  echo
  echo "全量分批完成：总计 $total，成功 $ok，跳过 $skip，失败 $fail"
  [ "$fail" -eq 0 ] || echo "⚠️  有失败包，重跑本脚本即可续传（已存在的包会自动跳过）"
}

case "$MODE" in
  t0)   analyze; backup_t0 ;;
  full) backup_full ;;
  all)  analyze; backup_t0; backup_full ;;
  *)    echo "未知模式: $MODE（可选 t0|full|all）" >&2; exit 2 ;;
esac

echo
echo "===== 完成。目标目录内容 ====="
ls -lh "$DEST" 2>/dev/null | head -30
