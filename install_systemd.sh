#!/usr/bin/env bash
# Install the application services and continuous graph-maintenance timers.
# Run on the final Linux server as root. This script never logs credentials and
# never deletes application data or uses rsync --delete.
set -Eeuo pipefail

SOURCE_ROOT="${SOURCE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
GRAPH_SOURCE="${GRAPH_SOURCE:-${SOURCE_ROOT}/trusted-job-graph-current}"
# The graph repository is the integrated release source of truth.  The
# workspace can contain older standalone copies of these two projects; using
# them here silently deploys a stale frontend or an incompatible resume API.
RESUME_SOURCE="${RESUME_SOURCE:-${GRAPH_SOURCE}/resume-analysis-agent}"
FRONTEND_SOURCE="${FRONTEND_SOURCE:-${GRAPH_SOURCE}/qianduan/html-main2}"
CRAWLER_SOURCE="${CRAWLER_SOURCE:-${SOURCE_ROOT}/爬虫代码}"
APP_ROOT="${APP_ROOT:-/opt/talentgraph}"
STATE_ROOT="${STATE_ROOT:-/var/lib/talentgraph}"
CONFIG_ROOT="${CONFIG_ROOT:-/etc/talentgraph}"
LOG_ROOT="${LOG_ROOT:-/var/log/talentgraph}"
APP_USER="${APP_USER:-talentgraph}"
VENV="${APP_ROOT}/.venv"
DOMAIN="${TG_DOMAIN:-talentgraphagent.site}"
TLS_CERT="${TG_TLS_CERT:-/etc/letsencrypt/live/${DOMAIN}/fullchain.pem}"
TLS_KEY="${TG_TLS_KEY:-/etc/letsencrypt/live/${DOMAIN}/privkey.pem}"
HTTP_ONLY="${TG_HTTP_ONLY:-0}"
NEO4J_UNIT="${NEO4J_UNIT:-}"
CRAWLER_INTERVAL="${TG_CRAWLER_INTERVAL:-12h}"

die() { echo "[talentgraph] $*" >&2; exit 1; }
info() { echo "[talentgraph] $*"; }

[[ "$(id -u)" == 0 ]] || die "请使用 sudo 运行。"
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || die "TG_DOMAIN 只允许字母、数字、点和连字符。"
[[ "$CRAWLER_INTERVAL" =~ ^[1-9][0-9]*(min|h|d)$ ]] || die "TG_CRAWLER_INTERVAL 必须类似 30min、6h 或 1d。"
[[ -d "$GRAPH_SOURCE" && -f "$GRAPH_SOURCE/display_graph_handoff.py" ]] || die "缺少图谱工程：$GRAPH_SOURCE"
[[ -d "$RESUME_SOURCE" && -f "$RESUME_SOURCE/api_server.py" ]] || die "缺少简历工程：$RESUME_SOURCE"
[[ -d "$FRONTEND_SOURCE" && -f "$FRONTEND_SOURCE/index.html" ]] || die "缺少统一前端：$FRONTEND_SOURCE"
[[ -d "$CRAWLER_SOURCE" && -f "$CRAWLER_SOURCE/spider_zhilian_step1.py" ]] || die "缺少爬虫入口：$CRAWLER_SOURCE"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-dev build-essential nginx curl rsync netcat-openbsd gettext-base ca-certificates
command -v systemctl >/dev/null || die "目标系统没有 systemd。"
if ! command -v google-chrome >/dev/null 2>&1 && ! command -v google-chrome-stable >/dev/null 2>&1 \
  && ! command -v microsoft-edge >/dev/null 2>&1; then
  chrome_deb="$(mktemp --suffix=.deb)"
  trap 'rm -f "$chrome_deb"' EXIT
  curl -fsSLo "$chrome_deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y "$chrome_deb"
  rm -f "$chrome_deb"
  trap - EXIT
fi

