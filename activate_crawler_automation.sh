#!/usr/bin/env bash
# Activate crawler automation on an existing /opt/talentgraph deployment.
# Expected staged files are uploaded separately so existing production files
# can be backed up before they are replaced.
set -Eeuo pipefail

APP_ROOT="${APP_ROOT:-/opt/talentgraph}"
STATE_ROOT="${STATE_ROOT:-/var/lib/talentgraph}"
CONFIG_ROOT="${CONFIG_ROOT:-/etc/talentgraph}"
LOG_ROOT="${LOG_ROOT:-/var/log/talentgraph}"
APP_USER="${APP_USER:-talentgraph}"
STAGE_ROOT="${STAGE_ROOT:-/tmp/talentgraph-crawler-stage}"
VENV="${APP_ROOT}/.venv"
INTERVAL="${TG_CRAWLER_INTERVAL:-12h}"
STAMP="$(date +%Y%m%d%H%M%S)"
BACKUP_ROOT="/var/backups/talentgraph-crawler-pre-${STAMP}"

die() { printf '[talentgraph-crawler] %s\n' "$*" >&2; exit 1; }
[[ "$(id -u)" == 0 ]] || die "请使用 sudo 运行。"
[[ "$INTERVAL" =~ ^[1-9][0-9]*(min|h|d)$ ]] || die "TG_CRAWLER_INTERVAL 必须类似 30min、6h 或 1d。"

for file in job_crawler_runner.py main_51job.py run_scheduled_crawler.sh; do
  [[ -f "${STAGE_ROOT}/${file}" ]] || die "缺少暂存文件：${STAGE_ROOT}/${file}"
done
[[ -x "${VENV}/bin/python" ]] || die "缺少生产 Python：${VENV}/bin/python"
[[ -f "${CONFIG_ROOT}/neo4j_connection.json" ]] || die "缺少 Neo4j 配置。"
[[ -f "${CONFIG_ROOT}/talentgraph.env" ]] || die "缺少生产环境文件。"
grep -q '^IFLYTEK_SPARK_API_PASSWORD=.' "${CONFIG_ROOT}/talentgraph.env" || die "生产环境尚未配置讯飞密钥。"

install -d -o root -g root -m 0700 "$BACKUP_ROOT"
for file in \
  "${APP_ROOT}/trusted-job-graph-current/job_crawler_runner.py" \
  "${APP_ROOT}/trusted-job-graph-current/deploy/run_scheduled_crawler.sh" \
  "${APP_ROOT}/爬虫代码/main_51job.py" \
  /etc/systemd/system/talentgraph-crawler.service \
  /etc/systemd/system/talentgraph-crawler.timer \
  /etc/systemd/system/talentgraph-full-normalization.service; do
  [[ -e "$file" ]] && cp -a "$file" "$BACKUP_ROOT/"
done
cp -a "${CONFIG_ROOT}/talentgraph.env" "$BACKUP_ROOT/talentgraph.env"

install -o "$APP_USER" -g "$APP_USER" -m 0640 \
  "${STAGE_ROOT}/job_crawler_runner.py" \
  "${APP_ROOT}/trusted-job-graph-current/job_crawler_runner.py"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 \
  "${APP_ROOT}/trusted-job-graph-current/deploy" \
  "${STATE_ROOT}/crawler" "$LOG_ROOT"
install -o "$APP_USER" -g "$APP_USER" -m 0750 \
  "${STAGE_ROOT}/run_scheduled_crawler.sh" \
  "${APP_ROOT}/trusted-job-graph-current/deploy/run_scheduled_crawler.sh"
install -o "$APP_USER" -g "$APP_USER" -m 0640 \
  "${STAGE_ROOT}/main_51job.py" \
  "${APP_ROOT}/爬虫代码/main_51job.py"

