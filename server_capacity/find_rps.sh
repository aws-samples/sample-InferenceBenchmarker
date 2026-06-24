#!/usr/bin/env bash
# find_rps.sh — run a Locust wave at a target RPS.
#
# Usage:
#   ./find_rps.sh \
#     --factories-file   factories/sagemakerai_realtime/factories_cnn.py \
#     --endpoint-config  server_capacity/server_metrics_configs/sagemakerai_realtime.json \
#     --client-rps      10 \
#     --obs-time        30 \
#     --workers         5 \
#     --port            5557 \
#     --success-threshold 0.95    # min acceptable success rate (default 0.95)
#
# aiperf (optional):
#   --aiperf       run the wave, pause for confirmation, then run aiperf profile
#   --aiperf-only  skip the wave, run aiperf profile directly
#   both require --url and --api-key; --aiperf-args '{"key":"value"}' overrides/adds aiperf flags
#
# Output files under .tmp/<timestamp>_benchmark/:
#   find_rps.log                      — find_rps summary + CloudWatch metrics
#   locust_logs/primary.log           — raw locust primary output
#   locust_logs/worker_<n>.log        — raw locust worker output per worker
#   locust_stats/locust_*.csv         — locust CSV stats
#   requests_fired/worker_<n>.txt     — per-worker request counts
#   aiperf/                           — aiperf artifacts + generated input.jsonl

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
ENDPOINT_CONFIG=""
FACTORIES_FILE=""
CLIENT_RPS=10
OBS_TIME=0
NUM_REQUESTS=0
WORKERS=1
PORT=5557
LOCUST_FILE=""
POSTPROCESS=1
PARENT_DIR=""
SUCCESS_THRESHOLD=0.95
DEBUG=0
SAMPLE_HW=0
AIPERF=0
AIPERF_ONLY=0
URL=""
API_KEY=""
AIPERF_ARGS=""
PLOT=0
PLOT_DIRS=()
PLOT_OUTPUT_DIR=""
PLOT_FIELDS=""
PLOT_METADATA=""
THEME="light"

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --endpoint-config)   ENDPOINT_CONFIG="$2";   shift 2 ;;
        --factories-file)    FACTORIES_FILE="$2";    shift 2 ;;
        --client-rps)        CLIENT_RPS="$2";        shift 2 ;;
        --obs-time)          OBS_TIME="$2";          shift 2 ;;
        --num-requests)      NUM_REQUESTS="$2";      shift 2 ;;
        --workers)           WORKERS="$2";           shift 2 ;;
        --port)              PORT="$2";              shift 2 ;;
        --locust-file)       LOCUST_FILE="$2";       shift 2 ;;
        --no-postprocess)    POSTPROCESS=0;          shift 1 ;;
        --parent-dir)        PARENT_DIR="$2";        shift 2 ;;
        --success-threshold) SUCCESS_THRESHOLD="$2"; shift 2 ;;
        --debug)             DEBUG=1;                shift 1 ;;
        --sample-client-hw)  SAMPLE_HW=1;            shift 1 ;;
        --aiperf)            AIPERF=1;               shift 1 ;;
        --aiperf-only)       AIPERF_ONLY=1;          shift 1 ;;
        --url)               URL="$2";               shift 2 ;;
        --api-key)           API_KEY="$2";           shift 2 ;;
        --aiperf-args)       AIPERF_ARGS="$2";       shift 2 ;;
        --plot)
            PLOT=1; shift 1
            # consume following args as run dirs until the next --flag; accept
            # comma-separated forms too ("d1, d2" or "d1,d2")
            while [[ $# -gt 0 && "$1" != --* ]]; do
                IFS=',' read -ra _parts <<< "$1"
                for _p in "${_parts[@]}"; do
                    _p="${_p#"${_p%%[![:space:]]*}"}"; _p="${_p%"${_p##*[![:space:]]}"}"  # trim
                    [[ -n "$_p" ]] && PLOT_DIRS+=("$_p")
                done
                shift 1
            done ;;
        --plot-output-dir)   PLOT_OUTPUT_DIR="$2";   shift 2 ;;
        --plot-fields)       PLOT_FIELDS="$2";       shift 2 ;;
        --plot-metadata)     PLOT_METADATA="$2";     shift 2 ;;
        --theme)             THEME="$2";             shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Plot mode — visualize existing run dirs and exit (no wave, no aiperf)