id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_ROOT" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" "$APP_ROOT" "$STATE_ROOT" "$STATE_ROOT/evolution" "$STATE_ROOT/raw-jd" "$STATE_ROOT/full-normalization" "$STATE_ROOT/crawler" "$LOG_ROOT"
install -d -o root -g "$APP_USER" -m 0750 "$CONFIG_ROOT"
install -d -o "$APP_USER" -g "$APP_USER" -m 0755 "$APP_ROOT/trusted-job-graph-current" "$APP_ROOT/resume-analysis-agent" "$APP_ROOT/qianduan" "$APP_ROOT/qianduan/html-main2" "$APP_ROOT/爬虫代码"

# Keep remote user changes and data. Secrets and generated results are explicitly
# excluded from the code sync; copy new/changed files without deleting anything.
rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
  --exclude='.env' --exclude='config/neo4j_connection.json' --exclude='output' \
  "$GRAPH_SOURCE/" "$APP_ROOT/trusted-job-graph-current/"
rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.env' --exclude='tests' \
  "$RESUME_SOURCE/" "$APP_ROOT/resume-analysis-agent/"
rsync -a "$FRONTEND_SOURCE/" "$APP_ROOT/qianduan/html-main2/"
rsync -a "$CRAWLER_SOURCE/" "$APP_ROOT/爬虫代码/"

# A release may be extracted from a private (0700) staging directory. Keep the
# exact systemd WorkingDirectory roots traversable after every sync without
# broad recursive permission changes to application files.
install -d -o "$APP_USER" -g "$APP_USER" -m 0755 "$APP_ROOT/trusted-job-graph-current" "$APP_ROOT/resume-analysis-agent" "$APP_ROOT/qianduan" "$APP_ROOT/qianduan/html-main2" "$APP_ROOT/爬虫代码"

python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel
"$VENV/bin/pip" install --index-url https://download.pytorch.org/whl/cpu torch
"$VENV/bin/pip" install fastapi 'uvicorn[standard]' python-multipart python-dotenv neo4j 'markitdown[pdf,docx]' mammoth docx2txt pdfplumber matplotlib numpy langchain-openai requests selenium websocket-client 'sentence-transformers>=2.2'

install -o "$APP_USER" -g "$APP_USER" -m 0750 -d "$STATE_ROOT/evolution"
if [[ -z "${NEO4J_PASSWORD:-}" ]]; then
  die "请以环境变量 NEO4J_PASSWORD 提供 Neo4j 密码（不会写入日志）。"
fi
if [[ -z "${IFLYTEK_SPARK_API_PASSWORD:-}" ]]; then
  die "自动采集发布需要环境变量 IFLYTEK_SPARK_API_PASSWORD（不会写入日志）。"
fi
if [[ "${#NEO4J_PASSWORD}" -lt 12 ]]; then die "NEO4J_PASSWORD 至少 12 个字符。"; fi
[[ "$NEO4J_PASSWORD" =~ ^[A-Za-z0-9._%+=,@:-]+$ ]] || die "NEO4J_PASSWORD 请使用不含空格、引号、斜杠的 ASCII 强密码。"
umask 077
NEO4J_PASSWORD="$NEO4J_PASSWORD" NEO4J_USER="${NEO4J_USER:-neo4j}" NEO4J_DATABASE="${NEO4J_DATABASE:-neo4j}" \
  python3 - "$CONFIG_ROOT/neo4j_connection.json" <<'PY'
import json, os, sys
from pathlib import Path
target = Path(sys.argv[1])
target.write_text(json.dumps({
    "http_uri": "http://127.0.0.1:7474",
    "bolt_uri": "bolt://127.0.0.1:7687",
    "database": os.environ.get("NEO4J_DATABASE", "neo4j"),
    "username": os.environ.get("NEO4J_USER", "neo4j"),
    "password": os.environ["NEO4J_PASSWORD"],
    "timeout_seconds": 30,
}, ensure_ascii=False, indent=2), encoding="utf-8")
PY
chmod 0640 "$CONFIG_ROOT/neo4j_connection.json"

