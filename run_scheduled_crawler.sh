#!/usr/bin/env bash
# Execute one bounded production crawl -> clean -> normalize -> publish cycle.
# Scheduling and mutual exclusion are owned by systemd; this script only builds
# a validated argv array and never evaluates configuration as shell code.
set -Eeuo pipefail

APP_ROOT="${TG_APP_ROOT:-/opt/talentgraph}"
STATE_ROOT="${TG_STATE_ROOT:-/var/lib/talentgraph}"
CONFIG_ROOT="${TG_CONFIG_ROOT:-/etc/talentgraph}"
GRAPH_ROOT="${APP_ROOT}/trusted-job-graph-current"
CRAWLER_SOURCE="${TG_CRAWLER_SOURCE_DIR:-${APP_ROOT}/爬虫代码}"
OUTPUT_ROOT="${TG_CRAWLER_OUTPUT_ROOT:-${STATE_ROOT}/crawler}"
PYTHON="${TG_PYTHON:-${APP_ROOT}/.venv/bin/python}"

SCAN_MODE="${TG_CRAWLER_SCAN_MODE:-full}"
PAGES="${TG_CRAWLER_PAGES:-3}"
COLLECTION_LIMIT="${TG_CRAWLER_COLLECTION_LIMIT:-1000}"
PIPELINE_LIMIT="${TG_CRAWLER_PIPELINE_LIMIT:-0}"
NEW_ROLE_LIMIT="${TG_CRAWLER_NEW_ROLE_LIMIT:-50}"
ABILITY_CHANGE_LIMIT="${TG_CRAWLER_ABILITY_CHANGE_LIMIT:-40}"

die() { printf '[talentgraph-crawler] %s\n' "$*" >&2; exit 2; }
is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }

[[ -x "$PYTHON" ]] || die "Python 不可执行：$PYTHON"
[[ -f "$GRAPH_ROOT/job_crawler_runner.py" ]] || die "缺少统一采集入口"
[[ -d "$CRAWLER_SOURCE" ]] || die "缺少爬虫目录：$CRAWLER_SOURCE"
[[ -r "$CONFIG_ROOT/neo4j_connection.json" ]] || die "Neo4j 配置不可读"
[[ -n "${IFLYTEK_SPARK_API_PASSWORD:-}" ]] || die "未配置 IFLYTEK_SPARK_API_PASSWORD，拒绝产生无法完整处理的积压数据"
[[ "$SCAN_MODE" == "full" || "$SCAN_MODE" == "quick" ]] || die "TG_CRAWLER_SCAN_MODE 只能是 full 或 quick"
is_uint "$PAGES" && (( PAGES >= 1 && PAGES <= 20 )) || die "TG_CRAWLER_PAGES 必须是 1-20 的整数"
is_uint "$COLLECTION_LIMIT" && (( COLLECTION_LIMIT >= 20 && COLLECTION_LIMIT <= 2000 )) || die "TG_CRAWLER_COLLECTION_LIMIT 必须是 20-2000 的整数"
is_uint "$PIPELINE_LIMIT" || die "TG_CRAWLER_PIPELINE_LIMIT 必须是非负整数"
is_uint "$NEW_ROLE_LIMIT" && (( NEW_ROLE_LIMIT <= 50 )) || die "TG_CRAWLER_NEW_ROLE_LIMIT 必须是 0-50 的整数"
is_uint "$ABILITY_CHANGE_LIMIT" && (( ABILITY_CHANGE_LIMIT <= 100 )) || die "TG_CRAWLER_ABILITY_CHANGE_LIMIT 必须是 0-100 的整数"

install -d -m 0700 "$OUTPUT_ROOT"

exec "$PYTHON" -B "$GRAPH_ROOT/job_crawler_runner.py" run \
  --source-dir "$CRAWLER_SOURCE" \
  --output-root "$OUTPUT_ROOT" \
  --scan-mode "$SCAN_MODE" \
  --pages "$PAGES" \
  --collection-limit "$COLLECTION_LIMIT" \
  --pipeline-limit "$PIPELINE_LIMIT" \
  --new-role-limit "$NEW_ROLE_LIMIT" \
  --ability-change-limit "$ABILITY_CHANGE_LIMIT" \
  --fresh-scan \
  --non-interactive \
  --system-import \
  --system-publish \
  --neo4j-config "$CONFIG_ROOT/neo4j_connection.json"
