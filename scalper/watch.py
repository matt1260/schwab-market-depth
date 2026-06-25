#!/usr/bin/env python3
"""Quick live-status reader for the standalone scalper (writes clean summary to a
file the agent reads, bypassing shell stdout mangling). Usage: python -m scalper.watch"""
import json, urllib.request, sys

def main():
    try:
        with urllib.request.urlopen("http://localhost:8080/scalper/status", timeout=8) as r:
            d = json.load(r)
    except Exception as e:
        print("ERR:", e); return
    out = []
    out.append(f"enabled={d.get('enabled')} mode={d.get('signal_mode')} ai_mode={d.get('ai_mode')} ai_err={d.get('ai_error')} opts={d.get('options_quote_source')} feed={d.get('feed_running')}")
    out.append(f"pnl={d.get('day_pnl')} peak={d.get('peak_pnl')} entries={d.get('entries_today')} exits={d.get('exits_today')} wins={d.get('wins_today')} open={d.get('open_scalps')} wr={d.get('win_rate')} kill={d.get('kill_switch_tripped')} lock={d.get('profit_lock_tripped')}")
    t = d.get('tunables', {})
    out.append("tunables: " + " ".join(f"{k}={t.get(k)}" for k in ('entry_threshold','stop_pct','profit_target_pct','time_stop_seconds','chop_min_efficiency','min_recent_range_pct')))
    for s, v in (d.get('signals') or {}).items():
        out.append(f"  {s}: last={v.get('last')} age={v.get('quote_age_s')}s ER={_r(v.get('efficiency_ratio'))} rng%={_r(v.get('recent_range_pct'))} z={_r(v.get('vwap_z'))} mtf={v.get('mtf')} book={_r(v.get('book_imbalance'))}")
    out.append("--- AI feed ---")
    for e in (d.get('ai_log') or [])[:5]:
        out.append(f"  [{e.get('cycle')}] {e.get('mode')} {(e.get('timestamp') or '')[11:19]} chg={e.get('param_changes')} :: {(e.get('reasoning') or e.get('assessment') or '')[:160]}")
    open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/scalp_watch.txt", "w").write("\n".join(out))
    print("\n".join(out))

def _r(x):
    return round(x, 3) if isinstance(x, float) else x

if __name__ == "__main__":
    main()