cat > "$CONFIG_ROOT/talentgraph.env" <<EOF
STORE_BACKEND=neo4j
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USER=${NEO4J_USER:-neo4j}
NEO4J_PASSWORD=${NEO4J_PASSWORD}
NEO4J_DATABASE=${NEO4J_DATABASE:-neo4j}
NEO4J_CONFIG_PATH=${CONFIG_ROOT}/neo4j_connection.json
EVOLUTION_DATA_ROOT=${STATE_ROOT}/evolution
EVOLUTION_API_URL=http://127.0.0.1:8070/api/v1/evolution
RAW_JD_ARCHIVE_ROOT=${STATE_ROOT}/raw-jd
TG_APP_ROOT=${APP_ROOT}
TG_STATE_ROOT=${STATE_ROOT}
TG_CONFIG_ROOT=${CONFIG_ROOT}
TG_CRAWLER_SOURCE_DIR=${APP_ROOT}/爬虫代码
TG_CRAWLER_OUTPUT_ROOT=${STATE_ROOT}/crawler
TG_MAINTENANCE_LOCK=${STATE_ROOT}/maintenance.lock
TG_RELEASE_ID=closed-loop-2026.08
TG_CRAWLER_SCAN_MODE=${TG_CRAWLER_SCAN_MODE:-full}
TG_CRAWLER_PAGES=${TG_CRAWLER_PAGES:-3}
TG_CRAWLER_COLLECTION_LIMIT=${TG_CRAWLER_COLLECTION_LIMIT:-1500}
TG_CRAWLER_PIPELINE_LIMIT=${TG_CRAWLER_PIPELINE_LIMIT:-0}
TG_CRAWLER_NEW_ROLE_LIMIT=${TG_CRAWLER_NEW_ROLE_LIMIT:-50}
TG_CRAWLER_ABILITY_CHANGE_LIMIT=${TG_CRAWLER_ABILITY_CHANGE_LIMIT:-40}
PYTHONUNBUFFERED=1
EOF
if [[ -n "${IFLYTEK_SPARK_API_PASSWORD:-}" ]]; then
  printf 'IFLYTEK_SPARK_API_PASSWORD=%s\nIFLYTEK_SPARK_MODEL=%s\nIFLYTEK_SPARK_BASE_URL=%s\n' \
    "${IFLYTEK_SPARK_API_PASSWORD}" "${IFLYTEK_SPARK_MODEL:-lite}" \
    "${IFLYTEK_SPARK_BASE_URL:-https://spark-api-open.xf-yun.com/v1}" \
    >> "$CONFIG_ROOT/talentgraph.env"
fi
chmod 0640 "$CONFIG_ROOT/talentgraph.env"
chown root:"$APP_USER" "$CONFIG_ROOT/talentgraph.env" "$CONFIG_ROOT/neo4j_connection.json"

# If a Neo4j instance is already listening, leave it untouched. Otherwise use a
# persistent Docker container managed by its own systemd unit.
NEO4J_DOCKER=0
neo4j_bolt_ready() {
  if command -v nc >/dev/null 2>&1; then
    nc -z -w 2 127.0.0.1 7687 >/dev/null 2>&1
  else
    timeout 2 bash -c '</dev/tcp/127.0.0.1/7687' >/dev/null 2>&1
  fi
}
NEO4J_EXTERNAL_UNIT=""
if ! curl -fsS --max-time 2 http://127.0.0.1:7474 >/dev/null 2>&1 && ! neo4j_bolt_ready; then
  command -v docker >/dev/null 2>&1 || die "Neo4j 未运行且未安装 Docker；请先安装/启动现有 Neo4j。"
  docker image inspect neo4j:5-community >/dev/null 2>&1 || docker pull neo4j:5-community
  docker inspect talentgraph-neo4j >/dev/null 2>&1 || docker run -d --name talentgraph-neo4j \
    --memory=1200m --restart unless-stopped -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 \
    -e "NEO4J_AUTH=${NEO4J_USER:-neo4j}/${NEO4J_PASSWORD}" \
    -v talentgraph_neo4j_data:/data neo4j:5-community >/dev/null
  docker update --restart unless-stopped talentgraph-neo4j >/dev/null
  NEO4J_DOCKER=1
