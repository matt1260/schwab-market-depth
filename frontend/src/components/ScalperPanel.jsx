// Standalone scalper control panel — status, mode, toggle, live signals, AI feed.
import { useState, useEffect, useCallback } from 'react';
import CandleChart from './CandleChart';
import ConfirmCard from './ConfirmCard';
import { OptionsLadder } from './PositionsPanel';
import PnlMeter from './PnlMeter';

export default function ScalperPanel({ apiBaseUrl }) {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const [trades, setTrades] = useState([]);
  const [stratFilter, setStratFilter] = useState('all');   // trades-table strategy filter

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${apiBaseUrl}/scalper/status`);
      const d = await r.json();
      setSt(d); setErr(null);
    } catch (e) { setErr(e.message); }
  }, [apiBaseUrl]);

  const refreshTrades = useCallback(async () => {
    try {
      const r = await fetch(`${apiBaseUrl}/scalper/trades?limit=50`);
      setTrades((await r.json()).trades || []);
    } catch { /* ignore */ }
  }, [apiBaseUrl]);

  useEffect(() => {
    refresh(); refreshTrades();
    const id = setInterval(() => { refresh(); refreshTrades(); }, 3000);
    return () => clearInterval(id);
  }, [refresh, refreshTrades]);

  const post = async (path, body) => {
    await fetch(`${apiBaseUrl}${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    refresh();
  };

  const toggleLive = async () => {
    if (!st.live_trading) {
      // arming LIVE — real money: require explicit typed confirmation
      const ok = window.confirm(
        '⚠️ SWITCH TO LIVE — REAL MONEY?\n\nOrders will route to your funded Alpaca LIVE account. ' +
        'This is a thin, still-being-validated strategy. Click OK only if you intend to trade real money.'
      );
      if (!ok) return;
      const res = await fetch(`${apiBaseUrl}/scalper/live`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ live: true }),
      });
      if (!res.ok) { const e = await res.json().catch(() => ({})); alert('Live switch refused: ' + (e.detail || res.status)); }
      else { const d = await res.json(); alert(`LIVE active. Live account equity: $${d.live_equity ?? '?'}`); }
    } else {
      await fetch(`${apiBaseUrl}/scalper/live`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ live: false }),
      });
    }
    refresh();
  };

  if (err) return <div className="glass p-6 rounded-xl text-down">Scalper API error: {err}</div>;
  if (!st) return <div className="text-muted p-6">Loading scalper…</div>;
  if (!st.available) return <div className="glass p-6 rounded-xl text-warn">Scalper not initialized — check the API server logs (Schwab re-auth may be required).</div>;

  const pnlColor = (st.day_pnl ?? 0) >= 0 ? 'text-up' : 'text-down';

  // ── signal-mode model (two tiers: AI-chooses vs. run-one-manually) ─────────
  // Each entry: value sent to the API, short label, and an info blurb (info icon).
  const MODE_INFO = {
    ai:        { label: 'AI',        desc: 'The AI supervisor reads the live tape every cycle and decides how to trade: momentum, fade, or stand aside (pause). On a clean, sustained trend it can also switch ON the mechanical MA-cross to run alongside it. This is the "let the AI drive" mode.' },
    all:       { label: 'All (A/B)', desc: 'Runs the survivors at once — APEX, X, Bull Pivot, and the AI — each holding its own separate positions, so you can compare their P&L head-to-head. (9/21/50 was retired for poor performance.)' },
    bullpivot: { label: 'Bull Pivot', desc: 'CALLS ONLY, traded against 5-minute pivot support levels. A Bull Pivot / Pivot 2 bar (a bullish reversal of a 5m downtrend) sets a SUPPORT level at its low. Buys calls on a pullback toward that support, with a HARD STOP if price breaks below it, and holds until the next pivot resets the level. Exempt from the chop gate (it is a reversal play by design). Ported from your ThinkScript.' },
    apex:      { label: 'APEX', desc: 'The synthesis strategy — every filter that won across 2 weeks of live A/B, stacked for maximum selectivity and a high win rate. ENTRY needs ALL of: (1) a fresh 8/21 EMA cross [best-price timing], (2) L2 order-book/wall confirmation [the biggest edge], (3) the dominant trend agreeing [never fight it], (4) efficiency above a strict floor [no chop], (5) not over-extended from VWAP [no chasing]. EXIT: peak-giveback lock + underlying stop. Few trades, high conviction.' },
    macross:   { label: 'MA9/21/50 (retired)', desc: 'RETIRED — consistently the worst performer (-$460 all-time, ~30% win rate, stopped on half its trades). Kept selectable for reference only. 9/21/50 triple-EMA: enter when the 9 leads through both the 21 and 50; exit on the opposite 9×21 cross; stop on EMA50 break.' },
    macross_x: { label: 'MA cross X',desc: 'A stripped-down cross variant: enters ONLY on a fresh EMA cross confirmed by L2 order-flow or a liquidity wall — no other gates. Faster, more selective on confirmation. The A/B foil to the full-gate MA cross.' },
    momentum:  { label: 'Momentum',  desc: 'Legacy rule-based mode: trade WITH the move using the momentum/efficiency signals. No AI, no EMA-cross — the original mechanical scalper path.' },
    fade:      { label: 'Fade',      desc: 'Legacy rule-based mode: mean-reversion — fade extensions (buy dips / sell pops) around VWAP and levels. The counter-trend sibling of Momentum.' },
    auto:      { label: 'Auto',      desc: 'Legacy mode: automatically switches between momentum and fade based on the efficiency ratio (trend vs. range) — mechanical, no AI.' },
  };
  const MANUAL_MODES = ['apex', 'bullpivot', 'macross_x', 'macross', 'momentum', 'fade', 'auto'];  // single-strategy options
  const aiDriven = st.signal_mode === 'ai';
  const allMode = st.signal_mode === 'all';
  const manualOnly = st.signal_mode === 'manual';
  const oneStrategy = !aiDriven && !allMode && !manualOnly;   // running a single algo

  // ── per-strategy comparison + trades-table filter ─────────────────────────
  // compact labels that fit the tight Strat column. macross3 = the new 9/21/50 triple;
  // 'macross' = the RETIRED 8/21 full-gate strat (kept distinct so old trades aren't renamed).
  const stratLabel = (s) => ({
    apex: 'APEX',
    bullpivot: 'BULL',
    manual: 'MANUAL',
    macross3: '9/21/50',
    macross_x: 'X',
    ai: 'AI',
  }[s] || s || '—');
  const stratOf = (t) => t.strategy || 'macross';
  // RETIRED strategies hidden from the comparison table: the old 8/21 'macross'
  // (MA-old) and the pre-labeling NULL trades (which default to 'macross'). Only the
  // strategies actually being run today (9/21/50, X, AI) are shown.
  // hidden from the comparison: retired 8/21 'macross', retired '9/21/50' (macross3),
  // and 'rehydrated' (positions re-tracked after a restart — unattributable to a strategy).
  const HIDDEN_STRATS = new Set(['macross', 'macross3', 'rehydrated']);
  const liveTrades = trades.filter((t) => !HIDDEN_STRATS.has(stratOf(t)));
  // which strategy labels actually appear in the recent trades (for filter chips)
  const stratsPresent = Array.from(new Set(liveTrades.map(stratOf)));
  // per-strategy tallies so the user can compare the A/B strategies at a glance
  const stratStats = stratsPresent.map((s) => {
    const rows = liveTrades.filter((t) => stratOf(t) === s);
    const pnl = rows.reduce((a, t) => a + (t.pnl ?? 0), 0);
    const wins = rows.filter((t) => (t.pnl ?? 0) >= 0).length;
    return { strat: s, n: rows.length, pnl, wr: rows.length ? (wins / rows.length) * 100 : 0 };
  });
  const shownTrades = stratFilter === 'all' ? liveTrades : liveTrades.filter((t) => stratOf(t) === stratFilter);

  return (
    <div className="space-y-4">
      {/* control toolbar — engine on/off, autonomy, account (status shown in the top rail) */}
      <div className={`panel px-4 py-3 flex flex-wrap items-center gap-3 ${st.live_trading ? 'ring-1 ring-down/60' : ''}`}>
        <div className="flex items-center gap-2">
          <button
            onClick={() => post('/scalper/toggle', { enabled: !st.enabled })}
            className={`px-4 py-1.5 rounded-md font-semibold text-sm transition-colors ${
              st.enabled ? 'bg-up/15 text-up ring-1 ring-up/40' : 'bg-raised text-muted ring-1 ring-line'
            }`}
          >
            <span className={`inline-block w-1.5 h-1.5 rounded-full mr-1.5 align-middle ${st.enabled ? 'bg-up livedot' : 'bg-faint'}`} />
            Engine {st.enabled ? 'On' : 'Off'}
          </button>
        </div>
        <div className="h-5 w-px bg-line" />
        <div className="text-xs text-muted num">{st.symbols?.join(' · ')}</div>
        <div className="flex-1" />
        <button
          onClick={() => post('/scalper/autonomous', { autonomous: !st.autonomous })}
          title={st.autonomous
            ? 'Autonomous: signals fire automatically. Click to require confirmation.'
            : 'Confirm mode: you approve each entry/exit signal (risk stops still auto-fire).'}
          className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ring-1 ${
            st.autonomous ? 'bg-accent-dim text-accent ring-accent/40' : 'bg-warn/10 text-warn ring-warn/40'
          }`}
        >
          {st.autonomous ? 'Autonomous' : 'Confirm mode'}
        </button>
        <button
          onClick={toggleLive}
          disabled={!st.live_available && !st.live_trading}
          title={!st.live_available ? 'No live Alpaca keys configured' : ''}
          className={`px-3 py-1.5 rounded-md text-xs font-semibold transition-colors ring-1 disabled:opacity-40 ${
            st.live_trading ? 'bg-down text-white ring-down' : 'bg-raised text-muted ring-line hover:text-ink'
          }`}
        >
          {st.live_trading ? 'LIVE — click for paper' : st.live_available ? 'Go Live $' : 'Live (no keys)'}
        </button>
      </div>

      {/* Pending-signal confirmation cards (only appear in Confirm mode) */}
      {!st.autonomous && (
        <ConfirmCard
          apiBaseUrl={apiBaseUrl}
          confirmWindowS={st.confirm_window_s || 8}
          onAction={refresh}
        />
      )}

      {(st.kill_switch_tripped || st.profit_lock_tripped) && (
        <div className="glass p-3 rounded-lg text-sm text-warn">
          {st.kill_switch_tripped ? '⛔ Kill switch tripped — new entries halted.' : '🔒 Profit lock tripped — gains locked, entries halted.'}
        </div>
      )}

      {/* entry state — is it actually trading right now? */}
      <EntryStateBanner state={st.entry_state} reason={st.entry_reason}
                        startUtc={st.daily_entry_start_utc} endUtc={st.daily_entry_end_utc} />

      {/* ── TRADING COCKPIT — per-symbol chart + Buy Call/Put + Close, quick access ── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {(st.symbols || []).map((sym) => (
          <SymbolCockpit key={sym} apiBaseUrl={apiBaseUrl} symbol={sym}
            positions={st.open_positions || []} live={st.live_trading}
            refresh={refresh} last={(st.signals || {})[sym]?.last} />
        ))}
      </div>

      {/* options ladder — strikes the algo is currently selecting */}
      <OptionsLadder ladder={st.ladder || {}} />

      {/* mode selector — two tiers: WHO decides, then (if manual) WHICH strategy */}
      <div className="glass p-4 rounded-xl">
        <div className="eyebrow mb-2">Who's trading</div>

        {/* Tier 1: Manual only | Run all (A/B) | Run one strategy */}
        <div className="flex gap-2 flex-wrap">
          <ModeCard active={manualOnly} title="Manual only" sub="you trade — no auto strategies"
                    info="No strategy runs and nothing fires automatically. Only YOUR Buy Call / Buy Put clicks open trades (tracked as MANUAL). Open positions still auto-exit via the peak-lock/stop ladder, or Close them yourself."
                    onClick={() => post('/scalper/config', { signal_mode: 'manual' })} />
          <ModeCard active={allMode} title="Run all (A/B)" sub="every strategy, compared"
                    info={MODE_INFO.all.desc}
                    onClick={() => post('/scalper/config', { signal_mode: 'all' })} />
          <ModeCard active={oneStrategy} title="Run one strategy" sub="a single algo, auto"
                    info="Run exactly one mechanical strategy automatically — choose it below."
                    onClick={() => { if (!oneStrategy) post('/scalper/config', { signal_mode: 'macross_x' }); }} />
        </div>

        {/* Tier 2: strategy picker (only when 'Run one strategy' is selected) */}
        {oneStrategy && (
          <div className="mt-3 pt-3 border-t border-line">
            <div className="eyebrow mb-1.5">Strategy</div>
            <div className="flex gap-2 flex-wrap">
              {MANUAL_MODES.map((m) => (
                <button key={m} onClick={() => post('/scalper/config', { signal_mode: m })}
                  className={`px-3 py-1 rounded text-sm inline-flex items-center gap-1 ${st.signal_mode === m ? 'bg-accent text-black font-bold' : 'bg-raised text-ink'}`}>
                  {MODE_INFO[m].label}
                  <InfoDot text={MODE_INFO[m].desc} dark={st.signal_mode === m} />
                </button>
              ))}
            </div>
          </div>
        )}

        {['macross', 'macross_x', 'all'].includes(st.signal_mode) && (
          <div className="mt-3 space-y-3">
            <div className="text-xs text-muted">
              MA crossover · <span className="font-mono text-accent">{st.ma_fast}/{st.ma_slow}</span> EMA on {st.ma_tf_seconds}s ·
              spread (fast−slow): <span className="font-mono">{st.ma_min_separation_pct}% min to enter</span>
              {st.signal_mode === 'macross_x' && <span className="ml-1 text-warn">· X: fresh cross + L2/wall only</span>}
              {st.signal_mode === 'all' && (
                <span className="ml-1 text-warn">
                  · running {(st.active_strategies || ['macross','macross_x']).map(stratLabel).join(' + ')} independently
                </span>
              )}
            </div>
            <Collapsible title="Trading parameters" subtitle={`${st.ma_fast}/${st.ma_slow} EMA · ${st.ma_tf_seconds}s · sep ${st.ma_min_separation_pct}%`}>
              <div className="grid grid-cols-2 gap-3">
                <Stepper label="Fast EMA" val={st.ma_fast} min={3} max={st.ma_slow - 1} step={1}
                         onSet={(v) => post('/scalper/config', { ma_fast: v })} />
                <Stepper label="Slow EMA" val={st.ma_slow} min={st.ma_fast + 1} max={60} step={1}
                         onSet={(v) => post('/scalper/config', { ma_slow: v })} />
              </div>
              <div className="grid grid-cols-2 gap-3 mt-3">
                <Stepper label="Timeframe (s)" val={st.ma_tf_seconds} min={15} max={120} step={15}
                         onSet={(v) => post('/scalper/config', { ma_tf_seconds: v })} />
                <Stepper label="Min sep %" val={st.ma_min_separation_pct} min={0} max={0.10} step={0.005} dp={3}
                         onSet={(v) => post('/scalper/config', { ma_min_separation_pct: v })} />
              </div>
              <div className="text-[11px] text-muted mt-2">
                Lower fast vs slow = more crosses / faster (more whipsaw). Wider gap = fewer, cleaner signals.
                Fast {st.ma_fast} must stay below Slow {st.ma_slow}.
              </div>
            </Collapsible>
          </div>
        )}
      </div>

      {/* stats */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
        <Stat label="Day P&L" val={`$${(st.day_pnl ?? 0).toFixed(0)}`} color={pnlColor} />
        <Stat label="Win rate" val={st.win_rate != null ? `${Math.round(st.win_rate * 100)}%` : '—'} />
        <Stat label="Entries" val={st.entries_today ?? 0} />
        <Stat label="Exits" val={st.exits_today ?? 0} />
        <Stat label="Open" val={st.open_scalps ?? 0} />
      </div>

      {/* risk & timing settings (operator-only) — collapsible */}
      <Collapsible title="Risk & timing" subtitle={`$${st.per_trade_usd}/trade · kill $${st.daily_loss_kill_usd} · ${st.max_concurrent} max`}>
        <div className="grid grid-cols-2 gap-3">
          <Stepper label="Size $/trade" val={st.per_trade_usd} min={50} max={5000} step={50}
                   onSet={(v) => post('/scalper/config', { per_trade_usd: v })} />
          <Stepper label="Max concurrent" val={st.max_concurrent} min={1} max={12} step={1}
                   onSet={(v) => post('/scalper/config', { max_concurrent: v })} />
          <Stepper label="Kill switch $" val={st.daily_loss_kill_usd} min={10} max={200000} step={10}
                   onSet={(v) => post('/scalper/config', { daily_loss_kill_usd: v })} />
          <Stepper label="Wait after open (min)" val={st.open_blackout_minutes} min={0} max={60} step={1}
                   onSet={(v) => post('/scalper/config', { open_blackout_minutes: v })} />
        </div>
        <div className="text-[11px] text-muted mt-2">
          Size = premium budget per position (qty = floor($/trade ÷ option cost)). Applies to ALL strategies incl. Manual — keeps the comparison fair.
          Max concurrent caps open positions at once (budget = size × max). Kill switch halts new entries once down this much. Wait-after-open sits out the first N min after the bell.
        </div>
      </Collapsible>

      {/* manual-trade exit control — per-rule toggles so the engine only auto-exits
          the user's OWN discretionary trades the way THEY want. Unchecked = that rule
          never touches a manual trade. The algo strategies are unaffected. */}
      <Collapsible title="Manual trade exits" subtitle={manualExitSummary(st)}>
        <div className="text-[11px] text-muted mb-2">
          Which auto-exits apply to <span className="text-ink font-semibold">your manual trades</span> (Buy Call / Buy Put).
          Unchecked = that rule never closes a manual trade — you close it yourself with the Close button.
          The algo strategies (APEX / X / Bull Pivot / AI) always use their full exit ladder.
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          <ExitToggle on={st.manual_exit_failed_reversal} label="Failed-reversal cut"
            desc="Cuts ~45s after entry if it hasn't worked yet. This is what was closing your puts in under a minute."
            onToggle={() => post('/scalper/config', { manual_exit_failed_reversal: !st.manual_exit_failed_reversal })} />
          <ExitToggle on={st.manual_exit_cross_back} label="EMA cross-back"
            desc="Exits when the fast/slow EMAs flip against your position."
            onToggle={() => post('/scalper/config', { manual_exit_cross_back: !st.manual_exit_cross_back })} />
          <ExitToggle on={st.manual_exit_fast_exit} label="Fast-EMA break"
            desc="Exits when the underlying breaks back through the exit-EMA."
            onToggle={() => post('/scalper/config', { manual_exit_fast_exit: !st.manual_exit_fast_exit })} />
          <ExitToggle on={st.manual_exit_peak_lock} label="Peak-lock / trail"
            desc="Auto-locks a winner's gains on a sharp reversal, so a runner doesn't give it all back."
            onToggle={() => post('/scalper/config', { manual_exit_peak_lock: !st.manual_exit_peak_lock })} />
          <ExitToggle on={st.manual_exit_profit_target} label="Profit target"
            desc="Auto-banks the trade once it reaches the profit target."
            onToggle={() => post('/scalper/config', { manual_exit_profit_target: !st.manual_exit_profit_target })} />
          <ExitToggle on={st.manual_exit_stop} label="Stops (safety floor)"
            desc="Underlying stop + catastrophe backstop + far time-stop. Keep ON so a forgotten trade can't bleed to zero."
            onToggle={() => post('/scalper/config', { manual_exit_stop: !st.manual_exit_stop })} />
        </div>
      </Collapsible>

      {/* live signals — compact per-symbol read */}
      <div className="glass p-4 rounded-xl">
        <div className="eyebrow mb-2">Live signals</div>
        <div className="grid grid-cols-2 gap-3">
          {Object.entries(st.signals || {}).map(([sym, s]) => (
            <div key={sym} className="text-xs font-mono space-y-0.5">
              <div className="text-accent font-bold">{sym} {s.last ? s.last.toFixed(2) : '—'}</div>
              <div>ER: {fmt(s.efficiency_ratio)} · range%: {fmt(s.recent_range_pct)}</div>
              <div>vwap_z: {fmt(s.vwap_z)} · MTF: {s.mtf ? `${s.mtf[0]>0?'up':s.mtf[0]<0?'down':'flat'} ${(s.mtf[1]*100).toFixed(0)}%` : '—'}</div>
              <div>book: <span className={s.book_imbalance == null ? '' : s.book_imbalance > 0 ? 'text-up' : 'text-down'}>{fmt(s.book_imbalance)}</span></div>
            </div>
          ))}
        </div>
      </div>

      {/* recent trades — collapsible so it can be tucked away */}
      <Collapsible title="Recent trades" defaultOpen subtitle={tradesSummary(trades)}>
        <div className="flex items-center justify-end mb-2">
          {/* strategy filter — compare the A/B scalp strategies */}
          {stratsPresent.length > 1 && (
            <div className="flex gap-1">
              {['all', ...stratsPresent].map((s) => (
                <button key={s} onClick={() => setStratFilter(s)}
                  className={`px-2 py-0.5 rounded text-[10px] font-mono ${stratFilter === s ? 'bg-accent text-black font-bold' : 'bg-raised text-muted'}`}>
                  {s === 'all' ? 'All' : stratLabel(s)}
                </button>
              ))}
            </div>
          )}
        </div>
        {/* per-strategy comparison line (only when >1 strategy has traded) */}
        {stratStats.length > 1 && (
          <div className="flex flex-wrap gap-3 mb-2 text-[11px] font-mono">
            {stratStats.map((s) => (
              <span key={s.strat} className="text-muted">
                <span className="text-accent">{stratLabel(s.strat)}</span>{' '}
                <span className={s.pnl >= 0 ? 'text-up' : 'text-down'}>{s.pnl >= 0 ? '+' : ''}{s.pnl.toFixed(0)}</span>{' '}
                <span className="text-faint">({s.n}·{s.wr.toFixed(0)}%wr)</span>
              </span>
            ))}
          </div>
        )}
        {/* aligned grid: time | strat | contract | qty | entry→exit | P&L | % | held | reason */}
        <div className="text-[11px] font-mono">
          <div className="grid grid-cols-[70px_58px_96px_30px_92px_54px_40px_38px_1fr] gap-x-2 px-1 pb-1 text-[10px] text-muted uppercase tracking-wide border-b border-line">
            <span>Time</span><span>Strat</span><span>Contract</span><span className="text-right">Qty</span>
            <span className="text-right">Entry→Exit</span><span className="text-right">P&amp;L</span>
            <span className="text-right">%</span><span className="text-right">Held</span><span>Exit</span>
          </div>
          <div className="max-h-72 overflow-y-auto">
            {shownTrades.map((t) => {
              const win = (t.pnl ?? 0) >= 0;
              const dt = new Date(t.ts);
              const isToday = dt.toDateString() === new Date().toDateString();
              const when = isToday
                ? dt.toLocaleTimeString([], { hour12: false })
                : dt.toLocaleString([], { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
              return (
                <div key={t.id} className={`grid grid-cols-[70px_58px_96px_30px_92px_54px_40px_38px_1fr] gap-x-2 px-1 py-0.5 border-b border-line items-center ${isToday ? '' : 'opacity-50'}`}>
                  <span className="text-faint">{when}</span>
                  <span className="text-accent text-[9px] leading-tight truncate" title={stratLabel(stratOf(t))}>{stratLabel(stratOf(t))}</span>
                  <span className={t.direction === 'call' ? 'text-up' : 'text-down'}>{prettyOcc(t.occ)}</span>
                  <span className="text-right text-muted">{t.qty ?? '?'}</span>
                  <span className="text-right text-muted">{t.entry != null ? t.entry.toFixed(2) : '—'}→{t.exit != null ? t.exit.toFixed(2) : '—'}</span>
                  <span className={`text-right font-bold ${win ? 'text-up' : 'text-down'}`}>{win ? '+' : ''}{(t.pnl ?? 0).toFixed(0)}</span>
                  <span className={`text-right ${win ? 'text-up/70' : 'text-down/70'}`}>{t.pnl_pct != null ? `${(t.pnl_pct * 100).toFixed(0)}%` : ''}</span>
                  <span className="text-right text-faint">{t.held_s != null ? `${Math.round(t.held_s)}s` : ''}</span>
                  <span className="text-faint truncate">{t.reason}</span>
                </div>
              );
            })}
            {!shownTrades.length && <div className="text-muted px-1 py-2">No trades yet.</div>}
          </div>
        </div>
      </Collapsible>
    </div>
  );
}

const fmtSec = (s) => (s == null ? '—' : s >= 60 ? `${Math.round(s / 60)}m${s % 60 ? ` ${s % 60}s` : ''}` : `${s}s`);
function Cadence({ label, valSec, opts, onPick }) {
  return (
    <div>
      <div className="text-[10px] text-muted uppercase mb-1">{label} <span className="text-accent">{fmtSec(valSec)}</span></div>
      <div className="flex gap-1 flex-wrap">
        {opts.map((o) => (
          <button key={o} onClick={() => onPick(o)}
            className={`px-2 py-0.5 rounded text-[10px] ${valSec === o ? 'bg-accent text-black font-bold' : 'bg-raised text-muted'}`}>
            {fmtSec(o)}
          </button>
        ))}
      </div>
    </div>
  );
}
function prettyOcc(occ) {
  if (!occ) return '—';
  const m = occ.match(/^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/);
  if (!m) return occ;
  const [, root, , mm, dd, cp, strike] = m;
  return `${root} ${parseInt(mm)}/${parseInt(dd)} ${parseInt(strike) / 1000}${cp}`;
}
const fmt = (v) => (v == null ? '—' : typeof v === 'number' ? v.toFixed(3) : String(v));
const Stat = ({ label, val, color }) => (
  <div className="glass p-3 rounded-lg text-center">
    <div className="text-[10px] text-muted uppercase">{label}</div>
    <div className={`text-lg font-bold ${color || 'text-ink'}`}>{val}</div>
  </div>
);

// summary chip for the Manual-exits collapsible ("2/6 rules on")
const MANUAL_EXIT_KEYS = ['manual_exit_failed_reversal', 'manual_exit_cross_back',
  'manual_exit_fast_exit', 'manual_exit_peak_lock', 'manual_exit_profit_target', 'manual_exit_stop'];
function manualExitSummary(st) {
  const on = MANUAL_EXIT_KEYS.filter((k) => st[k]).length;
  if (on === 0) return 'all off — you close every manual trade yourself';
  if (on === MANUAL_EXIT_KEYS.length) return 'all on — manual trades exit like the algos';
  return `${on}/${MANUAL_EXIT_KEYS.length} rules on`;
}

// subtitle for the Recent-trades collapsible: today's count, P&L, win rate
function tradesSummary(trades) {
  const today = new Date().toDateString();
  const todays = (trades || []).filter((t) => new Date(t.ts).toDateString() === today);
  if (!todays.length) return (trades || []).length ? `${trades.length} total · none today` : 'no trades yet';
  const pnl = todays.reduce((a, t) => a + (t.pnl ?? 0), 0);
  const wins = todays.filter((t) => (t.pnl ?? 0) >= 0).length;
  return `${todays.length} today · ${pnl >= 0 ? '+' : ''}$${pnl.toFixed(0)} · ${Math.round((wins / todays.length) * 100)}% wr`;
}

// a single enable/disable checkbox row for a manual-trade exit rule
function ExitToggle({ on, label, desc, onToggle }) {
  return (
    <button onClick={onToggle} role="switch" aria-checked={!!on} aria-label={label}
      className={`flex items-start gap-2.5 text-left p-2 rounded-md ring-1 transition-colors ${on ? 'bg-accent/10 ring-accent/40' : 'bg-raised ring-line hover:ring-muted/50'}`}>
      <span className={`mt-0.5 flex-none w-4 h-4 rounded flex items-center justify-center text-[10px] font-bold ${on ? 'bg-accent text-base' : 'bg-base ring-1 ring-line text-transparent'}`}>✓</span>
      <span className="min-w-0">
        <span className={`block text-xs font-semibold ${on ? 'text-ink' : 'text-muted'}`}>{label}</span>
        <span className="block text-[10px] text-faint leading-snug">{desc}</span>
      </span>
    </button>
  );
}

// Collapsible pane — tucks parameter groups (risk / timing / trading knobs) away
// so the cockpit (chart, ladder, positions, confirm) stays front-and-center.
// Defaults collapsed; state is local (resets on reload, which is fine for knobs).
function Collapsible({ title, subtitle, defaultOpen = false, children }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="glass rounded-xl overflow-hidden">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-4 py-2.5 text-left hover:bg-white/5 transition-colors"
      >
        <span className="flex items-center gap-2">
          <span className="text-xs font-medium text-ink">{title}</span>
          {subtitle && <span className="text-[10px] text-muted">{subtitle}</span>}
        </span>
        <span className={`text-muted text-xs transition-transform ${open ? 'rotate-90' : ''}`}>▶</span>
      </button>
      {open && <div className="px-4 pb-4 pt-1 border-t border-line">{children}</div>}
    </div>
  );
}

// SYMBOL COCKPIT — everything for one symbol in one place: chart + P&L meter +
// Buy Call / Buy Put + a Close button on each open position. Trade controls sit right
// beside the chart for quick, in-context execution. Manual entries are tracked as MANUAL.
function stratShort(s) {
  return ({ apex: 'APEX', bullpivot: 'BULL', manual: 'MANUAL', macross_x: 'X', ai: 'AI',
    macross3: '9/21/50', rehydrated: '—' })[s] || (s || '—');
}
function SymbolCockpit({ apiBaseUrl, symbol, positions, live, refresh, last }) {
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState(null);
  const mine = positions.filter((p) => p.underlying === symbol);
  const hasManual = mine.some((p) => p.strategy === 'manual');

  const buy = async (direction) => {
    setBusy(direction); setNote(null);
    try {
      const r = await fetch(`${apiBaseUrl}/scalper/manual`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, direction }),
      });
      const d = await r.json();
      setNote(d.ok
        ? { ok: true, msg: `Bought ${direction.toUpperCase()} ×${d.qty} @ $${d.entry}` }
        : { ok: false, msg: d.error || 'entry failed' });
    } catch (e) { setNote({ ok: false, msg: e.message }); }
    finally { setBusy(''); refresh(); }
  };
  const close = async (p) => {
    setBusy(`close:${p.scalp_key || p.occ}`);
    try {
      await fetch(`${apiBaseUrl}/scalper/close`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scalp_key: p.scalp_key || p.occ }),
      });
    } finally { setBusy(''); refresh(); }
  };

  return (
    <div className="panel p-3 flex flex-col gap-2">
      {/* header: symbol + last price + position count */}
      <div className="flex items-baseline justify-between">
        <div className="flex items-baseline gap-2">
          <span className="font-display font-bold text-ink">{symbol}</span>
          {last != null && <span className="num text-sm text-muted">{last.toFixed(2)}</span>}
        </div>
        <span className="eyebrow">{mine.length ? `${mine.length} open` : 'flat'}</span>
      </div>

      {/* live P&L meter for the symbol's position(s) */}
      {mine.map((p) => <PnlMeter key={`m${p.scalp_key || p.occ}`} position={p} />)}

      {/* chart */}
      <CandleChart apiBaseUrl={apiBaseUrl} symbol={symbol} height={280} />

      {/* quick-access trade controls, right under the chart */}
      <div className="flex items-center gap-2">
        <button onClick={() => buy('call')} disabled={!!busy || hasManual}
          title={hasManual ? 'Manual already holds this symbol' : 'Buy a call at spot (tracked as MANUAL)'}
          className="flex-1 px-3 py-2 rounded-md bg-up/15 text-up ring-1 ring-up/40 hover:bg-up/25 text-sm font-semibold disabled:opacity-30 transition-colors">
          {busy === 'call' ? '…' : 'Buy Call'}
        </button>
        <button onClick={() => buy('put')} disabled={!!busy || hasManual}
          title={hasManual ? 'Manual already holds this symbol' : 'Buy a put at spot (tracked as MANUAL)'}
          className="flex-1 px-3 py-2 rounded-md bg-down/15 text-down ring-1 ring-down/40 hover:bg-down/25 text-sm font-semibold disabled:opacity-30 transition-colors">
          {busy === 'put' ? '…' : 'Buy Put'}
        </button>
      </div>

      {/* open positions on this symbol — prominent one-click Close per position,
          right beside the chart, same weight as the Buy buttons (quick exits) */}
      {mine.length > 0 && (
        <div className="flex flex-col gap-1.5 pt-2 border-t border-line">
          {mine.map((p) => {
            const up = (p.pnl_usd ?? 0) >= 0;
            const closing = busy === `close:${p.scalp_key || p.occ}`;
            return (
              <div key={p.scalp_key || p.occ} className="flex items-center gap-2">
                <span className={`num px-1.5 py-0.5 rounded text-[10px] font-semibold ${p.direction === 'call' ? 'bg-up/15 text-up' : 'bg-down/15 text-down'}`}>
                  {stratShort(p.strategy)}
                </span>
                <span className="num text-xs text-muted">{p.direction}{p.qty ? ` ×${p.qty}` : ''}</span>
                {p.pnl_usd != null && (
                  <span className={`num text-xs font-semibold ${up ? 'text-up' : 'text-down'}`}>
                    {up ? '+' : ''}{p.pnl_usd.toFixed(0)}
                  </span>
                )}
                <button onClick={() => close(p)} disabled={busy.startsWith('close')}
                  title={`Close ${p.direction} ×${p.qty || 1}`}
                  className="flex-1 px-3 py-2 rounded-md bg-down/15 text-down ring-1 ring-down/40 hover:bg-down/25 text-sm font-semibold disabled:opacity-40 transition-colors">
                  {closing ? 'Closing…' : `Close ${p.direction.toUpperCase()}`}
                </button>
              </div>
            );
          })}
        </div>
      )}
      {note && <div className={`text-[11px] ${note.ok ? 'text-up' : 'text-warn'}`}>{note.msg}</div>}
      <div className="text-[10px] text-faint">{live ? 'LIVE $ — real money' : 'paper'} · manual exits auto (peak-lock / stop) like the algos</div>
    </div>
  );
}

// info icon with a hover/tap tooltip — explains each mode/strategy
function InfoDot({ text, dark = false }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="relative inline-flex">
      <button type="button"
        onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
        onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}
        className={`w-4 h-4 rounded-full text-[10px] font-bold flex items-center justify-center ${dark ? 'bg-black/30 text-black' : 'bg-raised text-ink'}`}
        aria-label="info">i</button>
      {open && (
        <span className="absolute z-20 bottom-full left-1/2 -translate-x-1/2 mb-1 w-64 p-2 rounded-lg bg-base border border-line text-[11px] text-ink font-normal normal-case leading-snug shadow-xl">
          {text}
        </span>
      )}
    </span>
  );
}

// tier-1 selectable card (AI decides / Run all / Run one) with an info icon
function ModeCard({ active, title, sub, info, onClick }) {
  return (
    <button onClick={onClick}
      className={`flex-1 min-w-[130px] text-left px-3 py-2 rounded-lg border transition ${active ? 'bg-accent border-blue-400 text-black' : 'bg-raised/60 border-line text-ink hover:bg-raised'}`}>
      <div className="flex items-center justify-between gap-1">
        <span className="text-sm font-bold">{title}</span>
        <InfoDot text={info} dark={active} />
      </div>
      <div className={`text-[10px] ${active ? 'text-black/70' : 'text-muted'}`}>{sub}</div>
    </button>
  );
}

// numeric +/- stepper with clamping (used for EMA fast/slow/timeframe/sep)
function Stepper({ label, val, min, max, step, dp = 0, onSet }) {
  const clamp = (v) => Math.min(max, Math.max(min, +v.toFixed(dp)));
  const cur = val == null ? min : val;
  return (
    <div>
      <div className="text-[10px] text-muted uppercase mb-1">{label}</div>
      <div className="flex items-center gap-1">
        <button onClick={() => onSet(clamp(cur - step))} disabled={cur <= min}
          className="px-2 py-0.5 rounded bg-raised text-ink text-sm disabled:opacity-30">−</button>
        <span className="font-mono text-accent text-sm w-14 text-center">{dp ? cur.toFixed(dp) : cur}</span>
        <button onClick={() => onSet(clamp(cur + step))} disabled={cur >= max}
          className="px-2 py-0.5 rounded bg-raised text-ink text-sm disabled:opacity-30">+</button>
      </div>
    </div>
  );
}

const ET = (utcMin) => {
  if (utcMin == null) return '—';
  let m = (utcMin - 240) % 1440; if (m < 0) m += 1440;  // EDT = UTC-4
  const h = Math.floor(m / 60), mm = m % 60;
  const ap = h >= 12 ? 'PM' : 'AM', h12 = ((h + 11) % 12) + 1;
  return `${h12}:${String(mm).padStart(2, '0')} ${ap} ET`;
};

// Big clear banner: is the scalper actually placing trades right now?
function EntryStateBanner({ state, reason, startUtc, endUtc }) {
  const active = state === 'active';
  const win = (startUtc != null || endUtc != null)
    ? `Entry window ${ET(startUtc)}–${ET(endUtc)}` : 'Trading all day';
  const map = {
    active:        ['🟢', 'TRADING — entries active', 'text-up ring-green-500/40'],
    outside_window:['🌙', 'STOPPED — outside entry window', 'text-warn ring-yellow-500/40'],
    // 'blackout' covers BOTH the post-open wait and the pre-close window — the backend
    // 'reason' (shown below) says which; don't hardcode "close" here.
    blackout:      ['🔕', 'STOPPED — blackout window', 'text-warn ring-yellow-500/40'],
    paused:        ['⏸️', 'PAUSED — AI stand-aside', 'text-warn ring-yellow-500/40'],
    stopped:       ['⛔', 'STOPPED', 'text-muted ring-line/40'],
    kill:          ['⛔', 'STOPPED — kill switch', 'text-down ring-red-500/40'],
    profit_lock:   ['🔒', 'STOPPED — profit lock', 'text-warn ring-amber-500/40'],
  };
  const [icon, title, cls] = map[state] || ['•', (state || '—').toUpperCase(), 'text-muted ring-line/40'];
  return (
    <div className={`glass p-3 rounded-xl ring-1 ${cls.split(' ').slice(1).join(' ')} flex items-center justify-between`}>
      <div className="flex items-center gap-2">
        <span className="text-lg">{icon}</span>
        <div>
          <div className={`text-sm font-bold ${cls.split(' ')[0]}`}>{title}</div>
          {reason && !active && <div className="text-[11px] text-muted">{reason}</div>}
        </div>
      </div>
      <div className="text-[11px] text-muted text-right">{win}</div>
    </div>
  );
}