# ---------------------------------------------------------------------------
if [[ "$PLOT" -eq 1 ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    ROOT_DIR="$(dirname "$SCRIPT_DIR")"
    if [[ ${#PLOT_DIRS[@]} -eq 0 ]]; then
        echo "Error: --plot needs at least one run dir, e.g. --plot dir1 dir2"; exit 1
    fi
    PYTHONPATH="${ROOT_DIR}/visualization:${PYTHONPATH:-}" \
    python3 "${ROOT_DIR}/visualization/plot.py" \
        "$PLOT_OUTPUT_DIR" "$THEME" "$PLOT_FIELDS" "$PLOT_METADATA" "${PLOT_DIRS[@]}"
    exit $?
fi

# endpoint-config is optional — server metrics skipped when absent

# --factories-file is required for the wave unless a self-contained --locust-file is
# provided. Skipped for --aiperf-only (no wave runs).
if [[ "$AIPERF_ONLY" -ne 1 && -z "$FACTORIES_FILE" && -z "$LOCUST_FILE" ]]; then
    echo "Error: --factories-file is required (or pass a self-contained --locust-file)"; exit 1
fi

# --aiperf / --aiperf-only require --url and --api-key
if [[ "$AIPERF" -eq 1 || "$AIPERF_ONLY" -eq 1 ]]; then
    if [[ -z "$URL" || -z "$API_KEY" ]]; then
        echo "Error: --url and --api-key are required with --aiperf / --aiperf-only"; exit 1
    fi
fi

_dbg() { [[ "$DEBUG" -eq 1 ]] && echo "[$(date '+%H:%M:%S')] [debug] $*" || true; }
_ts()  { [[ "$DEBUG" -eq 1 ]] && echo "[$(date '+%H:%M:%S')] [debug] $*" || true; }

# ---------------------------------------------------------------------------
# Derived values
# ---------------------------------------------------------------------------
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# ---------------------------------------------------------------------------
# Derive LOCUST_USERS, RUN_TIME, and LOCUST_TOTAL_REQUESTS from mode:
#   obs_time only:    users = client_rps * obs_time, run-time = obs_time
#   num_requests only: users = num_requests, no run-time (stops via LOCUST_TOTAL_REQUESTS)
#   both:             users = num_requests, run-time = obs_time (whichever hits first)
# ---------------------------------------------------------------------------
if [[ "$NUM_REQUESTS" -gt 0 && "$OBS_TIME" -gt 0 ]]; then
    LOCUST_USERS="$NUM_REQUESTS"
    RUN_TIME="$OBS_TIME"
elif [[ "$NUM_REQUESTS" -gt 0 ]]; then
    LOCUST_USERS="$NUM_REQUESTS"
    RUN_TIME=""
elif [[ "$OBS_TIME" -gt 0 ]]; then
    # client_rps may be fractional (e.g. 0.1), so compute with python and ceil to a whole user count
    LOCUST_USERS=$(python3 -c "import math; print(math.ceil($CLIENT_RPS * $OBS_TIME))")
    RUN_TIME="$OBS_TIME"
else
    echo "Error: at least one of --obs-time or --num-requests is required"; exit 1
fi

_dbg "SCRIPT_DIR=$SCRIPT_DIR"
_dbg "CLIENT_RPS=$CLIENT_RPS OBS_TIME=$OBS_TIME NUM_REQUESTS=$NUM_REQUESTS WORKERS=$WORKERS PORT=$PORT"
_dbg "LOCUST_USERS=$LOCUST_USERS RUN_TIME=${RUN_TIME:-none}"

if [[ -n "$PARENT_DIR" ]]; then
    RUN_DIR="$PARENT_DIR"
    REL_RUN_DIR="${PARENT_DIR#${SCRIPT_DIR}/}"
else
    REL_RUN_DIR=".tmp/${TIMESTAMP}_benchmark"
    RUN_DIR="${ROOT_DIR}/${REL_RUN_DIR}"
fi

REQUESTS_FIRED_DIR="${RUN_DIR}/requests_fired"
LOCUST_LOGS_DIR="${RUN_DIR}/locust_logs"
LOCUST_STATS_DIR="${RUN_DIR}/locust_stats"
CSV_PREFIX="${LOCUST_STATS_DIR}/locust"
FIND_RPS_LOG="${RUN_DIR}/find_rps.log"
if [[ -n "$LOCUST_FILE" ]]; then
    LOCUST_USER_SCRIPT="$LOCUST_FILE"
elif [[ "$NUM_REQUESTS" -gt 0 ]]; then
    LOCUST_USER_SCRIPT="${ROOT_DIR}/locust_scripts/locust_user_num_requests.py"
else
    LOCUST_USER_SCRIPT="${ROOT_DIR}/locust_scripts/locust_user.py"
fi
FACTORIES_PKL="${RUN_DIR}/factories.pkl"

_dbg "RUN_DIR=$RUN_DIR"
_dbg "LOCUST_USER_SCRIPT=$LOCUST_USER_SCRIPT"

mkdir -p "$RUN_DIR" "$REQUESTS_FIRED_DIR" "$LOCUST_LOGS_DIR" "$LOCUST_STATS_DIR"

# ---------------------------------------------------------------------------
# Serialize factories to pkl — only for the wave (skipped for --aiperf-only),
# and only when --factories-file provided.
# ---------------------------------------------------------------------------
if [[ "$AIPERF_ONLY" -ne 1 && -n "$FACTORIES_FILE" ]]; then
    _dbg "Serializing factories from $FACTORIES_FILE to $FACTORIES_PKL"
    PYTHONPATH="${ROOT_DIR}/..:${PYTHONPATH:-}" \
    python3 "${SCRIPT_DIR}/_find_rps_serialize.py" "$FACTORIES_FILE" "$FACTORIES_PKL"
    export FACTORIES_PATH="$FACTORIES_PKL"
    _dbg "Factories serialized: $FACTORIES_PKL"
fi

# ---------------------------------------------------------------------------
# Print + log header
# ---------------------------------------------------------------------------
header() {
cat <<EOF
================================================================================
InferenceBenchmarker$([[ "$DEBUG" -eq 1 ]] && echo " [debug] [$(date '+%H:%M:%S')]" || true)
================================================================================
${ENDPOINT_CONFIG:+   Endpoint config:    $ENDPOINT_CONFIG
}${FACTORIES_FILE:+   Factories file:     $FACTORIES_FILE
}   Client RPS:         $CLIENT_RPS req/s
${OBS_TIME:+   Observation time:   ${OBS_TIME}s
}${NUM_REQUESTS:+   Num requests:       $NUM_REQUESTS
}   Users:              $LOCUST_USERS
   Workers:            $WORKERS
   Results dir:        $REL_RUN_DIR
--------------------------------------------------------------------------------
EOF
}
header | tee "$FIND_RPS_LOG"

# ---------------------------------------------------------------------------
# Export env vars read by locust_user.py
# ---------------------------------------------------------------------------
export LOCUST_WAVE_DIR="$REQUESTS_FIRED_DIR"
export PYTHONPATH="${ROOT_DIR}/..:${PYTHONPATH:-}"
export BENCHMARKER_SAMPLE_HW="$SAMPLE_HW"
export LOCUST_TOTAL_REQUESTS="$NUM_REQUESTS"

# ===========================================================================
# WAVE — run find_rps (skipped entirely for --aiperf-only)
# ===========================================================================
if [[ "$AIPERF_ONLY" -ne 1 ]]; then

# ---------------------------------------------------------------------------
# Launch primary
# ---------------------------------------------------------------------------
_dbg "Launching primary (pid will follow)..."
LOCUST_CMD=(
    locust -f "$LOCUST_USER_SCRIPT"
    --master --headless
    --users          "$LOCUST_USERS"
    --spawn-rate     "$CLIENT_RPS"
    --stop-timeout   0
    --expect-workers "$WORKERS"
    --master-bind-port "$PORT"
    --csv            "$CSV_PREFIX"
)
[[ -n "$RUN_TIME" ]] && LOCUST_CMD+=(--run-time "${RUN_TIME}s")

"${LOCUST_CMD[@]}" > "${LOCUST_LOGS_DIR}/primary.log" 2>&1 &

PRIMARY_PID=$!
_dbg "Primary pid=$PRIMARY_PID — sleeping 2s for port bind..."
sleep 2

# ---------------------------------------------------------------------------
# Launch workers — each gets its own log and worker index
# ---------------------------------------------------------------------------
WORKER_PIDS=()
for i in $(seq 1 "$WORKERS"); do
    WORKER_INDEX="$i" locust -f "$LOCUST_USER_SCRIPT" \
        --worker \
        --master-port "$PORT" \
        > "${LOCUST_LOGS_DIR}/worker_${i}.log" 2>&1 &
    WORKER_PIDS+=($!)
    _dbg "Worker $i pid=${WORKER_PIDS[-1]} launched"
done

# ---------------------------------------------------------------------------
# Wait for primary to finish
# ---------------------------------------------------------------------------
_ts "Waiting for primary (pid=$PRIMARY_PID) to finish..."
wait "$PRIMARY_PID" || true
_ts "Primary finished"

_ts "Stopping workers..."
for pid in "${WORKER_PIDS[@]}"; do
    if [[ "$DEBUG" -eq 1 ]]; then
        kill "$pid" && _dbg "Sent SIGTERM to worker pid=$pid" || _dbg "Worker pid=$pid already exited"
    else
        kill "$pid" 2>/dev/null || true
    fi
done
if [[ "$DEBUG" -eq 1 ]]; then
    wait "${WORKER_PIDS[@]}" && _ts "All workers exited cleanly" || _ts "Some workers exited with non-zero status"
else
    wait "${WORKER_PIDS[@]}" 2>/dev/null || true
fi
_ts "Workers stopped"


# cleanup pkl
_dbg "Cleaning up factories pkl..."
[[ -n "$FACTORIES_FILE" ]] && rm -f "$FACTORIES_PKL"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
TOTAL_FIRED=0
for f in "$REQUESTS_FIRED_DIR"/worker_*.txt; do
    [[ -f "$f" ]] && TOTAL_FIRED=$(( TOTAL_FIRED + $(cat "$f") ))
done

{
echo ""
echo "RESULTS – InferenceBenchmarker"
echo "------------------------------"
echo "   Total requests fired: $TOTAL_FIRED"
if [[ "$SAMPLE_HW" -eq 1 ]]; then
    grep "Client Hardware\|Client CPU\|Client Memory" "${LOCUST_LOGS_DIR}/primary.log" 2>/dev/null || true
fi
} | tee -a "$FIND_RPS_LOG"

# Flag a client-side bottleneck if the locust logs show CPU/heartbeat warnings
python3 "${SCRIPT_DIR}/detect_locust_heat.py" \
    "$RUN_DIR" \
    2>&1 | tee -a "$FIND_RPS_LOG"

# ---------------------------------------------------------------------------
# Post-processing: CSV stats (always) + server metrics (if --endpoint-config set)
# ---------------------------------------------------------------------------
if [[ "$POSTPROCESS" -eq 1 ]]; then
    _dbg "Running postprocessor..."
    python3 "${SCRIPT_DIR}/find_rps_postprocess.py" \
        "$RUN_DIR" "$SUCCESS_THRESHOLD" \
        2>&1 | tee -a "$FIND_RPS_LOG"

    # server metrics use the actual load window (start/last-request epochs) from wave_window.txt
    WAVE_WINDOW=$(cat "${RUN_DIR}/wave_window.txt" 2>/dev/null || echo "")
    if [[ -n "$ENDPOINT_CONFIG" && "$WAVE_WINDOW" != "WARN" && -n "$WAVE_WINDOW" ]]; then
        python3 "${SCRIPT_DIR}/fetch_server_metrics.py" \
            "$ENDPOINT_CONFIG" $WAVE_WINDOW \
            2>&1 | tee -a "$FIND_RPS_LOG"
    fi
    _dbg "Postprocessor done"
fi

fi  # end WAVE (AIPERF_ONLY guard)

# ===========================================================================
# AIPERF — run aiperf profile and fetch server metrics for its window
#   --aiperf:      pause for confirmation after the wave, then run
#   --aiperf-only: run directly (no wave above)
# ===========================================================================
if [[ "$AIPERF" -eq 1 || "$AIPERF_ONLY" -eq 1 ]]; then
    if [[ "$AIPERF" -eq 1 ]]; then
        read -r -p "Press return to proceed to aiperf after server has reached acceptable baseline"
    fi

    python3 "${SCRIPT_DIR}/run_aiperf.py" \
        "$FACTORIES_FILE" "$CLIENT_RPS" "$OBS_TIME" "$NUM_REQUESTS" \
        "$URL" "$API_KEY" "$RUN_DIR" "$ENDPOINT_CONFIG" "$AIPERF_ARGS" "$SUCCESS_THRESHOLD" \
        2>&1 | tee -a "$FIND_RPS_LOG"
fi