else
  # An already-running native Neo4j must have a real systemd owner. Do not
  # claim to supervise it with a no-op unit: require/parameterize its unit.
  if command -v docker >/dev/null 2>&1 && docker inspect talentgraph-neo4j >/dev/null 2>&1; then
    NEO4J_DOCKER=1
  else
    if [[ -n "$NEO4J_UNIT" ]] && systemctl cat "$NEO4J_UNIT" >/dev/null 2>&1; then
      NEO4J_EXTERNAL_UNIT="$NEO4J_UNIT"
    else
      for candidate in neo4j.service neo4j@neo4j.service; do
        if systemctl cat "$candidate" >/dev/null 2>&1; then NEO4J_EXTERNAL_UNIT="$candidate"; break; fi
      done
    fi
    [[ -n "$NEO4J_EXTERNAL_UNIT" ]] || die "已检测到 Neo4j，但没有可管理的 systemd unit；请设置 NEO4J_UNIT=实际 unit，或改用本脚本创建的 Docker Neo4j。"
    NEO4J_DOCKER=0
  fi
fi

if [[ "$NEO4J_DOCKER" == 1 ]]; then
cat > /etc/systemd/system/talentgraph-neo4j.service <<'EOF'
[Unit]
Description=TalentGraph Neo4j dependency
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/docker start talentgraph-neo4j
ExecStop=/usr/bin/docker stop -t 30 talentgraph-neo4j
ExecReload=/usr/bin/docker restart talentgraph-neo4j

[Install]
WantedBy=multi-user.target
EOF
elif [[ -n "$NEO4J_EXTERNAL_UNIT" ]]; then
cat > /etc/systemd/system/talentgraph-neo4j.service <<EOF
[Unit]
Description=TalentGraph Neo4j dependency (${NEO4J_EXTERNAL_UNIT})
After=network-online.target
Wants=network-online.target
Requires=${NEO4J_EXTERNAL_UNIT}
After=${NEO4J_EXTERNAL_UNIT}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/systemctl start ${NEO4J_EXTERNAL_UNIT}
ExecStop=/usr/bin/systemctl stop ${NEO4J_EXTERNAL_UNIT}

[Install]
WantedBy=multi-user.target
EOF
else
die "Neo4j 管理模式未确定。"
fi

cat > /etc/systemd/system/talentgraph-graph.service <<EOF
[Unit]
Description=TalentGraph graph API (8010)
After=talentgraph-neo4j.service
Requires=talentgraph-neo4j.service

[Service]
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_ROOT}/trusted-job-graph-current
EnvironmentFile=${CONFIG_ROOT}/talentgraph.env
ExecStart=${VENV}/bin/python display_graph_handoff.py serve --neo4j-config ${CONFIG_ROOT}/neo4j_connection.json --host 127.0.0.1 --port 8010
Restart=always
RestartSec=5
UMask=0077
StandardOutput=append:${LOG_ROOT}/graph.log
StandardError=append:${LOG_ROOT}/graph-error.log
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/talentgraph-evolution.service <<EOF
[Unit]
Description=TalentGraph role evolution review API (8070)
After=talentgraph-graph.service
Requires=talentgraph-graph.service

[Service]
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_ROOT}/trusted-job-graph-current
EnvironmentFile=${CONFIG_ROOT}/talentgraph.env
ExecStart=${VENV}/bin/python -m new_role_discovery.app --neo4j-config ${CONFIG_ROOT}/neo4j_connection.json --data-root ${STATE_ROOT}/evolution --host 127.0.0.1 --port 8070
Restart=always
RestartSec=5
UMask=0077
StandardOutput=append:${LOG_ROOT}/evolution.log
StandardError=append:${LOG_ROOT}/evolution-error.log
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/talentgraph-resume.service <<EOF
[Unit]
Description=TalentGraph resume API (8000)
After=talentgraph-graph.service
Requires=talentgraph-graph.service