grep -q '^IFLYTEK_SPARK_MODEL=' "${CONFIG_ROOT}/talentgraph.env" || printf 'IFLYTEK_SPARK_MODEL=lite\n' >> "${CONFIG_ROOT}/talentgraph.env"
grep -q '^IFLYTEK_SPARK_BASE_URL=' "${CONFIG_ROOT}/talentgraph.env" || printf 'IFLYTEK_SPARK_BASE_URL=https://spark-api-open.xf-yun.com/v1\n' >> "${CONFIG_ROOT}/talentgraph.env"
grep -q '^TG_CRAWLER_SCAN_MODE=' "${CONFIG_ROOT}/talentgraph.env" || cat >> "${CONFIG_ROOT}/talentgraph.env" <<'EOF'
TG_CRAWLER_SCAN_MODE=full
TG_CRAWLER_PAGES=3
TG_CRAWLER_COLLECTION_LIMIT=1500
TG_CRAWLER_PIPELINE_LIMIT=0
TG_CRAWLER_NEW_ROLE_LIMIT=50
TG_CRAWLER_ABILITY_CHANGE_LIMIT=40
EOF
grep -q '^TG_CRAWLER_OUTPUT_ROOT=' "${CONFIG_ROOT}/talentgraph.env" || printf 'TG_CRAWLER_OUTPUT_ROOT=%s/crawler\n' "$STATE_ROOT" >> "${CONFIG_ROOT}/talentgraph.env"
grep -q '^TG_MAINTENANCE_LOCK=' "${CONFIG_ROOT}/talentgraph.env" || printf 'TG_MAINTENANCE_LOCK=%s/maintenance.lock\n' "$STATE_ROOT" >> "${CONFIG_ROOT}/talentgraph.env"
chown root:"$APP_USER" "${CONFIG_ROOT}/talentgraph.env"
chmod 0640 "${CONFIG_ROOT}/talentgraph.env"

"${VENV}/bin/pip" install --disable-pip-version-check requests selenium websocket-client
if ! command -v google-chrome >/dev/null 2>&1 && ! command -v google-chrome-stable >/dev/null 2>&1 \
  && ! command -v microsoft-edge >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  chrome_deb="$(mktemp --suffix=.deb)"
  trap 'rm -f "$chrome_deb"' EXIT
  curl -fsSLo "$chrome_deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y "$chrome_deb"
  rm -f "$chrome_deb"
  trap - EXIT
fi

cat > /etc/systemd/system/talentgraph-full-normalization.service <<EOF
[Unit]
Description=TalentGraph weekly full normalization calibration
After=talentgraph-neo4j.service
Requires=talentgraph-neo4j.service

[Service]
Type=oneshot
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_ROOT}/trusted-job-graph-current
EnvironmentFile=${CONFIG_ROOT}/talentgraph.env
ExecStart=/usr/bin/flock -w 3600 ${STATE_ROOT}/maintenance.lock ${VENV}/bin/python run_incremental_knowledge_graph.py --skip-import --normalization-mode full --publish --skip-new-role-discovery --work-dir ${STATE_ROOT}/full-normalization
UMask=0077
StandardOutput=append:${LOG_ROOT}/full-normalization.log
StandardError=append:${LOG_ROOT}/full-normalization-error.log
NoNewPrivileges=true
EOF

cat > /etc/systemd/system/talentgraph-crawler.service <<EOF
[Unit]
Description=TalentGraph scheduled crawl, clean, normalize and publish cycle
After=network-online.target talentgraph-neo4j.service
Wants=network-online.target
Requires=talentgraph-neo4j.service
StartLimitIntervalSec=6h
StartLimitBurst=2

[Service]
Type=oneshot
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_ROOT}/trusted-job-graph-current
EnvironmentFile=${CONFIG_ROOT}/talentgraph.env
ExecStart=/usr/bin/flock -w 3600 ${STATE_ROOT}/maintenance.lock ${APP_ROOT}/trusted-job-graph-current/deploy/run_scheduled_crawler.sh
TimeoutStartSec=10h
Restart=on-failure
RestartSec=30min
Nice=10
UMask=0077
StandardOutput=append:${LOG_ROOT}/crawler.log
StandardError=append:${LOG_ROOT}/crawler-error.log
NoNewPrivileges=true
PrivateTmp=true
EOF

cat > /etc/systemd/system/talentgraph-crawler.timer <<EOF
[Unit]
Description=Continuously schedule TalentGraph data maintenance

[Timer]
OnBootSec=15min
OnUnitActiveSec=${INTERVAL}
AccuracySec=5min
RandomizedDelaySec=15min

[Install]
WantedBy=timers.target
EOF

systemd-analyze verify \
  /etc/systemd/system/talentgraph-crawler.service \
  /etc/systemd/system/talentgraph-crawler.timer \
  /etc/systemd/system/talentgraph-full-normalization.service
systemctl daemon-reload
systemctl enable --now talentgraph-crawler.timer talentgraph-full-normalization.timer
printf '[talentgraph-crawler] activated; backup=%s\n' "$BACKUP_ROOT"
