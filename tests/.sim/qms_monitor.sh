#!/usr/bin/env bash
# =============================================================================
# qms_monitor.sh — Live dashboard for QMS task queue + KEDA scaling
#
# Connects directly to PostgreSQL (via port-forward) and K8s cluster.
# Run alongside qms_demo.py to see state changes in real-time.
#
# Prerequisites:
#   kubectl port-forward -n p8 svc/p8-postgres-rw 5488:5432 &
#   # or docker-compose up (uses port 5488 for local dev)
#
# Usage:
#   ./tests/.sim/qms_monitor.sh
#   ./tests/.sim/qms_monitor.sh --interval 3
#   ./tests/.sim/qms_monitor.sh --no-k8s          # skip kubectl calls
# =============================================================================

set -euo pipefail

# ── load .env ──
ENV_FILE="$(cd "$(dirname "$0")/../.." && pwd)/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source <(grep -v '^#' "$ENV_FILE" | grep '=')
  set +a
fi

# ── config ──
DB_URL="${P8_DATABASE_URL:-postgresql://p8:p8_dev@localhost:5488/p8}"
CTX="${KUBE_CONTEXT:-p8-w-1}"
NS="${KUBE_NAMESPACE:-p8}"
INTERVAL=2
USE_K8S=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --interval)  INTERVAL="$2"; shift 2 ;;
    --context)   CTX="$2"; shift 2 ;;
    --no-k8s)    USE_K8S=false; shift ;;
    *) echo "Unknown: $1"; exit 1 ;;
  esac
done

