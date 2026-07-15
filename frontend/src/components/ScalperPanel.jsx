// Standalone scalper control panel — status, mode, toggle, live signals, AI feed.
import { useState, useEffect, useCallback } from 'react';
import CandleChart from './CandleChart';
import ConfirmCard from './ConfirmCard';

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

  if (err) return <div className="glass p-6 rounded-xl text-red-400">Scalper API error: {err}</div>;
  if (!st) return <div className="text-text-secondary p-6">Loading scalper…</div>;
  if (!st.available) return <div className="glass p-6 rounded-xl text-yellow-400">Scalper not initialized — check the API server logs (Schwab re-auth may be required).</div>;

  const pnlColor = (st.day_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400';

  // ── signal-mode model (two tiers: AI-chooses vs. run-one-manually) ─────────
  // Each entry: value sent to the API, short label, and an info blurb (info icon).
  const MODE_INFO = {
    ai:        { label: 'AI',        desc: 'The AI supervisor reads the live tape every cycle and decides how to trade: momentum, fade, or stand aside (pause). On a clean, sustained trend it can also switch ON the mechanical MA-cross to run alongside it. This is the "let the AI drive" mode.' },
    all:       { label: 'All (A/B)', desc: 'Runs the surviving strategies at once — APEX, X, and the AI — each holding its own separate positions, so you can compare their P&L head-to-head. (9/21/50 was retired for poor performance.)' },
    apex:      { label: 'APEX', desc: 'The synthesis strategy — every filter that won across 2 weeks of live A/B, stacked for maximum selectivity and a high win rate. ENTRY needs ALL of: (1) a fresh 8/21 EMA cross [best-price timing], (2) L2 order-book/wall confirmation [the biggest edge], (3) the dominant trend agreeing [never fight it], (4) efficiency above a strict floor [no chop], (5) not over-extended from VWAP [no chasing]. EXIT: peak-giveback lock + underlying stop. Few trades, high conviction.' },
    macross:   { label: 'MA9/21/50 (retired)', desc: 'RETIRED — consistently the worst performer (-$460 all-time, ~30% win rate, stopped on half its trades). Kept selectable for reference only. 9/21/50 triple-EMA: enter when the 9 leads through both the 21 and 50; exit on the opposite 9×21 cross; stop on EMA50 break.' },
    macross_x: { label: 'MA cross X',desc: 'A stripped-down cross variant: enters ONLY on a fresh EMA cross confirmed by L2 order-flow or a liquidity wall — no other gates. Faster, more selective on confirmation. The A/B foil to the full-gate MA cross.' },
    momentum:  { label: 'Momentum',  desc: 'Legacy rule-based mode: trade WITH the move using the momentum/efficiency signals. No AI, no EMA-cross — the original mechanical scalper path.' },
    fade:      { label: 'Fade',      desc: 'Legacy rule-based mode: mean-reversion — fade extensions (buy dips / sell pops) around VWAP and levels. The counter-trend sibling of Momentum.' },
    auto:      { label: 'Auto',      desc: 'Legacy mode: automatically switches between momentum and fade based on the efficiency ratio (trend vs. range) — mechanical, no AI.' },
  };
  const MANUAL_MODES = ['apex', 'macross_x', 'macross', 'momentum', 'fade', 'auto'];  // "run one" options
  const aiDriven = st.signal_mode === 'ai';
  const allMode = st.signal_mode === 'all';

  // ── per-strategy comparison + trades-table filter ─────────────────────────
  // compact labels that fit the tight Strat column. macross3 = the new 9/21/50 triple;
  // 'macross' = the RETIRED 8/21 full-gate strat (kept distinct so old trades aren't renamed).
  const stratLabel = (s) => ({
    apex: 'APEX',
    macross3: '9/21/50',
    macross_x: 'X',
    ai: 'AI',
  }[s] || s || '—');
  const stratOf = (t) => t.strategy || 'macross';
  // RETIRED strategies hidden from the comparison table: the old 8/21 'macross'
  // (MA-old) and the pre-labeling NULL trades (which default to 'macross'). Only the
  // strategies actually being run today (9/21/50, X, AI) are shown.
  const HIDDEN_STRATS = new Set(['macross']);
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
      {/* header / master switch */}
      <div className={`glass p-4 rounded-xl flex items-center justify-between ${st.live_trading ? 'ring-2 ring-red-500' : ''}`}>
        <div>
          <div className="text-lg font-bold text-blue-400">
            Scalper {st.live_trading
              ? <span className="text-red-400">· 🔴 LIVE — REAL MONEY</span>
              : <span className="text-text-secondary">· 📝 paper</span>}
          </div>
          <div className="text-xs text-text-secondary">
            {st.symbols?.join(', ')} · feed {st.feed_running ? '🟢 live' : '🔴 down'} · {st.live_trading ? 'Alpaca LIVE' : 'Alpaca paper'}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={toggleLive}
            disabled={!st.live_available && !st.live_trading}
            title={!st.live_available ? 'No live Alpaca keys configured' : ''}
            className={`px-3 py-1.5 rounded text-xs font-bold transition-colors disabled:opacity-40 ${
              st.live_trading ? 'bg-red-600 text-white hover:bg-red-500' : 'bg-secondary text-text-secondary hover:bg-secondary/70'
            }`}
          >
            {st.live_trading ? 'LIVE ✓ (click for paper)' : st.live_available ? 'Go LIVE 💵' : 'LIVE (no keys)'}
          </button>
          <button
            onClick={() => post('/scalper/autonomous', { autonomous: !st.autonomous })}
            title={st.autonomous
              ? 'Autonomous: signals fire automatically. Click to require confirmation.'
              : 'Confirm mode: you approve each entry/exit signal (risk stops still auto-fire). Click for autonomous.'}
            className={`px-3 py-1.5 rounded text-xs font-bold transition-colors ${
              st.autonomous ? 'bg-indigo-500 text-black hover:bg-indigo-400' : 'bg-amber-500 text-black hover:bg-amber-400'
            }`}
          >
            {st.autonomous ? '🤖 Autonomous' : '🙋 Confirm mode'}
          </button>
          <button
            onClick={() => post('/scalper/toggle', { enabled: !st.enabled })}
            className={`px-4 py-1.5 rounded font-bold ${st.enabled ? 'bg-green-500 text-black' : 'bg-gray-600 text-white'}`}
          >
            {st.enabled ? 'ON' : 'OFF'}
          </button>
        </div>
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
        <div className="glass p-3 rounded-lg text-sm text-amber-300">
          {st.kill_switch_tripped ? '⛔ Kill switch tripped — new entries halted.' : '🔒 Profit lock tripped — gains locked, entries halted.'}
        </div>
      )}

      {/* entry state — is it actually trading right now? */}
      <EntryStateBanner state={st.entry_state} reason={st.entry_reason}
                        startUtc={st.daily_entry_start_utc} endUtc={st.daily_entry_end_utc} />

      {/* mode selector — two tiers: WHO decides, then (if manual) WHICH strategy */}
      <div className="glass p-4 rounded-xl">
        <div className="text-xs text-text-secondary mb-2">Who's trading</div>

        {/* Tier 1: AI drives  |  All (A/B)  |  Run one manually */}
        <div className="flex gap-2 flex-wrap">
          <ModeCard active={aiDriven} title="AI decides" sub="AI picks the strategy live"
                    info={MODE_INFO.ai.desc}
                    onClick={() => post('/scalper/config', { signal_mode: 'ai' })} />
          <ModeCard active={allMode} title="Run all (A/B)" sub="every strategy, compared"
                    info={MODE_INFO.all.desc}
                    onClick={() => post('/scalper/config', { signal_mode: 'all' })} />
          <ModeCard active={!aiDriven && !allMode} title="Run one manually" sub="pick a single strategy"
                    info="Turn OFF the AI/all and run exactly one strategy yourself — choose it below."
                    onClick={() => { if (aiDriven || allMode) post('/scalper/config', { signal_mode: 'macross' }); }} />
        </div>

        {/* Tier 2: manual strategy picker (only when a single strategy is selected) */}
        {!aiDriven && !allMode && (
          <div className="mt-3 pt-3 border-t border-gray-700/60">
            <div className="text-[10px] text-text-secondary uppercase mb-1.5">Strategy</div>
            <div className="flex gap-2 flex-wrap">
              {MANUAL_MODES.map((m) => (
                <button key={m} onClick={() => post('/scalper/config', { signal_mode: m })}
                  className={`px-3 py-1 rounded text-sm inline-flex items-center gap-1 ${st.signal_mode === m ? 'bg-blue-500 text-black font-bold' : 'bg-gray-700 text-gray-200'}`}>
                  {MODE_INFO[m].label}
                  <InfoDot text={MODE_INFO[m].desc} dark={st.signal_mode === m} />
                </button>
              ))}
            </div>
          </div>
        )}

        {/* AI-drives detail: current call + macross-alongside toggle state */}
        {aiDriven && (
          <div className="mt-3 pt-3 border-t border-gray-700/60 text-xs space-y-1">
            <div className="text-text-secondary">
              AI call: <span className="text-blue-300 font-mono">{st.ai_mode || '—'}</span>
              {st.ai_reason && <span> — {st.ai_reason}</span>}
            </div>
            <div className="text-text-secondary">
              Mechanical MA-cross alongside:{' '}
              <span className={st.ai_enable_macross ? 'text-green-400 font-bold' : 'text-slate-500'}>
                {st.ai_enable_macross ? 'ON (AI sees a clean trend)' : 'off (AI: not a trend)'}
              </span>
            </div>
          </div>
        )}
        {['macross', 'macross_x', 'all'].includes(st.signal_mode) && (
          <div className="mt-3 space-y-3">
            <div className="text-xs text-text-secondary">
              MA crossover · <span className="font-mono text-blue-300">{st.ma_fast}/{st.ma_slow}</span> EMA on {st.ma_tf_seconds}s ·
              spread (fast−slow): <span className="font-mono">{st.ma_min_separation_pct}% min to enter</span>
              {st.signal_mode === 'macross_x' && <span className="ml-1 text-amber-300">· X: fresh cross + L2/wall only</span>}
              {st.signal_mode === 'all' && (
                <span className="ml-1 text-amber-300">
                  · running {(st.active_strategies || ['macross','macross_x']).map(stratLabel).join(' + ')} independently
                </span>
              )}
            </div>
            {st.signal_mode === 'all' && (st.active_strategies || []).includes('ai') && (
              <div className="text-[11px] text-text-secondary">
                AI strategy: mode <span className="font-mono text-blue-300">{st.ai_mode || '—'}</span>
                {' '}· own EMA <span className="font-mono text-blue-300">{st.ma_fast_ai}/{st.ma_slow_ai}</span> (AI-tuned)
                {st.ai_reason && <span> — {st.ai_reason}</span>}
              </div>
            )}
            <div className="grid grid-cols-2 gap-3">
              <Stepper label="Fast EMA" val={st.ma_fast} min={3} max={st.ma_slow - 1} step={1}
                       onSet={(v) => post('/scalper/config', { ma_fast: v })} />
              <Stepper label="Slow EMA" val={st.ma_slow} min={st.ma_fast + 1} max={60} step={1}
                       onSet={(v) => post('/scalper/config', { ma_slow: v })} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <Stepper label="Timeframe (s)" val={st.ma_tf_seconds} min={15} max={120} step={15}
                       onSet={(v) => post('/scalper/config', { ma_tf_seconds: v })} />
              <Stepper label="Min sep %" val={st.ma_min_separation_pct} min={0} max={0.10} step={0.005} dp={3}
                       onSet={(v) => post('/scalper/config', { ma_min_separation_pct: v })} />
            </div>
            <div className="text-[11px] text-text-secondary">
              Lower fast vs slow = more crosses / faster (more whipsaw). Wider gap = fewer, cleaner signals.
              Fast {st.ma_fast} must stay below Slow {st.ma_slow}.
            </div>
          </div>
        )}
        {st.signal_mode === 'ai' && (
          <>
            <div className="mt-2 text-xs">
              AI mode: <span className="text-blue-300 font-mono">{st.ai_mode || '—'}</span>
              {st.ai_reason && <span className="text-text-secondary"> — {st.ai_reason}</span>}
              {st.ai_error && <span className="text-red-400"> · error: {st.ai_error}</span>}
            </div>
            <div className="mt-3 grid grid-cols-2 gap-3">
              <Cadence label="Mode-check every" valSec={st.ai_supervisor_seconds}
                       opts={[60, 120, 180, 300, 600, 900]} onPick={(v) => post('/scalper/config', { ai_supervisor_seconds: v })} />
              <Cadence label="Deep tune every" valSec={st.ai_tune_seconds}
                       opts={[180, 300, 600, 900]} onPick={(v) => post('/scalper/config', { ai_tune_seconds: v })} />
            </div>
          </>
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

      {/* risk & timing settings (operator-only) */}
      <div className="glass p-4 rounded-xl">
        <div className="text-xs text-text-secondary mb-2">Risk &amp; timing</div>
        <div className="grid grid-cols-2 gap-3">
          <Stepper label="Kill switch $" val={st.daily_loss_kill_usd} min={10} max={2000} step={10}
                   onSet={(v) => post('/scalper/config', { daily_loss_kill_usd: v })} />
          <Stepper label="Wait after open (min)" val={st.open_blackout_minutes} min={0} max={60} step={1}
                   onSet={(v) => post('/scalper/config', { open_blackout_minutes: v })} />
        </div>
        <div className="text-[11px] text-text-secondary mt-2">
          Kill switch halts new entries once the day is down this much (from the current baseline).
          Wait-after-open sits out the first N min after 9:30 ET — the opening whipsaw — before scalping (0 = trade from the bell).
        </div>
      </div>

      {/* live signals */}
      <div className="glass p-4 rounded-xl">
        <div className="text-xs text-text-secondary mb-2">Live signals</div>
        <div className="grid grid-cols-2 gap-3">
          {Object.entries(st.signals || {}).map(([sym, s]) => (
            <div key={sym} className="text-xs font-mono space-y-0.5">
              <div className="text-blue-300 font-bold">{sym} {s.last ? s.last.toFixed(2) : '—'}</div>
              <div>ER: {fmt(s.efficiency_ratio)} · range%: {fmt(s.recent_range_pct)}</div>
              <div>vwap_z: {fmt(s.vwap_z)} · MTF: {s.mtf ? `${s.mtf[0]>0?'up':s.mtf[0]<0?'down':'flat'} ${(s.mtf[1]*100).toFixed(0)}%` : '—'}</div>
              <div>book: <span className={s.book_imbalance == null ? '' : s.book_imbalance > 0 ? 'text-green-400' : 'text-red-400'}>{fmt(s.book_imbalance)}</span></div>
            </div>
          ))}
        </div>
      </div>

      {/* live 1-min candlestick charts — 9/21/50 EMAs + 9×21 crossover markers */}
      <div className="glass p-4 rounded-xl">
        <div className="text-xs text-text-secondary mb-2">Live charts — 1-min candles · EMA 9 / 21 / 50 · crossover signals (on candle close)</div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {(st.symbols || []).map((sym) => (
            <div key={sym}>
              <div className="text-[11px] font-bold text-blue-300 mb-1">{sym}</div>
              <CandleChart apiBaseUrl={apiBaseUrl} symbol={sym} height={300} />
            </div>
          ))}
        </div>
      </div>

      {/* active strategy spec */}
      {st.active_strategy_spec && (
        <div className="glass p-3 rounded-xl text-xs">
          <div className="text-text-secondary mb-1">AI strategy spec active</div>
          <pre className="text-green-300 overflow-x-auto">{JSON.stringify(st.active_strategy_spec, null, 1)}</pre>
        </div>
      )}

      {/* AI feed */}
      <div className="glass p-4 rounded-xl">
        <div className="text-xs text-text-secondary mb-2">AI supervisor feed</div>
        <div className="space-y-2 max-h-72 overflow-y-auto">
          {(st.ai_log || []).map((e, i) => (
            <div key={i} className="text-xs border-b border-gray-700 pb-1.5">
              <span className={`font-bold ${e.cycle === 'deep' ? 'text-purple-300' : 'text-blue-300'}`}>[{e.cycle}] {e.mode?.toUpperCase()}</span>
              <span className="text-text-secondary"> {(e.timestamp || '').slice(11, 19)}</span>
              <div className="text-gray-200">{e.reasoning || e.assessment}</div>
              {e.param_changes && <div className="text-amber-300 font-mono">tuned: {JSON.stringify(e.param_changes)}</div>}
            </div>
          ))}
          {(!st.ai_log || !st.ai_log.length) && <div className="text-text-secondary text-xs">No AI decisions yet (AI mode runs every ~2min in market hours).</div>}
        </div>
      </div>

      {/* recent trades */}
      <div className="glass p-4 rounded-xl">
        <div className="flex items-center justify-between mb-2">
          <div className="text-xs text-text-secondary">Recent trades</div>
          {/* strategy filter — compare the A/B scalp strategies */}
          {stratsPresent.length > 1 && (
            <div className="flex gap-1">
              {['all', ...stratsPresent].map((s) => (
                <button key={s} onClick={() => setStratFilter(s)}
                  className={`px-2 py-0.5 rounded text-[10px] font-mono ${stratFilter === s ? 'bg-blue-500 text-black font-bold' : 'bg-gray-700 text-gray-300'}`}>
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
              <span key={s.strat} className="text-slate-400">
                <span className="text-blue-300">{stratLabel(s.strat)}</span>{' '}
                <span className={s.pnl >= 0 ? 'text-green-400' : 'text-red-400'}>{s.pnl >= 0 ? '+' : ''}{s.pnl.toFixed(0)}</span>{' '}
                <span className="text-slate-500">({s.n}·{s.wr.toFixed(0)}%wr)</span>
              </span>
            ))}
          </div>
        )}
        {/* aligned grid: time | strat | contract | qty | entry→exit | P&L | % | held | reason */}
        <div className="text-[11px] font-mono">
          <div className="grid grid-cols-[70px_58px_96px_30px_92px_54px_40px_38px_1fr] gap-x-2 px-1 pb-1 text-[10px] text-text-secondary uppercase tracking-wide border-b border-gray-700">
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
                <div key={t.id} className={`grid grid-cols-[70px_58px_96px_30px_92px_54px_40px_38px_1fr] gap-x-2 px-1 py-0.5 border-b border-gray-800/60 items-center ${isToday ? '' : 'opacity-50'}`}>
                  <span className="text-slate-500">{when}</span>
                  <span className="text-blue-300 text-[9px] leading-tight truncate" title={stratLabel(stratOf(t))}>{stratLabel(stratOf(t))}</span>
                  <span className={t.direction === 'call' ? 'text-green-400' : 'text-red-400'}>{prettyOcc(t.occ)}</span>
                  <span className="text-right text-slate-400">{t.qty ?? '?'}</span>
                  <span className="text-right text-slate-400">{t.entry != null ? t.entry.toFixed(2) : '—'}→{t.exit != null ? t.exit.toFixed(2) : '—'}</span>
                  <span className={`text-right font-bold ${win ? 'text-green-400' : 'text-red-400'}`}>{win ? '+' : ''}{(t.pnl ?? 0).toFixed(0)}</span>
                  <span className={`text-right ${win ? 'text-green-400/70' : 'text-red-400/70'}`}>{t.pnl_pct != null ? `${(t.pnl_pct * 100).toFixed(0)}%` : ''}</span>
                  <span className="text-right text-slate-500">{t.held_s != null ? `${Math.round(t.held_s)}s` : ''}</span>
                  <span className="text-slate-500 truncate">{t.reason}</span>
                </div>
              );
            })}
            {!shownTrades.length && <div className="text-text-secondary px-1 py-2">No trades yet.</div>}
          </div>
        </div>
      </div>
    </div>
  );
}

