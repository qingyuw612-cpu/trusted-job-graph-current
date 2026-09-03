#!/usr/bin/env bash
# Read-only deployment check. It deliberately avoids printing response bodies,
# query strings, credentials, or user data.
set -Eeuo pipefail

BASE_URL="https://talentgraphagent.site"
ALLOW_INSECURE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url) BASE_URL="${2:?--base-url 需要值}"; shift 2 ;;
    --allow-insecure) ALLOW_INSECURE=1; shift ;;
    *) echo "用法：$0 [--base-url https://domain] [--allow-insecure]" >&2; exit 2 ;;
  esac
done
BASE_URL="${BASE_URL%/}"
[[ "$BASE_URL" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || { echo "非法 BASE_URL" >&2; exit 2; }
CURL_OPTS=(-sS)
[[ "$ALLOW_INSECURE" == 1 ]] && CURL_OPTS+=(-k)

failures=0
pass() { printf '[OK] %s\n' "$1"; }
fail() { printf '[FAIL] %s\n' "$1"; failures=$((failures + 1)); }
http_status() { curl "${CURL_OPTS[@]}" -o /dev/null --connect-timeout 5 --max-time 15 -w '%{http_code}' "$1" || printf '000'; }

check_http() {
  local label="$1" url="$2" expected="${3:-200}" status
  status="$(http_status "$url")"
  [[ "$status" == "$expected" ]] && pass "$label ($status)" || fail "$label ($status, expected $expected)"
}

if [[ "$BASE_URL" == https://* ]]; then
  status="$(http_status "$BASE_URL/")"
  [[ "$status" == 200 ]] && pass "HTTPS frontend ($status)" || fail "HTTPS frontend ($status)"
  if [[ "$ALLOW_INSECURE" == 0 ]]; then
    check_http "HTTP redirects to HTTPS" "${BASE_URL/https:/http:}/" 301
  fi
else
  [[ "$ALLOW_INSECURE" == 1 ]] || { echo "HTTP 检查必须显式传 --allow-insecure" >&2; exit 2; }
  check_http "HTTP frontend" "$BASE_URL/" 200
fi

check_http "evolution health" "$BASE_URL/api/v1/evolution/health" 200
check_http "graph health" "$BASE_URL/api/health" 200
check_http "resume health" "$BASE_URL/api/v1/resume/health" 200
check_http "maintenance status" "$BASE_URL/api/v1/maintenance/status" 200
check_http "radar configuration" "$BASE_URL/api/v1/radar/config" 200
check_http "frontend emerging roles" "$BASE_URL/emerging-roles.html" 200

for spec in "127.0.0.1:7687 Neo4j Bolt" "127.0.0.1:8000 resume" "127.0.0.1:8010 graph" "127.0.0.1:8070 evolution" "127.0.0.1:8090 frontend"; do
  endpoint="${spec%% *}"; label="${spec#* }"; host="${endpoint%:*}"; port="${endpoint##*:}"
  if command -v nc >/dev/null 2>&1; then
    nc -z -w 3 "$host" "$port" && pass "internal $label ($port)" || fail "internal $label ($port)"
  else
    curl -sS --connect-timeout 2 --max-time 3 -o /dev/null "http://$endpoint" && pass "internal $label ($port)" || fail "internal $label ($port)"
  fi
done

# Validate health semantics without echoing the JSON. The evolution health
# includes the Neo4j source and versioned review-storage availability.
health_file="$(mktemp)"
graph_file="$(mktemp)"
resume_file="$(mktemp)"
maintenance_file="$(mktemp)"
radar_file="$(mktemp)"
frontend_file="$(mktemp)"
facets_file="$(mktemp)"
roles_file="$(mktemp)"
panorama_file="$(mktemp)"
filtered_panorama_file="$(mktemp)"
trap 'rm -f "$health_file" "$graph_file" "$resume_file" "$maintenance_file" "$radar_file" "$frontend_file" "$facets_file" "$roles_file" "$panorama_file" "$filtered_panorama_file"' EXIT
if curl "${CURL_OPTS[@]}" -f --connect-timeout 5 --max-time 15 "$BASE_URL/api/v1/evolution/health" -o "$health_file" \
  && curl "${CURL_OPTS[@]}" -f --connect-timeout 5 --max-time 15 "$BASE_URL/api/health" -o "$graph_file" \
  && curl "${CURL_OPTS[@]}" -f --connect-timeout 5 --max-time 15 "$BASE_URL/api/v1/resume/health" -o "$resume_file" \
  && curl "${CURL_OPTS[@]}" -f --connect-timeout 5 --max-time 15 "$BASE_URL/api/v1/maintenance/status" -o "$maintenance_file" \
  && curl "${CURL_OPTS[@]}" -f --connect-timeout 5 --max-time 15 "$BASE_URL/api/v1/radar/config" -o "$radar_file" \
  && curl "${CURL_OPTS[@]}" -f --connect-timeout 5 --max-time 15 "$BASE_URL/" -o "$frontend_file" \
  && curl "${CURL_OPTS[@]}" -f --connect-timeout 5 --max-time 15 "$BASE_URL/api/v1/facets" -o "$facets_file" \
  && curl "${CURL_OPTS[@]}" -f --connect-timeout 5 --max-time 15 "$BASE_URL/api/v1/roles" -o "$roles_file"; then
  if python3 - "$health_file" "$graph_file" "$resume_file" "$maintenance_file" "$radar_file" "$frontend_file" "$facets_file" "$roles_file" <<'PY'
import json, sys
evolution = json.load(open(sys.argv[1], encoding='utf-8'))
graph = json.load(open(sys.argv[2], encoding='utf-8'))
resume = json.load(open(sys.argv[3], encoding='utf-8'))
maintenance = json.load(open(sys.argv[4], encoding='utf-8'))
radar = json.load(open(sys.argv[5], encoding='utf-8'))
frontend = open(sys.argv[6], encoding='utf-8').read()
facets = json.load(open(sys.argv[7], encoding='utf-8'))
roles = json.load(open(sys.argv[8], encoding='utf-8'))

assert graph.get('status') == 'ok'
assert graph.get('release_id') == 'closed-loop-2026.08'
assert graph.get('backend') == 'neo4j'
assert int(graph.get('counts', {}).get('roles', 0)) >= 20
assert int(graph.get('counts', {}).get('jds', 0)) > 0
assert resume.get('store_backend') == 'neo4j'
assert resume.get('release_id') == 'closed-loop-2026.08'
assert resume.get('data_mode') == 'production'
assert int(resume.get('roles_available', 0)) >= 20
assert evolution.get('status') in {'ok', 'degraded'}
assert evolution.get('release_id') == 'closed-loop-2026.08'
assert evolution.get('data_mode') == 'production'
assert evolution.get('source', {}).get('connected') is True
assert evolution.get('source', {}).get('schema_ready') is True
assert int(evolution.get('source', {}).get('usable_jds', 0)) > 0
assert evolution.get('review_storage', {}).get('available') is True
assert maintenance.get('freshness', {}).get('closed_loop_healthy') is True
assert len(radar.get('full_keywords', [])) >= 50
assert 'talentgraph-release" content="closed-loop-2026.08' in frontend
assert isinstance(facets.get('stacks'), list) and facets['stacks'], 'normalized stack facets are empty'
assert isinstance(roles, list) and roles and roles[0].get('role_id'), 'roles unavailable for stack gate'
PY
  then
    mapfile -t role_ids < <(python3 - "$roles_file" <<'PY'
import json, sys
for row in json.load(open(sys.argv[1], encoding='utf-8')):
    if row.get('role_id'):
        print(row['role_id'])
PY
    )
    mapfile -t stacks < <(python3 - "$facets_file" <<'PY'
import json, sys
for value in json.load(open(sys.argv[1], encoding='utf-8')).get('stacks', []):
    if value:
        print(value)
PY
    )
    stack_gate_passed=0
    selected_role_id=""
    selected_stack=""
    for role_id in "${role_ids[@]}"; do
      [[ -n "$role_id" ]] || continue
      if ! curl "${CURL_OPTS[@]}" -f --get --connect-timeout 5 --max-time 15 "$BASE_URL/api/v1/graph/panorama" \
        --data-urlencode "role_id=$role_id" --data-urlencode "min_support=0" --data-urlencode "skill_limit=50" -o "$panorama_file"; then
        continue
      fi
      for stack in "${stacks[@]}"; do
        [[ -n "$stack" ]] || continue
        if ! curl "${CURL_OPTS[@]}" -f --get --connect-timeout 5 --max-time 15 "$BASE_URL/api/v1/graph/panorama" \
          --data-urlencode "role_id=$role_id" --data-urlencode "stack=$stack" --data-urlencode "min_support=0" --data-urlencode "skill_limit=50" -o "$filtered_panorama_file"; then
          continue
        fi
        if python3 - "$panorama_file" "$filtered_panorama_file" "$stack" 2>/dev/null <<'PY'
import json, sys
all_graph = json.load(open(sys.argv[1], encoding='utf-8'))
filtered = json.load(open(sys.argv[2], encoding='utf-8'))
requested_stack = sys.argv[3]
all_skills = {node.get('entity_id') for node in all_graph.get('nodes', []) if node.get('type') == 'skill'}
filtered_skills = [node for node in filtered.get('nodes', []) if node.get('type') == 'skill']
assert filtered.get('filters', {}).get('stack') == requested_stack, 'stack filter was not applied'
assert filtered_skills and all(node.get('stack') for node in filtered_skills), 'filtered skill node stack missing'
assert filtered.get('stats', {}).get('filtered_jds', 0) < all_graph.get('stats', {}).get('filtered_jds', 0) or {node.get('entity_id') for node in filtered_skills} != all_skills, 'stack filter did not change result'
PY
        then
          stack_gate_passed=1
          selected_role_id="$role_id"
          selected_stack="$stack"
          break 2
        fi
      done
    done
    if [[ "$stack_gate_passed" == 1 ]]; then
      pass "normalized stack facets, filter behavior, and node metadata (${selected_role_id}/${selected_stack})"
    else
      fail "normalized stack facets, filter behavior, and node metadata"
    fi
    for required_stack in AI 后端 前端; do
      required_found=0
      for role_id in "${role_ids[@]}"; do
        [[ -n "$role_id" ]] || continue
        if curl "${CURL_OPTS[@]}" -f --get --connect-timeout 5 --max-time 15 "$BASE_URL/api/v1/graph/panorama" \
          --data-urlencode "role_id=$role_id" --data-urlencode "stack=$required_stack" \
          --data-urlencode "min_support=0" --data-urlencode "skill_limit=50" -o "$filtered_panorama_file" \
          && python3 - "$filtered_panorama_file" "$required_stack" 2>/dev/null <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding='utf-8'))
expected = sys.argv[2]
skills = [node for node in payload.get('nodes', []) if node.get('type') == 'skill']
assert payload.get('filters', {}).get('stack') == expected
assert int(payload.get('stats', {}).get('filtered_jds', 0)) > 0
assert skills and all(node.get('stack') == expected for node in skills)
PY
        then
          required_found=1
          break
        fi
      done
      if [[ "$required_found" == 1 ]]; then
        pass "required stack filter has data ($required_stack)"
      else
        fail "required stack filter has data ($required_stack)"
      fi
    done
    pass "production semantics, closed-loop freshness, and release identity"
  else
    fail "production semantics, closed-loop freshness, and release identity"
  fi
else
  fail "read production semantic payloads"
fi

# A deliberately invalid candidate-review request proves the reverse proxy
# reaches the write route without creating a run or including user content.
review_status="$(curl "${CURL_OPTS[@]}" -o /dev/null --connect-timeout 5 --max-time 15 -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' -d '{}' \
  "$BASE_URL/api/v1/evolution/runs/deploy-probe/candidates/deploy-probe/review" || printf '000')"
[[ "$review_status" == 404 || "$review_status" == 400 ]] && pass "review POST route reachable ($review_status)" || fail "review POST route ($review_status)"

if command -v systemctl >/dev/null 2>&1; then
  for svc in talentgraph-neo4j talentgraph-graph talentgraph-evolution talentgraph-resume talentgraph-frontend; do
    systemctl is-active --quiet "$svc" && pass "systemd $svc" || fail "systemd $svc"
  done
  for timer in talentgraph-crawler.timer talentgraph-full-normalization.timer; do
    systemctl is-active --quiet "$timer" && pass "systemd $timer" || fail "systemd $timer"
    systemctl is-enabled --quiet "$timer" && pass "enabled $timer" || fail "enabled $timer"
  done
fi

if command -v ss >/dev/null 2>&1; then
  public="$(ss -ltnH | awk '$4 ~ /:80$|:443$/ {print $4}' | tr '\n' ' ')"
  printf '[INFO] public listeners: %s\n' "${public:-none}"
  if ss -ltnH | awk '$4 ~ /:8000$|:8010$|:8070$|:8090$|:7687$/ {print $4}' | grep -qE '0\.0\.0\.0|\[::\]'; then
    fail "internal ports are loopback-only"
  else
    pass "internal ports are loopback-only"
  fi
fi

if [[ "$failures" -ne 0 ]]; then
  printf '%d deployment check(s) failed.\n' "$failures" >&2
  exit 1
fi
printf 'All production checks passed.\n'
