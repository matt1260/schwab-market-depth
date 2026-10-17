#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# External feed watchdog for the standalone scalper.
#
# WHY: the in-process feed watchdog (schwab_feed.py) rebuilds the stream on
# staleness, but when Schwab drops the socket in a bad way (1011 / closed with no
# frame) the stop()/start() awaits can HANG, starving the asyncio event loop —
# the feed goes stale AND the API stops responding (observed 3x in one session).
# An in-process watchdog can't recover a hung loop. This EXTERNAL process polls
# the API and HARD-restarts the server when it's unresponsive or the feed is
# stale, so the scalper self-heals instead of sitting dead.
#
# It only restarts when a restart is SAFE-ish: it will restart on API-hang or
# stale-feed regardless, but it refuses to thrash — min 90s between restarts, and
# it backs off if restarts don't help. Rehydration re-tracks any open position.
#
# USAGE (run alongside the server, backgrounded):
#   cd schwab-market-depth
#   nohup ./feed_watchdog.sh > /private/tmp/scalper_watchdog.log 2>&1 &
# ─────────────────────────────────────────────────────────────────────────────
set -u

DIR="/Users/spearhead/python projects/SwarmTrader/schwab-market-depth"
API="http://localhost:8080/scalper/status"
LOG="/private/tmp/scalper_server.log"
STALE_MAX=180          # feed age (s) over this = wedged → restart
API_TIMEOUT=8          # curl timeout (s); no response = hung
POLL=30                # seconds between checks
MIN_RESTART_GAP=90     # never restart more often than this
FAILS_TO_RESTART=2     # require this many CONSECUTIVE bad checks (status endpoint
                       # occasionally returns malformed JSON — don't false-restart)
last_restart=0
fails=0

log(){ echo "$(date '+%H:%M:%S') $*"; }

restart_server(){
  now=$(date +%s)
  if [ $((now - last_restart)) -lt "$MIN_RESTART_GAP" ]; then
    log "restart suppressed (last was $((now - last_restart))s ago < ${MIN_RESTART_GAP}s)"
    return
  fi
  last_restart=$now
  log "RESTARTING server ($1)"
  PID=$(lsof -nP -iTCP:8080 -sTCP:LISTEN -t 2>/dev/null)
  [ -n "$PID" ] && kill -9 $PID 2>/dev/null
  sleep 3
  cd "$DIR" || return
  # shellcheck disable=SC1091
  source .venv/bin/activate 2>/dev/null
  nohup python api.py > "$LOG" 2>&1 &
  log "relaunched pid $!; waiting for it to come up..."
  sleep 20
}

log "watchdog started (poll=${POLL}s stale_max=${STALE_MAX}s)"
while true; do
  problem=""
  # 1) API reachable? capture status JSON (empty = hung/unreachable)
  body=$(curl -s --max-time "$API_TIMEOUT" "$API" 2>/dev/null)
  if [ -z "$body" ]; then
    problem="API hung (no body in ${API_TIMEOUT}s)"
  else
    # 2) parse feed age from signals.*.quote_age_s (min). age=-1 → couldn't read
    #    (malformed JSON, tolerated); age=-2 → parsed but no age yet (feed warming).
    read -r age enabled state <<<"$(printf '%s' "$body" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    sig=d.get('signals') or {}
    cand=[s.get('quote_age_s') for s in sig.values() if isinstance(s,dict) and s.get('quote_age_s') is not None]
    a=min(cand) if cand else -2
    print(a, d.get('enabled'), d.get('entry_state'))
except Exception:
    print(-1, '?', '?')
" 2>/dev/null)"
    if [ "${age:-}" = "-1" ] || [ -z "${age:-}" ]; then
      problem="status JSON unparseable"
    else
      ai=${age%.*}
      if [ "$age" != "-2" ] && [ "${ai:-0}" -gt "$STALE_MAX" ] 2>/dev/null; then
        problem="feed stale ${age}s (enabled=$enabled state=$state)"
      fi
    fi
  fi

  # 3) escalate only after N CONSECUTIVE problems (avoids false-restart on a
  #    single malformed-JSON blip, which happens intermittently)
  if [ -n "$problem" ]; then
    fails=$((fails + 1))
    log "problem ($fails/$FAILS_TO_RESTART): $problem"
    if [ "$fails" -ge "$FAILS_TO_RESTART" ]; then
      restart_server "$problem"
      fails=0
    fi
  else
    [ "$fails" -gt 0 ] && log "recovered (was $fails)"
    fails=0
  fi

  sleep "$POLL"
done
