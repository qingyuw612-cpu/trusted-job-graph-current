#!/usr/bin/env bash
# Safe in-place production upgrade with automatic code/config rollback.
# Graph data and application state are never copied over or deleted.
set -Eeuo pipefail

SOURCE_ROOT="${SOURCE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
GRAPH_SOURCE="${GRAPH_SOURCE:-${SOURCE_ROOT}/trusted-job-graph-current}"
APP_ROOT="${APP_ROOT:-/opt/talentgraph}"
CONFIG_ROOT="${CONFIG_ROOT:-/etc/talentgraph}"
BACKUP_PARENT="${BACKUP_PARENT:-/var/backups}"
STAMP="$(date +%Y%m%d%H%M%S)"
BACKUP_ROOT="${BACKUP_PARENT}/talentgraph-pre-${STAMP}"
ENV_FILE="${CONFIG_ROOT}/talentgraph.env"
DOMAIN="${TG_DOMAIN:-talentgraphagent.site}"
APP_USER="${APP_USER:-talentgraph}"

die() { echo "[talentgraph-upgrade] $*" >&2; exit 1; }
info() { echo "[talentgraph-upgrade] $*"; }

repair_working_directories() {
  install -d -o "$APP_USER" -g "$APP_USER" -m 0755 \
    "$APP_ROOT/trusted-job-graph-current" \
    "$APP_ROOT/resume-analysis-agent" \
    "$APP_ROOT/qianduan" \
    "$APP_ROOT/qianduan/html-main2" \
    "$APP_ROOT/爬虫代码"
}

[[ "$(id -u)" == 0 ]] || die "run this script with sudo"
[[ -f "${GRAPH_SOURCE}/deploy/install_systemd.sh" ]] || die "release layout is invalid"
[[ -d "$APP_ROOT" ]] || die "existing production app root was not found"
[[ ! -e "$BACKUP_ROOT" ]] || die "backup target already exists"

# Reuse only the known credential/config keys from the root-owned production
# environment. Values are assigned literally; the file is never evaluated.
if [[ -r "$ENV_FILE" ]]; then
  while IFS='=' read -r key value; do
    case "$key" in
      NEO4J_PASSWORD|NEO4J_USER|NEO4J_DATABASE|IFLYTEK_SPARK_API_PASSWORD|IFLYTEK_SPARK_MODEL|IFLYTEK_SPARK_BASE_URL)
        if [[ -z "${!key:-}" ]]; then
          printf -v "$key" '%s' "$value"
          export "$key"
        fi
        ;;
    esac
  done < "$ENV_FILE"
fi
[[ -n "${NEO4J_PASSWORD:-}" ]] || die "NEO4J_PASSWORD is unavailable"
[[ -n "${IFLYTEK_SPARK_API_PASSWORD:-}" ]] || die "IFLYTEK_SPARK_API_PASSWORD is unavailable"

install -d -m 0700 "$BACKUP_ROOT/app" "$BACKUP_ROOT/systemd" "$BACKUP_ROOT/nginx"
for directory in trusted-job-graph-current resume-analysis-agent qianduan/html-main2 "爬虫代码"; do
  if [[ -d "${APP_ROOT}/${directory}" ]]; then
    install -d -m 0700 "${BACKUP_ROOT}/app/${directory}"
    rsync -a --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
      "${APP_ROOT}/${directory}/" "${BACKUP_ROOT}/app/${directory}/"
  fi
done
[[ -d "$CONFIG_ROOT" ]] && cp -a "$CONFIG_ROOT" "$BACKUP_ROOT/config"
for unit in /etc/systemd/system/talentgraph-*; do
  [[ -e "$unit" ]] && cp -a "$unit" "$BACKUP_ROOT/systemd/"
done
nginx_file=/etc/nginx/sites-available/talentgraph
[[ -e "$nginx_file" || -L "$nginx_file" ]] && cp -a "$nginx_file" "$BACKUP_ROOT/nginx/"
info "rollback backup created: $BACKUP_ROOT"