const fmtSec = (s) => (s == null ? '—' : s >= 60 ? `${Math.round(s / 60)}m${s % 60 ? ` ${s % 60}s` : ''}` : `${s}s`);
function Cadence({ label, valSec, opts, onPick }) {
  return (
    <div>
      <div className="text-[10px] text-text-secondary uppercase mb-1">{label} <span className="text-blue-300">{fmtSec(valSec)}</span></div>
      <div className="flex gap-1 flex-wrap">
        {opts.map((o) => (
          <button key={o} onClick={() => onPick(o)}
            className={`px-2 py-0.5 rounded text-[10px] ${valSec === o ? 'bg-blue-500 text-black font-bold' : 'bg-gray-700 text-gray-300'}`}>
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
    <div className="text-[10px] text-text-secondary uppercase">{label}</div>
    <div className={`text-lg font-bold ${color || 'text-gray-100'}`}>{val}</div>
  </div>
);

// info icon with a hover/tap tooltip — explains each mode/strategy
function InfoDot({ text, dark = false }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="relative inline-flex">
      <button type="button"
        onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
        onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}
        className={`w-4 h-4 rounded-full text-[10px] font-bold flex items-center justify-center ${dark ? 'bg-black/30 text-black' : 'bg-gray-600 text-gray-200'}`}
        aria-label="info">i</button>
      {open && (
        <span className="absolute z-20 bottom-full left-1/2 -translate-x-1/2 mb-1 w-64 p-2 rounded-lg bg-gray-900 border border-gray-600 text-[11px] text-gray-200 font-normal normal-case leading-snug shadow-xl">
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
      className={`flex-1 min-w-[130px] text-left px-3 py-2 rounded-lg border transition ${active ? 'bg-blue-500 border-blue-400 text-black' : 'bg-gray-700/60 border-gray-600 text-gray-200 hover:bg-gray-700'}`}>
      <div className="flex items-center justify-between gap-1">
        <span className="text-sm font-bold">{title}</span>
        <InfoDot text={info} dark={active} />
      </div>
      <div className={`text-[10px] ${active ? 'text-black/70' : 'text-text-secondary'}`}>{sub}</div>
    </button>
  );
}

// numeric +/- stepper with clamping (used for EMA fast/slow/timeframe/sep)
function Stepper({ label, val, min, max, step, dp = 0, onSet }) {
  const clamp = (v) => Math.min(max, Math.max(min, +v.toFixed(dp)));
  const cur = val == null ? min : val;
  return (
    <div>
      <div className="text-[10px] text-text-secondary uppercase mb-1">{label}</div>
      <div className="flex items-center gap-1">
        <button onClick={() => onSet(clamp(cur - step))} disabled={cur <= min}
          className="px-2 py-0.5 rounded bg-gray-700 text-gray-200 text-sm disabled:opacity-30">−</button>
        <span className="font-mono text-blue-300 text-sm w-14 text-center">{dp ? cur.toFixed(dp) : cur}</span>
        <button onClick={() => onSet(clamp(cur + step))} disabled={cur >= max}
          className="px-2 py-0.5 rounded bg-gray-700 text-gray-200 text-sm disabled:opacity-30">+</button>
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
    active:        ['🟢', 'TRADING — entries active', 'text-green-400 ring-green-500/40'],
    outside_window:['🌙', 'STOPPED — outside entry window', 'text-yellow-300 ring-yellow-500/40'],
    // 'blackout' covers BOTH the post-open wait and the pre-close window — the backend
    // 'reason' (shown below) says which; don't hardcode "close" here.
    blackout:      ['🔕', 'STOPPED — blackout window', 'text-yellow-300 ring-yellow-500/40'],
    paused:        ['⏸️', 'PAUSED — AI stand-aside', 'text-yellow-300 ring-yellow-500/40'],
    stopped:       ['⛔', 'STOPPED', 'text-gray-300 ring-gray-500/40'],
    kill:          ['⛔', 'STOPPED — kill switch', 'text-red-400 ring-red-500/40'],
    profit_lock:   ['🔒', 'STOPPED — profit lock', 'text-amber-300 ring-amber-500/40'],
  };
  const [icon, title, cls] = map[state] || ['•', (state || '—').toUpperCase(), 'text-gray-300 ring-gray-500/40'];
  return (
    <div className={`glass p-3 rounded-xl ring-1 ${cls.split(' ').slice(1).join(' ')} flex items-center justify-between`}>
      <div className="flex items-center gap-2">
        <span className="text-lg">{icon}</span>
        <div>
          <div className={`text-sm font-bold ${cls.split(' ')[0]}`}>{title}</div>
          {reason && !active && <div className="text-[11px] text-text-secondary">{reason}</div>}
        </div>
      </div>
      <div className="text-[11px] text-text-secondary text-right">{win}</div>
    </div>
  );
}
