// Standalone scalper control panel — status, mode, toggle, live signals, AI feed.
import { useState, useEffect, useCallback } from 'react';
import VwapChart from './VwapChart';

export default function ScalperPanel({ apiBaseUrl }) {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const [trades, setTrades] = useState([]);

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
  const modes = ['momentum', 'fade', 'auto', 'ai'];

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
            onClick={() => post('/scalper/toggle', { enabled: !st.enabled })}
            className={`px-4 py-1.5 rounded font-bold ${st.enabled ? 'bg-green-500 text-black' : 'bg-gray-600 text-white'}`}
          >
            {st.enabled ? 'ON' : 'OFF'}
          </button>
        </div>
      </div>

      {(st.kill_switch_tripped || st.profit_lock_tripped) && (
        <div className="glass p-3 rounded-lg text-sm text-amber-300">
          {st.kill_switch_tripped ? '⛔ Kill switch tripped — new entries halted.' : '🔒 Profit lock tripped — gains locked, entries halted.'}
        </div>
      )}

      {/* mode selector */}
      <div className="glass p-4 rounded-xl">
        <div className="text-xs text-text-secondary mb-2">Signal mode</div>
        <div className="flex gap-2">
          {modes.map((m) => (
            <button key={m} onClick={() => post('/scalper/config', { signal_mode: m })}
              className={`px-3 py-1 rounded text-sm capitalize ${st.signal_mode === m ? 'bg-blue-500 text-black font-bold' : 'bg-gray-700 text-gray-200'}`}>
              {m}
            </button>
          ))}
        </div>
        {st.signal_mode === 'ai' && (
          <>
            <div className="mt-2 text-xs">
              AI mode: <span className="text-blue-300 font-mono">{st.ai_mode || '—'}</span>
              {st.ai_reason && <span className="text-text-secondary"> — {st.ai_reason}</span>}
              {st.ai_error && <span className="text-red-400"> · error: {st.ai_error}</span>}
            </div>
            <div className="mt-3 grid grid-cols-2 gap-3">
              <Cadence label="Mode-check every" valSec={st.ai_supervisor_seconds}
                       opts={[60, 120, 180, 300]} onPick={(v) => post('/scalper/config', { ai_supervisor_seconds: v })} />
              <Cadence label="Deep tune every" valSec={st.ai_tune_seconds}
                       opts={[180, 300, 600, 900, 1800]} onPick={(v) => post('/scalper/config', { ai_tune_seconds: v })} />
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

      {/* live VWAP-ish charts — dual pane (one per symbol) */}
      <div className="glass p-4 rounded-xl">
        <div className="text-xs text-text-secondary mb-2">Live charts — price vs session mean (VWAP-ish) ±2σ</div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {(st.symbols || []).map((sym) => (
            <div key={sym}>
              <div className="text-[11px] font-bold text-blue-300 mb-1">{sym}</div>
              <VwapChart apiBaseUrl={apiBaseUrl} symbol={sym} height={280} />
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
        <div className="text-xs text-text-secondary mb-2">Recent trades</div>
        {/* aligned grid: time | contract | qty | entry→exit | P&L | % | held | reason */}
        <div className="text-[11px] font-mono">
          <div className="grid grid-cols-[64px_110px_36px_104px_64px_48px_44px_1fr] gap-x-2 px-1 pb-1 text-[10px] text-text-secondary uppercase tracking-wide border-b border-gray-700">
            <span>Time</span><span>Contract</span><span className="text-right">Qty</span>
            <span className="text-right">Entry→Exit</span><span className="text-right">P&amp;L</span>
            <span className="text-right">%</span><span className="text-right">Held</span><span>Exit</span>
          </div>
          <div className="max-h-72 overflow-y-auto">
            {trades.map((t) => {
              const win = (t.pnl ?? 0) >= 0;
              return (
                <div key={t.id} className="grid grid-cols-[64px_110px_36px_104px_64px_48px_44px_1fr] gap-x-2 px-1 py-0.5 border-b border-gray-800/60 items-center">
                  <span className="text-slate-500">{new Date(t.ts).toLocaleTimeString([], { hour12: false })}</span>
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
            {!trades.length && <div className="text-text-secondary px-1 py-2">No trades yet.</div>}
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