rollback() {
  local code=$?
  trap - ERR
  info "upgrade failed; restoring the previous release"
  systemctl stop talentgraph-crawler.service || true
  for directory in trusted-job-graph-current resume-analysis-agent qianduan/html-main2 "爬虫代码"; do
    if [[ -d "${BACKUP_ROOT}/app/${directory}" ]]; then
      rsync -a "${BACKUP_ROOT}/app/${directory}/" "${APP_ROOT}/${directory}/"
    fi
  done
  repair_working_directories
  [[ -d "${BACKUP_ROOT}/config" ]] && rsync -a "${BACKUP_ROOT}/config/" "$CONFIG_ROOT/"
  for unit in "$BACKUP_ROOT"/systemd/talentgraph-*; do
    [[ -e "$unit" ]] && cp -a "$unit" /etc/systemd/system/
  done
  if [[ -e "$BACKUP_ROOT/nginx/talentgraph" || -L "$BACKUP_ROOT/nginx/talentgraph" ]]; then
    cp -a "$BACKUP_ROOT/nginx/talentgraph" /etc/nginx/sites-available/talentgraph
    ln -sfn /etc/nginx/sites-available/talentgraph /etc/nginx/sites-enabled/talentgraph
  fi
  systemctl daemon-reload || true
  systemctl restart talentgraph-graph talentgraph-evolution talentgraph-resume talentgraph-frontend || true
  nginx -t && systemctl reload nginx || true
  exit "$code"
}
trap rollback ERR

export SOURCE_ROOT GRAPH_SOURCE APP_ROOT APP_USER CONFIG_ROOT TG_DOMAIN="$DOMAIN"
bash "${GRAPH_SOURCE}/deploy/install_systemd.sh"

# Backfill the canonical skill metadata as part of every production upgrade.
# The migration is additive/idempotent and runs under the same maintenance
# lock as crawler and normalization jobs, so it cannot race a publish cycle.
MIGRATION_REPORT="/var/lib/talentgraph/stack-backfill-${STAMP}.json"
/usr/bin/flock -w 3600 /var/lib/talentgraph/maintenance.lock \
  "${APP_ROOT}/.venv/bin/python" \
  "${APP_ROOT}/trusted-job-graph-current/scripts/backfill_normalized_skill_stacks.py" \
  --neo4j-config "${CONFIG_ROOT}/neo4j_connection.json" --apply --report "$MIGRATION_REPORT"
chown "${APP_USER}:${APP_USER}" "$MIGRATION_REPORT"

# Reclassify every formal Role from the same controlled taxonomy used by the
# publisher. Unknown roles fail the upgrade instead of silently returning to
# the former "扩展岗位（待细分）" catch-all.
ROLE_FAMILY_REPORT="/var/lib/talentgraph/role-family-migration-${STAMP}.json"
/usr/bin/flock -w 3600 /var/lib/talentgraph/maintenance.lock \
  "${APP_ROOT}/.venv/bin/python" \
  "${APP_ROOT}/trusted-job-graph-current/scripts/reclassify_role_families.py" \
  --neo4j-config "${CONFIG_ROOT}/neo4j_connection.json" --apply --report "$ROLE_FAMILY_REPORT"
chown "${APP_USER}:${APP_USER}" "$ROLE_FAMILY_REPORT"

for service in talentgraph-graph talentgraph-evolution talentgraph-resume talentgraph-frontend; do
  systemctl is-active --quiet "$service"
done

graph_file="$(mktemp)"
resume_file="$(mktemp)"
evolution_file="$(mktemp)"
frontend_file="$(mktemp)"
trap 'rm -f "$graph_file" "$resume_file" "$evolution_file" "$frontend_file"' EXIT
curl -fsS --retry 10 --retry-connrefused --retry-delay 2 http://127.0.0.1:8010/api/health -o "$graph_file"
curl -fsS --retry 10 --retry-connrefused --retry-delay 2 http://127.0.0.1:8000/health -o "$resume_file"
curl -fsS --retry 10 --retry-connrefused --retry-delay 2 http://127.0.0.1:8070/api/v1/evolution/health -o "$evolution_file"
curl -fsS --retry 10 --retry-connrefused --retry-delay 2 http://127.0.0.1:8090/index.html -o "$frontend_file"
python3 - "$graph_file" "$resume_file" "$evolution_file" "$frontend_file" <<'PY'
import json, sys
graph = json.load(open(sys.argv[1], encoding="utf-8"))
resume = json.load(open(sys.argv[2], encoding="utf-8"))
evolution = json.load(open(sys.argv[3], encoding="utf-8"))
frontend = open(sys.argv[4], encoding="utf-8").read()
release = "closed-loop-2026.08"
assert graph.get("status") == "ok" and graph.get("backend") == "neo4j"
assert graph.get("release_id") == release and int(graph.get("counts", {}).get("jds", 0)) > 0
assert resume.get("release_id") == release and resume.get("data_mode") == "production"
assert evolution.get("release_id") == release and evolution.get("data_mode") == "production"
assert evolution.get("source", {}).get("connected") is True
assert f'talentgraph-release" content="{release}' in frontend
PY

trap - ERR
trap 'rm -f "$graph_file" "$resume_file" "$evolution_file" "$frontend_file"' EXIT
info "immediate production checks passed"
info "the first maintenance cycle is running asynchronously; run check_production.sh after it completes"