[Service]
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_ROOT}/resume-analysis-agent
EnvironmentFile=${CONFIG_ROOT}/talentgraph.env
ExecStart=${VENV}/bin/uvicorn api_server:app --host 127.0.0.1 --port 8000 --no-access-log
Restart=always
RestartSec=5
UMask=0077
StandardOutput=append:${LOG_ROOT}/resume.log
StandardError=append:${LOG_ROOT}/resume-error.log
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/talentgraph-frontend.service <<EOF
[Unit]
Description=TalentGraph unified frontend (8090)
After=network.target

[Service]
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_ROOT}/qianduan/html-main2
ExecStart=${VENV}/bin/python -m http.server 8090 --bind 127.0.0.1
Restart=always
RestartSec=3
UMask=0077
StandardOutput=append:${LOG_ROOT}/frontend.log
StandardError=append:${LOG_ROOT}/frontend-error.log
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

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

chown "$APP_USER":"$APP_USER" "$APP_ROOT/trusted-job-graph-current/deploy/run_scheduled_crawler.sh"
chmod 0750 "$APP_ROOT/trusted-job-graph-current/deploy/run_scheduled_crawler.sh"

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

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/talentgraph-crawler.timer <<EOF
[Unit]
Description=Continuously schedule TalentGraph data maintenance

[Timer]
OnBootSec=15min
OnUnitActiveSec=${CRAWLER_INTERVAL}
AccuracySec=5min
RandomizedDelaySec=15min

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/talentgraph-full-normalization.timer <<'EOF'
[Unit]
Description=Run TalentGraph full normalization weekly

[Timer]
OnCalendar=Sun *-*-* 03:30:00
Persistent=true
RandomizedDelaySec=20m

[Install]
WantedBy=timers.target
EOF

export TG_DOMAIN="$DOMAIN" TG_TLS_CERT="$TLS_CERT" TG_TLS_KEY="$TLS_KEY"
if [[ "$HTTP_ONLY" == 1 ]]; then
  info "TG_HTTP_ONLY=1：仅生成 HTTP 配置，未启用 HTTPS。"
  envsubst '$TG_DOMAIN' < "$(dirname "$0")/nginx.talentgraph.http.conf.template" > /etc/nginx/sites-available/talentgraph
else
  [[ -f "$TLS_CERT" && -f "$TLS_KEY" ]] || die "证书不存在：请先配置 TG_TLS_CERT/TG_TLS_KEY，或临时显式设置 TG_HTTP_ONLY=1。"
  envsubst '$TG_DOMAIN $TG_TLS_CERT $TG_TLS_KEY' < "$(dirname "$0")/nginx.talentgraph.conf.template" > /etc/nginx/sites-available/talentgraph
fi
ln -sfn /etc/nginx/sites-available/talentgraph /etc/nginx/sites-enabled/talentgraph
nginx -t

cat > /etc/logrotate.d/talentgraph <<EOF
${LOG_ROOT}/*.log {
  daily
  rotate 14
  size 20M
  missingok
  notifempty
  compress
  copytruncate
  create 0600 ${APP_USER} ${APP_USER}
}
EOF

systemctl daemon-reload
systemctl enable --now talentgraph-neo4j.service
systemctl enable talentgraph-graph.service talentgraph-evolution.service talentgraph-resume.service talentgraph-frontend.service
# enable --now does not restart an already-active unit after code or environment
# changes. A production upgrade must explicitly replace all four app processes.
systemctl restart talentgraph-graph.service talentgraph-evolution.service talentgraph-resume.service talentgraph-frontend.service
systemctl enable --now talentgraph-crawler.timer talentgraph-full-normalization.timer
systemctl reload nginx || systemctl restart nginx
# Start the first closed-loop maintenance run immediately.  The timer remains
# the owner of later cycles, and --no-block keeps installation responsive.
systemctl reset-failed talentgraph-crawler.service || true
systemctl start --no-block talentgraph-crawler.service
info "安装完成。请运行 deploy/check_production.sh --base-url https://${DOMAIN}。"
