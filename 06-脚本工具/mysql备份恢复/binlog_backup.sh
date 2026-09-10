#!/bin/bash
# ============================================================
# MySQL binlog 增量归档（生产级）
#   用法: ./binlog_backup.sh
#   要点: 先 FLUSH BINARY LOGS 把当前日志"封口"，再只拷贝已关闭的
#         binlog，绝不用 mysqlbinlog --stop-never（那是 tail -f 语义，
#         放在 cron 里会永久挂住、每次触发堆一个进程）。
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_DIR="${CONF_DIR:-$SCRIPT_DIR/conf}"
CONF_FILE="$CONF_DIR/backup.env"
CLIENT_CNF="${CLIENT_CNF:-$CONF_DIR/client.cnf}"

[ -r "$CONF_FILE" ] || { echo "[FATAL] 找不到配置: $CONF_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
. "$CONF_FILE"

mkdir -p "$BINLOG_DIR" "$LOG_DIR"
LOG_FILE="$LOG_DIR/binlog_backup_$(date +%Y%m).log"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"; }
die() { log "[ERROR] $*"; exit 1; }

[ -r "$CLIENT_CNF" ] || die "找不到凭据文件: $CLIENT_CNF"

MYSQL_ARGS=(--defaults-extra-file="$CLIENT_CNF")

if command -v flock >/dev/null 2>&1; then
  exec 9>"${LOCK_FILE}.binlog" || die "无法创建锁文件"
  flock -n 9 || { log "已有归档任务在运行，本次退出"; exit 0; }
fi

log "===== 开始 binlog 增量归档 ====="

# ---------- 1. 封口当前 binlog ----------
"$MYSQL_BIN" "${MYSQL_ARGS[@]}" -e "FLUSH BINARY LOGS;" 2>>"$LOG_FILE" \
  || die "FLUSH BINARY LOGS 失败（账号需 RELOAD 权限）"

# ---------- 2. 定位 binlog 所在目录 ----------
BASENAME=$("$MYSQL_BIN" "${MYSQL_ARGS[@]}" -N -B -e "SELECT @@log_bin_basename;" 2>/dev/null || echo "NULL")
if [ -z "$BASENAME" ] || [ "$BASENAME" = "NULL" ]; then
  die "服务器未开启 binlog（log_bin=OFF），无法做增量归档"
fi
SRV_DIR=$(dirname "$BASENAME")
log "服务器 binlog 目录：$SRV_DIR"

# ---------- 3. 列出全部 binlog，排除最后一个（正在写入的） ----------
mapfile -t LOGS < <("$MYSQL_BIN" "${MYSQL_ARGS[@]}" -N -B -e "SHOW BINARY LOGS;" | awk '{print $1}')
TOTAL=${#LOGS[@]}
log "服务器共有 binlog $TOTAL 个（最后一个正在写入，跳过）"

if [ "$TOTAL" -lt 2 ]; then
  log "暂无已关闭的 binlog 可归档"
else
  COPIED=0
  SKIPPED=0
  for (( i = 0; i < TOTAL - 1; i++ )); do
    NAME="${LOGS[$i]}"
    SRC="$SRV_DIR/$NAME"
    DST="$BINLOG_DIR/$NAME"
    if [ ! -f "$SRC" ]; then
      log "[WARN] 源文件不存在，跳过：$SRC"
      continue
    fi
    # 已归档且大小一致 => 跳过（断点续传语义）
    if [ -f "$DST" ] && [ "$(stat -c %s "$DST")" = "$(stat -c %s "$SRC")" ]; then
      SKIPPED=$((SKIPPED + 1))
      continue
    fi
    if cp -p "$SRC" "$DST.tmp"; then
      mv -f "$DST.tmp" "$DST"
      if command -v "$SHA256_BIN" >/dev/null 2>&1; then
        ( cd "$BINLOG_DIR" && "$SHA256_BIN" "$NAME" > "$NAME.sha256" )
      fi
      COPIED=$((COPIED + 1))
      log "已归档：$NAME（$(du -h "$DST" | cut -f1)）"
    else
      rm -f "$DST.tmp"
      log "[ERROR] 归档失败：$NAME"
    fi
  done
  log "本次归档 $COPIED 个，跳过（已存在）$SKIPPED 个"
fi

# ---------- 4. 记录归档清单 ----------
{
  echo "# binlog 归档清单  更新于 $(date '+%F %T')"
  ls -1 "$BINLOG_DIR" 2>/dev/null | grep -E '^mysql-bin\.[0-9]+$' | sort | while read -r f; do
    printf '%s  %s\n' "$f" "$(stat -c %s "$BINLOG_DIR/$f")"
  done
} > "$BINLOG_DIR/ARCHIVE_INDEX.txt"

# ---------- 5. 过期清理 ----------
DEL_N=$(find "$BINLOG_DIR" -maxdepth 1 -type f -name 'mysql-bin.*' \
        -mtime +"$KEEP_DAYS_BINLOG" -print -delete 2>/dev/null | wc -l)
log "清理过期 binlog 归档 $DEL_N 个（保留 ${KEEP_DAYS_BINLOG} 天）"
log "===== 结束 ====="
exit 0
