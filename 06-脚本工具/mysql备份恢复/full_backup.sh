#!/bin/bash
# ============================================================
# MySQL 全量备份（生产级）
#   用法: ./full_backup.sh [full|week]
#         full = 日备（默认，保留 7 天）
#         week = 周备（保留 21 天）
#   特性: 一致性快照 / 记录 binlog 位点 / 完整性校验 / sha256 /
#         并发锁 / 空间护栏 / 失败清理 / 过期清理 / 全程日志
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_DIR="${CONF_DIR:-$SCRIPT_DIR/conf}"
MODE="${1:-full}"

CONF_FILE="$CONF_DIR/backup.env"
CLIENT_CNF="${CLIENT_CNF:-$CONF_DIR/client.cnf}"

if [ ! -r "$CONF_FILE" ]; then
  echo "[FATAL] 找不到配置文件: $CONF_FILE" >&2
  exit 1
fi
# shellcheck disable=SC1090
. "$CONF_FILE"

case "$MODE" in
  full) TARGET_DIR="$FULL_DIR"; KEEP_DAYS="$KEEP_DAYS_FULL"; TAG="date" ;;
  week) TARGET_DIR="$WEEK_DIR"; KEEP_DAYS="$KEEP_DAYS_WEEK"; TAG="week" ;;
  *)    echo "用法: $0 [full|week]" >&2; exit 2 ;;
esac

mkdir -p "$TARGET_DIR" "$LOG_DIR"
LOG_FILE="$LOG_DIR/full_backup_${TAG}_$(date +%Y%m).log"

log()  { echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"; }
die()  { log "[ERROR] $*"; exit 1; }

# ---------- 凭据文件 ----------
[ -r "$CLIENT_CNF" ] || die "找不到凭据文件: $CLIENT_CNF"
PERM=$(stat -c %a "$CLIENT_CNF" 2>/dev/null || echo unknown)
[ "$PERM" = "600" ] || log "[WARN] $CLIENT_CNF 权限为 $PERM，建议 chmod 600"

# ---------- 并发锁：防止 cron 重复触发 / 上次未跑完 ----------
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE" || die "无法创建锁文件 $LOCK_FILE"
  if ! flock -n 9; then
    log "已有备份任务在运行，本次直接退出"
    exit 0
  fi
else
  log "[WARN] 系统无 flock 命令，跳过并发锁"
fi

# ---------- 空间护栏 ----------
AVAIL=$(df -Pm "$TARGET_DIR" 2>/dev/null | awk 'NR==2 {print $4}')
if [ -n "${AVAIL:-}" ]; then
  if [ "$AVAIL" -lt "$MIN_FREE_MB" ]; then
    die "备份目录剩余空间不足：${AVAIL}MB < ${MIN_FREE_MB}MB"
  fi
  log "备份目录剩余空间：${AVAIL}MB"
else
  log "[WARN] 无法获取剩余空间，跳过空间检查"
fi

# ---------- 组装参数 ----------
DB_TAG=$(echo "$DB_NAMES" | tr ' ' '_')
TS=$(date +%Y%m%d%H%M%S)
SQL="$TARGET_DIR/${DB_TAG}_${TAG}_${TS}.sql"
GZ="${SQL}.gz"

IGNORE_ARGS=()
for t in $IGNORE_TABLES; do
  IGNORE_ARGS+=(--ignore-table="$t")
done

NICE=""
if [ "${NICE_LEVEL:-0}" -gt 0 ] && command -v nice >/dev/null 2>&1; then
  NICE="nice -n $NICE_LEVEL"
fi

log "===== 开始 ${TAG} 全量备份 | 库: $DB_NAMES | 目标: $GZ ====="
START_TS=$(date +%s)

# ---------- 执行备份 ----------
# 先落 SQL 明文再压缩：便于从中提取 binlog 位点，也便于判断 dump 是否真正成功
set +e
# shellcheck disable=SC2086
$NICE "$MYSQLDUMP_BIN" --defaults-extra-file="$CLIENT_CNF" \
  $DUMP_EXTRA_OPTS \
  --default-character-set=utf8mb4 \
  --databases $DB_NAMES \
  "${IGNORE_ARGS[@]}" > "$SQL" 2>>"$LOG_FILE"
RC=$?
set -e

if [ "$RC" -ne 0 ] || [ ! -s "$SQL" ]; then
  rm -f "$SQL"
  die "mysqldump 执行失败（退出码 $RC），已删除不完整文件"
fi

# ---------- 完整性校验：结尾标记 ----------
TAIL_LINE=$(tail -c 300 "$SQL" | tr -d '\r' | tail -1)
case "$TAIL_LINE" in
  *"Dump completed"*) : ;;
  *) rm -f "$SQL"; die "备份结尾异常，疑似中断（末行: $TAIL_LINE）" ;;