# Parse DB_URL for psql
# postgresql://user:pass@host:port/dbname
DB_HOST=$(echo "$DB_URL" | sed -E 's|.*@([^:]+):.*|\1|')
DB_PORT=$(echo "$DB_URL" | sed -E 's|.*:([0-9]+)/.*|\1|')
DB_NAME=$(echo "$DB_URL" | sed -E 's|.*/([^?]+).*|\1|')
DB_USER=$(echo "$DB_URL" | sed -E 's|.*://([^:]+):.*|\1|')
DB_PASS=$(echo "$DB_URL" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')

export PGPASSWORD="$DB_PASS"
export PGSSLMODE="disable"
PSQL="psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -t -A -q"
KC="kubectl --context=$CTX -n $NS"

# ── colors ──
BOLD='\033[1m'
DIM='\033[2m'
CYAN='\033[36m'
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
MAGENTA='\033[35m'
WHITE='\033[37m'
RESET='\033[0m'

header() { echo -e "\n${BOLD}${CYAN}  $1${RESET}"; echo -e "  ${DIM}$(printf '%.0s─' {1..56})${RESET}"; }

# ── verify DB connectivity ──
if ! $PSQL -c "SELECT 1" >/dev/null 2>&1; then
  echo -e "${RED}Cannot connect to PostgreSQL at $DB_HOST:$DB_PORT/$DB_NAME${RESET}"
  echo -e "${DIM}Start port-forward:  kubectl --context=$CTX -n $NS port-forward svc/p8-postgres-rw 5488:5432 &${RESET}"
  exit 1
fi

while true; do
  clear
  NOW=$(date '+%H:%M:%S')
  echo -e "${BOLD}${MAGENTA}  ╔══════════════════════════════════════════════════════╗${RESET}"
  echo -e "${BOLD}${MAGENTA}  ║         QMS Live Monitor   ${WHITE}$NOW${MAGENTA}                ║${RESET}"
  echo -e "${BOLD}${MAGENTA}  ╚══════════════════════════════════════════════════════╝${RESET}"

  # ── 1. Queue Summary ──
  header "TASK QUEUE"
  printf "  ${BOLD}%-10s %-14s %5s${RESET}\n" "TIER" "STATUS" "COUNT"
  $PSQL -c "
    SELECT tier, status, COUNT(*)
    FROM task_queue
    GROUP BY tier, status
    ORDER BY tier, CASE status
      WHEN 'pending' THEN 1 WHEN 'processing' THEN 2
      WHEN 'completed' THEN 3 WHEN 'failed' THEN 4 END;
  " 2>/dev/null | while IFS='|' read -r tier status count; do
    [[ -z "$tier" ]] && continue
    case "$status" in
      pending)    color=$YELLOW ;;
      processing) color=$CYAN ;;
      completed)  color=$GREEN ;;
      failed)     color=$RED ;;
      *)          color=$RESET ;;
    esac
    printf "  %-10s ${color}%-14s${RESET} %5s\n" "$tier" "$status" "$count"
  done
  TOTAL=$($PSQL -c "SELECT COUNT(*) FROM task_queue" 2>/dev/null | xargs)
  PENDING=$($PSQL -c "SELECT COUNT(*) FROM task_queue WHERE status='pending'" 2>/dev/null | xargs)
  echo -e "  ${DIM}total=$TOTAL  pending=$PENDING${RESET}"

  # ── 2. Recent Activity (last 6 tasks by activity) ──
  header "RECENT ACTIVITY"
  printf "  ${BOLD}%-8s %-11s %-12s %-6s %-24s${RESET}\n" "ID" "TYPE" "STATUS" "TIER" "INFO"
  $PSQL -c "
    SELECT
      LEFT(id::text, 8),
      task_type,
      status,
      tier,
      COALESCE(
        CASE WHEN status='completed' THEN LEFT(result::text, 28)
             WHEN error IS NOT NULL THEN LEFT(error, 28)
             ELSE '' END,
        ''
      )
    FROM task_queue
    ORDER BY GREATEST(completed_at, claimed_at, created_at) DESC NULLS LAST
    LIMIT 6;
  " 2>/dev/null | while IFS='|' read -r id ttype status tier info; do
    [[ -z "$id" ]] && continue
    case "$status" in
      pending)    color=$YELLOW ;;
      processing) color=$CYAN ;;
      completed)  color=$GREEN ;;
      failed)     color=$RED ;;
      *)          color=$RESET ;;
    esac
    printf "  %-8s %-11s ${color}%-12s${RESET} %-6s %-24s\n" "$id" "$ttype" "$status" "$tier" "$info"
  done

  if [[ "$USE_K8S" == "true" ]]; then
    # ── 3. Worker Pods ──
    header "WORKER PODS"
    PODS=$($KC get pods -l app.kubernetes.io/component=worker --no-headers 2>/dev/null || true)
    if [[ -z "$PODS" ]]; then
      echo -e "  ${DIM}(no worker pods — KEDA scaled to 0)${RESET}"
    else
      printf "  ${BOLD}%-38s %-7s %-14s %s${RESET}\n" "POD" "READY" "STATUS" "AGE"
      echo "$PODS" | while read -r name ready status restarts age _; do
        case "$status" in
          Running)            color=$GREEN ;;
          ContainerCreating)  color=$YELLOW ;;
          Terminating)        color=$RED ;;
          Pending)            color=$YELLOW ;;
          *)                  color=$RESET ;;
        esac
        printf "  %-38s %-7s ${color}%-14s${RESET} %s\n" "$name" "$ready" "$status" "$age"
      done
    fi

    # ── 4. KEDA ScaledObjects ──
    header "KEDA SCALING"
    SO=$($KC get scaledobjects --no-headers 2>/dev/null || true)
    if [[ -n "$SO" ]]; then
      printf "  ${BOLD}%-32s %-5s %-5s %-8s %-6s${RESET}\n" "SCALEDOBJECT" "MIN" "MAX" "ACTIVE" "READY"
      echo "$SO" | while read -r name scaletarget min max triggers auth ready active fallback age _; do
        if [[ "$active" == "True" ]]; then acolor=$GREEN; else acolor=$DIM; fi
        if [[ "$ready" == "True" ]]; then rcolor=$GREEN; else rcolor=$YELLOW; fi
        printf "  %-32s %-5s %-5s ${acolor}%-8s${RESET} ${rcolor}%-6s${RESET}\n" "$name" "$min" "$max" "$active" "$ready"
      done
    else
      echo -e "  ${DIM}(no ScaledObjects found)${RESET}"
    fi

    # ── 5. Replica Counts ──
    header "DEPLOYMENT REPLICAS"
    printf "  ${BOLD}%-28s %-10s %-10s${RESET}\n" "DEPLOYMENT" "READY" "REPLICAS"
    $KC get deployments -l app.kubernetes.io/component=worker --no-headers 2>/dev/null | while read -r name ready uptodate available age _; do
      if [[ "$ready" == "0/0" ]]; then color=$DIM; elif [[ "$ready" =~ ^[1-9] ]]; then color=$GREEN; else color=$YELLOW; fi
      printf "  %-28s ${color}%-10s${RESET} %-10s\n" "$name" "$ready" "$uptodate"
    done
    # Also show API
    $KC get deployment p8-api --no-headers 2>/dev/null | while read -r name ready uptodate available age _; do
      printf "  %-28s ${GREEN}%-10s${RESET} %-10s\n" "$name" "$ready" "$uptodate"
    done
  fi

  # ── 6. pg_cron Status ──
  header "pg_cron JOBS"
  HAS_CRON=$($PSQL -c "SELECT COUNT(*) FROM cron.job" 2>/dev/null || echo "")
  if [[ "$HAS_CRON" != "0" && -n "$HAS_CRON" ]]; then
    printf "  ${BOLD}%-25s %-14s %-20s${RESET}\n" "JOB" "SCHEDULE" "LAST RUN"
    $PSQL -c "
      SELECT j.jobname, j.schedule,
             COALESCE(TO_CHAR(MAX(r.end_time), 'HH24:MI:SS DD Mon'), 'never')
      FROM cron.job j
      LEFT JOIN cron.job_run_details r ON r.jobid = j.jobid
      GROUP BY j.jobname, j.schedule
      ORDER BY j.jobname;
    " 2>/dev/null | while IFS='|' read -r name sched lastrun; do
      [[ -z "$name" ]] && continue
      printf "  %-25s %-14s %-20s\n" "$name" "$sched" "$lastrun"
    done
  else
    echo -e "  ${DIM}(pg_cron not installed or no jobs)${RESET}"
  fi

  echo -e "\n  ${DIM}Refreshing every ${INTERVAL}s — Ctrl+C to stop${RESET}"
  sleep "$INTERVAL"
done
