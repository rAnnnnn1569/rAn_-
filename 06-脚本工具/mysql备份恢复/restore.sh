#!/bin/bash
# ============================================================
# MySQL 一键恢复（全量 + binlog 增量 / 时间点恢复 PITR）
#   用法:
#     ./restore.sh --full /opt/databk/date/mysql/clbs_date_20260910.sql.gz [选项]
#
#   选项:
#     --binlog FILE [FILE...]   增量 binlog 文件（按时间先后顺序给出）
#     --start-position N        回放起始位点（默认自动取 .pos 文件记录的位点）
#     --stop-position N         回放结束位点（点到点恢复）
#     --stop-datetime "YYYY-MM-DD HH:MM:SS"   恢复到指定时间点（PITR）
#     --yes                     确认执行（不加则只做预检、不写库）
#     --dry-run                 只打印将执行的命令
#
#   恢复顺序: 校验 sha256 → 解压导入全量 → 按位点/时间回放 binlog
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_DIR="${CONF_DIR:-$SCRIPT_DIR/conf}"
CONF_FILE="$CONF_DIR/backup.env"
CLIENT_CNF="${CLIENT_CNF:-$CONF_DIR/client.cnf}"

[ -r "$CONF_FILE" ] || { echo "[FATAL] 找不到配置: $CONF_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
. "$CONF_FILE"

FULL=""
SCHEMA=""
START_POS=""
STOP_POS=""
STOP_DATETIME=""
CONFIRM=0
DRY_RUN=0
BINLOGS=()

log() { echo "[$(date '+%F %T')] $*"; }
die() { echo "[ERROR] $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --full)          FULL="$2"; shift 2 ;;
    --schema)        SCHEMA="$2"; shift 2 ;;
    --binlog)        shift; while [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; do BINLOGS+=("$1"); shift; done ;;
    --start-position) START_POS="$2"; shift 2 ;;
    --stop-position)  STOP_POS="$2"; shift 2 ;;
    --stop-datetime)  STOP_DATETIME="$2"; shift 2 ;;
    --yes)           CONFIRM=1; shift ;;
    --dry-run)       DRY_RUN=1; shift ;;
    -h|--help)       sed -n '2,20p' "$0"; exit 0 ;;
    *)               die "未知参数: $1" ;;
  esac
done

[ -n "$FULL" ] || die "必须用 --full 指定全量备份文件（.sql.gz）"
[ -r "$FULL" ] || die "全量备份文件不存在或不可读: $FULL"

# ---------- 1. sha256 校验 ----------
if [ -f "${FULL}.sha256" ]; then
  log "校验 sha256 ..."
  if ( cd "$(dirname "$FULL")" && "$SHA256_BIN" -c "$(basename "$FULL").sha256" >/dev/null 2>&1 ); then
    log "sha256 校验通过"
  else
    die "sha256 校验失败！文件可能损坏，拒绝恢复"
  fi
else
  log "[WARN] 未找到 ${FULL}.sha256，跳过完整性校验"
fi

# ---------- 2. 确定增量起点 ----------
if [ -z "$START_POS" ] && [ -f "${FULL}.pos" ]; then
  START_POS=$(awk -F= '/^binlog_pos=/{print $2}' "${FULL}.pos")
  POS_FILE=$(awk -F= '/^binlog_file=/{print $2}' "${FULL}.pos")
  log "从 .pos 读取位点：${POS_FILE:-N/A} : ${START_POS:-N/A}"
fi