esac

# ---------- 提取 binlog 位点（兼容 5.7 MASTER_* 与 8.0 SOURCE_*） ----------
POS_LINE=$(grep -m1 -E "CHANGE MASTER TO|CHANGE REPLICATION SOURCE TO" "$SQL" || true)
BINLOG_FILE=""
BINLOG_POS=""
if [ -n "$POS_LINE" ]; then
  BINLOG_FILE=$(printf '%s' "$POS_LINE" | sed -E "s/.*(MASTER|SOURCE)_LOG_FILE='?([^',]+)'?.*/\2/")
  BINLOG_POS=$(printf '%s' "$POS_LINE"  | sed -E "s/.*(MASTER|SOURCE)_LOG_POS=([0-9]+).*/\2/")
fi

# ---------- 压缩 ----------
"$GZIP_BIN" -"$COMPRESS_LEVEL" "$SQL"
[ -s "$GZ" ] || die "压缩失败：$GZ"

# ---------- sha256 校验和 ----------
if command -v "$SHA256_BIN" >/dev/null 2>&1; then
  ( cd "$TARGET_DIR" && "$SHA256_BIN" "$(basename "$GZ")" > "$(basename "$GZ").sha256" )
fi

# ---------- 被排除表的结构补偿 ----------
# 注意：mysqldump --ignore-table 会把该表的结构和数据一起跳过，
#       直接恢复会导致这些表"消失"。此处单独导出其结构（不带数据），
#       恢复脚本会把它补回来。--skip-add-drop-table 保证不会误删已有表。
SCHEMA_GZ=""
if [ -n "${IGNORE_TABLES:-}" ]; then
  SCHEMA_SQL="$TARGET_DIR/${DB_TAG}_${TAG}_${TS}_ignored_schema.sql"
  : > "$SCHEMA_SQL"
  for t in $IGNORE_TABLES; do
    db="${t%%.*}"
    tb="${t##*.}"
    {
      echo "--"
      echo "-- 表结构（数据已按 --ignore-table 排除）：$t"
      echo "--"
    } >> "$SCHEMA_SQL"
    if ! "$MYSQLDUMP_BIN" --defaults-extra-file="$CLIENT_CNF" \
         --no-data --skip-add-drop-table --skip-comments \
         --default-character-set=utf8mb4 "$db" "$tb" >> "$SCHEMA_SQL" 2>>"$LOG_FILE"; then
      log "[WARN] 导出被排除表的结构失败：$t"
    fi
  done
  if [ -s "$SCHEMA_SQL" ]; then
    "$GZIP_BIN" -"$COMPRESS_LEVEL" "$SCHEMA_SQL"
    SCHEMA_GZ="${SCHEMA_SQL}.gz"
    if command -v "$SHA256_BIN" >/dev/null 2>&1; then
      ( cd "$TARGET_DIR" && "$SHA256_BIN" "$(basename "$SCHEMA_GZ")" > "$(basename "$SCHEMA_GZ").sha256" )
    fi
    log "已生成被排除表结构文件：$(basename "$SCHEMA_GZ")"
  else
    rm -f "$SCHEMA_SQL"
  fi
fi

# ---------- 位点元数据（一键恢复脚本据此衔接增量） ----------
cat > "${GZ}.pos" <<EOF
backup_file=$(basename "$GZ")
schema_file=$([ -n "$SCHEMA_GZ" ] && basename "$SCHEMA_GZ" || echo "")
backup_type=${TAG}
db_names=${DB_NAMES}
binlog_file=${BINLOG_FILE}
binlog_pos=${BINLOG_POS}
created_at=$(date '+%F %T')
host=$(hostname)
EOF

SIZE=$(du -h "$GZ" | cut -f1)
ELAPSED=$(( $(date +%s) - START_TS ))
log "备份完成：$GZ（$SIZE，用时 ${ELAPSED}s）"
log "记录位点：${BINLOG_FILE:-<未获取>} : ${BINLOG_POS:-<未获取>}"

# ---------- 清理过期备份 ----------
DEL_N=$(find "$TARGET_DIR" -maxdepth 1 -type f -mtime +"$KEEP_DAYS" -print -delete 2>/dev/null | wc -l)
log "清理过期备份 $DEL_N 个（保留 ${KEEP_DAYS} 天）"
log "===== 结束 ====="
exit 0
