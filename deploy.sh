#!/usr/bin/env bash
# 生产部署脚本：初始化目录、恢复 Neo4j dump、启动应用、健康检查。
# 在 ECS（Linux）上运行。首次上线流程：
#   ./deploy.sh init
#   ./deploy.sh restore /data/backups/your.dump
#   ./deploy.sh up
set -euo pipefail

COMPOSE_FILE="compose.prod.yaml"
ENV_FILE=".env"
NEO4J_IMAGE="neo4j:2026.05-enterprise"

# 默认路径（可被 .env 覆盖）
NEO4J_DATA_DIR_DEFAULT="/data/neo4j"
NEO4J_IMPORT_DIR_DEFAULT="/data/backups"
EVOLUTION_DATA_PATH_DEFAULT="./output/role_evolution_workbench_v2"
APP_PORT_DEFAULT="8080"

info() { echo -e "\033[1;32m[deploy]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
die()  { echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

# 从 .env 读取变量（若存在），缺失时用默认值
load_env() {
  if [[ -f "$ENV_FILE" ]]; then
    set -a; source "$ENV_FILE"; set +a
  fi
  NEO4J_DATA_DIR="${NEO4J_DATA_DIR:-$NEO4J_DATA_DIR_DEFAULT}"
  NEO4J_IMPORT_DIR="${NEO4J_IMPORT_DIR:-$NEO4J_IMPORT_DIR_DEFAULT}"
  EVOLUTION_DATA_PATH="${EVOLUTION_DATA_PATH:-$EVOLUTION_DATA_PATH_DEFAULT}"
  APP_PORT="${APP_PORT:-$APP_PORT_DEFAULT}"
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "未安装 docker，请先执行：curl -fsSL https://get.docker.com | sh"
  docker compose version >/dev/null 2>&1 || die "docker compose 插件不可用"
}

rand_password() {
  tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24 || true
}

wait_for_neo4j() {
  local tries=60
  info "等待 Neo4j 健康..."
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS "http://127.0.0.1:7474" >/dev/null 2>&1; then
      info "Neo4j 已健康。"
      return 0
    fi
    sleep 5
  done
  die "Neo4j 未能在超时时间内变健康，用 ./deploy.sh logs neo4j 查看日志。"
}

cmd_init() {
  require_docker
  load_env
  mkdir -p "$NEO4J_DATA_DIR" "$NEO4J_IMPORT_DIR" "$EVOLUTION_DATA_PATH"

  if [[ ! -f "$ENV_FILE" ]]; then
    local pass
    pass="$(rand_password)"
    [[ -n "$pass" ]] || pass="change-me-please-$(date +%s)"
    cat > "$ENV_FILE" <<EOF
# 生产部署环境变量（勿提交到 Git）
NEO4J_AUTH=neo4j/$pass
APP_PORT=$APP_PORT
NEO4J_DATA_DIR=$NEO4J_DATA_DIR
NEO4J_IMPORT_DIR=$NEO4J_IMPORT_DIR
EVOLUTION_DATA_PATH=$EVOLUTION_DATA_PATH
NEO4J_CONFIG_PATH=./config/neo4j_connection.json
BASE_IMAGE=python:3.12-slim
EOF
    info "已生成 $ENV_FILE（含随机 Neo4j 密码），请妥善保管："
    cat "$ENV_FILE"
  else
    info "$ENV_FILE 已存在，跳过生成。"
  fi

  # 重新加载，确保拿到 NEO4J_AUTH（无论 .env 是刚生成还是已存在）
  set -a; source "$ENV_FILE"; set +a

  # 同步生成应用连接配置，保证密码与 NEO4J_AUTH 一致
  local pass
  pass="${NEO4J_AUTH#*/}"
  cat > config/neo4j_connection.json <<EOF
{
  "http_uri": "http://neo4j:7474",
  "database": "neo4j",
  "username": "neo4j",
  "password": "$pass",
  "timeout_seconds": 120
}
EOF
  chmod 600 config/neo4j_connection.json
  info "已生成 config/neo4j_connection.json（密码与 .env 一致）。"
  info "初始化完成。下一步：把 .dump 文件放到 $NEO4J_IMPORT_DIR 后执行 ./deploy.sh restore <文件名>"
}

cmd_restore() {
  require_docker
  load_env
  [[ $# -ge 1 ]] || die "用法：./deploy.sh restore <dump文件路径或文件名>"
  local dump_src="$1"
  local dump_name
  dump_name="$(basename "$dump_src")"

  mkdir -p "$NEO4J_IMPORT_DIR"
  local dest="$NEO4J_IMPORT_DIR/$dump_name"
  if [[ -f "$dump_src" ]]; then
    # 源文件与目标相同时（已放在导入目录）无需复制，避免 cp 报 "same file"
    if [[ "$(readlink -f "$dump_src")" != "$(readlink -f "$dest")" ]]; then
      cp -f "$dump_src" "$dest"
      info "已复制 $dump_src -> $dest"
    fi
  elif [[ ! -f "$dest" ]]; then
    die "找不到 dump 文件：$dump_src（也不在 $NEO4J_IMPORT_DIR 中）"
  fi

  # 1) 首次启动 Neo4j，初始化 system 库并设置密码
  docker compose -f "$COMPOSE_FILE" up -d neo4j
  wait_for_neo4j

  # 2) 停库（neo4j-admin load 要求目标库离线）
  info "停止 Neo4j 以载入 dump..."
  docker compose -f "$COMPOSE_FILE" stop neo4j

  # 3) 载入 dump 到默认数据库 neo4j
  info "载入 dump：$dump_name"
  docker run --rm \
    -e NEO4J_ACCEPT_LICENSE_AGREEMENT=yes \
    -v "$NEO4J_DATA_DIR:/data" \
    -v "$NEO4J_IMPORT_DIR:/import:ro" \
    "$NEO4J_IMAGE" \
    neo4j-admin database load --from-path="/import" --overwrite-destination=true neo4j

  # 4) 重启
  info "重启 Neo4j..."
  docker compose -f "$COMPOSE_FILE" start neo4j
  wait_for_neo4j
  info "恢复完成。执行 ./deploy.sh up 启动应用。"
}

cmd_up() {
  require_docker
  load_env
  [[ -f config/neo4j_connection.json ]] || die "缺少 config/neo4j_connection.json，请先执行 ./deploy.sh init"
  docker compose -f "$COMPOSE_FILE" up -d --build
  info "应用已启动。健康检查："
  sleep 2
  curl -fsS "http://127.0.0.1:${APP_PORT}/healthz" || true
  echo
}

cmd_down() {
  load_env
  docker compose -f "$COMPOSE_FILE" down
  info "已停止全部服务（数据保留在 $NEO4J_DATA_DIR）。"
}

cmd_status() {
  load_env
  docker compose -f "$COMPOSE_FILE" ps
  echo
  curl -fsS "http://127.0.0.1:${APP_PORT}/healthz" || true
  echo
}

cmd_logs() {
  load_env
  docker compose -f "$COMPOSE_FILE" logs --tail=100 "$@"
}

usage() {
  cat <<'EOF'
用法：./deploy.sh <命令>

命令：
  init               初始化目录、生成 .env 和 config/neo4j_connection.json
  restore <dump>     启动 Neo4j 并载入 .dump 文件
  up                 构建并启动应用（neo4j 后端）
  down               停止全部服务（数据保留）
  status             查看服务状态与健康接口
  logs [服务]        查看日志

典型首次上线流程：
  ./deploy.sh init
  ./deploy.sh restore /data/backups/your.dump
  ./deploy.sh up
EOF
}

main() {
  [[ $# -ge 1 ]] || { usage; exit 1; }
  local cmd="$1"; shift
  case "$cmd" in
    init)    cmd_init "$@" ;;
    restore) cmd_restore "$@" ;;
    up)      cmd_up "$@" ;;
    down)    cmd_down "$@" ;;
    status)  cmd_status "$@" ;;
    logs)    cmd_logs "$@" ;;
    *)       usage; exit 1 ;;
  esac
}

main "$@"