if [ ${#BINLOGS[@]} -gt 0 ] && [ -z "$START_POS" ]; then
  die "需要回放 binlog 但未取得起始位点；请用 --start-position 显式指定"
fi

# ---------- 2.1 被排除表的结构补偿文件（自动发现） ----------
# 顺序：--schema 显式指定 > .pos 中记录的 schema_file > 按文件名推导
if [ -z "$SCHEMA" ] && [ -f "${FULL}.pos" ]; then
  SF=$(awk -F= '/^schema_file=/{print $2}' "${FULL}.pos")
  if [ -n "$SF" ] && [ -f "$(dirname "$FULL")/$SF" ]; then
    SCHEMA="$(dirname "$FULL")/$SF"
  fi
fi
if [ -z "$SCHEMA" ]; then
  AUTO="${FULL%.sql.gz}_ignored_schema.sql.gz"
  [ -f "$AUTO" ] && SCHEMA="$AUTO"
fi

# ---------- 3. 预检汇总 ----------
log "================ 恢复计划 ================"
log "全量文件  : $FULL"
if [ -n "$SCHEMA" ]; then log "结构文件  : $SCHEMA"; fi
if [ ${#BINLOGS[@]} -gt 0 ]; then log "增量文件  : ${BINLOGS[*]}"; fi
if [ -n "$START_POS" ]; then log "起始位点  : $START_POS"; fi
if [ -n "$STOP_POS" ]; then log "结束位点  : $STOP_POS"; fi
if [ -n "$STOP_DATETIME" ]; then log "截止时间  : $STOP_DATETIME"; fi
log "=========================================="

if [ "$DRY_RUN" -eq 1 ]; then
  log "[dry-run] 将执行：$GZIP_BIN -dc \"$FULL\" | $MYSQL_BIN --defaults-extra-file=$CLIENT_CNF"
  exit 0
fi

if [ "$CONFIRM" -ne 1 ]; then
  die "预检完成。确认要覆盖导入请追加 --yes 重新执行"
fi

MYSQL_ARGS=(--defaults-extra-file="$CLIENT_CNF")

# ---------- 4. 导入全量 ----------
log "开始导入全量备份（同名库将被覆盖）..."
if "$GZIP_BIN" -dc "$FULL" | "$MYSQL_BIN" "${MYSQL_ARGS[@]}"; then
  log "全量导入完成"
else
  die "全量导入失败"
fi

# ---------- 4.1 补回被排除表的结构（如存在） ----------
if [ -n "$SCHEMA" ]; then
  if [ -r "$SCHEMA" ]; then
    if [ -f "${SCHEMA}.sha256" ]; then
      ( cd "$(dirname "$SCHEMA")" && "$SHA256_BIN" -c "$(basename "$SCHEMA").sha256" >/dev/null 2>&1 ) \
        || die "结构文件 sha256 校验失败: $SCHEMA"
    fi
    log "导入被排除表的结构: $(basename "$SCHEMA")"
    if "$GZIP_BIN" -dc "$SCHEMA" | "$MYSQL_BIN" "${MYSQL_ARGS[@]}"; then
      log "结构导入完成（数据仍需靠业务回填或历史附件恢复）"
    else
      die "结构导入失败: $SCHEMA"
    fi
  else
    log "[WARN] 指定的结构文件不可读，跳过: $SCHEMA"
  fi
fi

# ---------- 5. 回放 binlog ----------
if [ ${#BINLOGS[@]} -gt 0 ]; then
  IDX=0
  for f in "${BINLOGS[@]}"; do
    [ -r "$f" ] || die "binlog 文件不存在: $f"
    ARGS=(--no-defaults)                 # 关键：绕开 my.cnf 里 mysqlbinlog 不认的变量
    if [ "$IDX" -eq 0 ] && [ -n "$START_POS" ]; then
      ARGS+=(--start-position="$START_POS")
    fi
    if [ -n "$STOP_POS" ]; then ARGS+=(--stop-position="$STOP_POS"); fi
    if [ -n "$STOP_DATETIME" ]; then ARGS+=(--stop-datetime="$STOP_DATETIME"); fi

    log "回放 binlog: $f  ${ARGS[*]}"
    if "$MYSQLBINLOG_BIN" "${ARGS[@]}" "$f" | "$MYSQL_BIN" "${MYSQL_ARGS[@]}"; then
      log "  -> 完成: $(basename "$f")"
    else
      die "binlog 回放失败: $f"
    fi
    IDX=$((IDX + 1))
  done
fi

log "================ 恢复结束 ================"
log "建议核对：表数量、关键表行数、最近一条业务数据的时间戳"
exit 0
