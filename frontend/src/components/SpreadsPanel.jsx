// Premium-selling core — put credit spreads. Status, IV-rank gate, open spreads, trades.
import { useState, useEffect, useCallback } from 'react';

export default function SpreadsPanel({ apiBaseUrl }) {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const [trades, setTrades] = useState([]);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${apiBaseUrl}/spreads/status`);
      setSt(await r.json()); setErr(null);
    } catch (e) { setErr(e.message); }
  }, [apiBaseUrl]);

  const refreshTrades = useCallback(async () => {
    try {
      const r = await fetch(`${apiBaseUrl}/spreads/trades?limit=50`);
      setTrades((await r.json()).trades || []);
    } catch { /* ignore */ }
  }, [apiBaseUrl]);

  useEffect(() => {
    refresh(); refreshTrades();
    const id = setInterval(() => { refresh(); refreshTrades(); }, 4000);
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
      const ok = window.confirm(
        '⚠️ SWITCH SPREADS TO LIVE — REAL MONEY?\n\nCredit spreads will route to your funded Alpaca LIVE account. ' +
        'Defined risk, but a 5-wide index spread risks ~$400 (~30% of a $1.3k account). Paper-validate first.'
      );
      if (!ok) return;
      const res = await fetch(`${apiBaseUrl}/spreads/live`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ live: true }),
      });
      if (!res.ok) { const e = await res.json().catch(() => ({})); alert('Live refused: ' + (e.detail || res.status)); }
      else { const d = await res.json(); alert(`Spreads LIVE. Equity: $${d.live_equity ?? '?'}`); }
    } else {
      await fetch(`${apiBaseUrl}/spreads/live`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ live: false }),
      });
    }
    refresh();
  };

  if (err) return <div className="glass p-6 rounded-xl text-down">Spreads API error: {err}</div>;
  if (!st) return <div className="text-muted p-6">Loading spreads…</div>;
  if (!st.available) return <div className="glass p-6 rounded-xl text-warn">Spread engine not initialized — check API logs.</div>;

  const p = st.params || {};
  const pnlColor = (st.day_pnl ?? 0) >= 0 ? 'text-up' : 'text-down';

  return (
    <div className="space-y-4">
      {/* header */}
      <div className={`glass p-4 rounded-xl flex items-center justify-between ${st.live_trading ? 'ring-2 ring-red-500' : ''}`}>
        <div>
          <div className="text-lg font-bold text-fuchsia-400">
            Premium Core · Put Credit Spreads {st.live_trading
              ? <span className="text-down">· 🔴 LIVE — REAL MONEY</span>
              : <span className="text-muted">· 📝 paper</span>}
          </div>
          <div className="text-xs text-muted">
            Sell premium when IV is rich · 30-45 DTE · manage 50% / 21 DTE · defined risk
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={toggleLive} disabled={!st.live_available && !st.live_trading}
            className={`px-3 py-1.5 rounded text-xs font-bold disabled:opacity-40 ${
              st.live_trading ? 'bg-down text-white hover:bg-down' : 'bg-secondary text-muted hover:bg-secondary/70'}`}>
            {st.live_trading ? 'LIVE ✓ (→paper)' : st.live_available ? 'Go LIVE 💵' : 'LIVE (no keys)'}
          </button>
          <button onClick={() => post('/spreads/toggle', { enabled: !st.enabled })}
            className={`px-4 py-1.5 rounded font-bold ${st.enabled ? 'bg-up text-black' : 'bg-raised text-white'}`}>
            {st.enabled ? 'ON' : 'OFF'}
          </button>
        </div>
      </div>

      {/* IV Rank gate — the edge */}
      <div className="glass p-4 rounded-xl">
        <div className="eyebrow mb-2">IV Rank gate — only sell when premium is rich (≥ {p.ivr_min})</div>
        <div className="grid grid-cols-2 gap-3">
          {Object.entries(st.ivr || {}).map(([sym, v]) => {
            const rank = v?.rank;
            const rich = rank != null && rank >= (p.ivr_min ?? 30);
            return (
              <div key={sym} className="text-xs font-mono">
                <span className="text-fuchsia-300 font-bold">{sym}</span> ({v?.index || '—'}):{' '}
                {rank == null ? <span className="text-muted">no data (runs when ON)</span> : (
                  <span className={rich ? 'text-up font-bold' : 'text-muted'}>
                    IVR {rank.toFixed(0)} {rich ? '✓ rich — sell' : '· too cheap, stand aside'}
                  </span>
                )}
                {v && <span className="text-muted"> · {v.current} [{v.low}–{v.high}]</span>}
              </div>
            );
          })}
        </div>
        <div className={`mt-2 text-[11px] ${st.in_window ? 'text-up' : 'text-warn'}`}>
          {st.in_window ? '🟢 in entry window' : '🌙 outside entry window'} · {st.note || 'idle'}
        </div>
      </div>

      {/* stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <Stat label="Day P&L" val={`$${(st.day_pnl ?? 0).toFixed(0)}`} color={pnlColor} />
        <Stat label="Win rate" val={st.win_rate != null ? `${Math.round(st.win_rate * 100)}%` : '—'} />
        <Stat label="Closed" val={st.closed_today ?? 0} />
        <Stat label="Open" val={st.open_count ?? 0} />
      </div>

      {/* params */}
      <div className="glass p-4 rounded-xl">
        <div className="eyebrow mb-2">Strategy params</div>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          <Step label="IVR min" v={p.ivr_min} min={0} max={100} step={5} onSet={(x) => post('/spreads/config', { ivr_min: x })} />
          <Step label="Short Δ" v={p.short_delta} min={0.10} max={0.45} step={0.05} dp={2} onSet={(x) => post('/spreads/config', { short_delta: x })} />
          <Step label="Width $" v={p.width} min={1} max={20} step={1} onSet={(x) => post('/spreads/config', { width: x })} />
          <Step label="Target DTE" v={p.target_dte} min={7} max={60} step={1} onSet={(x) => post('/spreads/config', { target_dte: x })} />
          <Step label="Take %" v={p.profit_take_frac} min={0.25} max={0.9} step={0.05} dp={2} onSet={(x) => post('/spreads/config', { profit_take_frac: x })} />
          <Step label="Max loss $" v={p.max_loss_per_spread_usd} min={50} max={800} step={25} onSet={(x) => post('/spreads/config', { max_loss_per_spread_usd: x })} />
        </div>
        <div className="mt-2 text-[11px] text-muted">
          A 5-wide index spread risks ~$400 (~30% of a $1.3k acct). Lower Max loss → fewer/no trades; the account is small for index spreads.
        </div>
      </div>

      {/* open spreads */}
      <div className="glass p-4 rounded-xl">
        <div className="eyebrow mb-2">Open spreads</div>
        {(!st.open_spreads || !st.open_spreads.length)
          ? <div className="text-xs text-muted">none</div>
          : (
            <div className="space-y-1 text-xs font-mono">
              {st.open_spreads.map((s, i) => (
                <div key={i} className="flex justify-between border-b border-line pb-1">
                  <span className="text-fuchsia-300">{s.underlying} {s.short_strike}/{s.long_strike}p</span>
                  <span>exp {s.expiration} ({s.dte}d)</span>
                  <span>x{s.qty} · credit ${s.entry_credit?.toFixed(2)}</span>
                  <span className="text-muted">peak {Math.round((s.peak_capture || 0) * 100)}%</span>
                </div>
              ))}
            </div>
          )}
      </div>

      {/* trades */}
      <div className="glass p-4 rounded-xl">
        <div className="eyebrow mb-2">Recent spread trades</div>
        {(!trades.length)
          ? <div className="text-xs text-muted">no closed spreads yet</div>
          : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs font-mono">
                <thead className="text-muted">
                  <tr className="text-left"><th>Time</th><th>Spread</th><th>Exp</th><th>Credit</th><th>P&L</th><th>Exit</th></tr>
                </thead>
                <tbody>
                  {trades.map((t, i) => (
                    <tr key={i} className="border-t border-line">
                      <td>{t.ts ? new Date(t.ts).toLocaleString([], { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'}</td>
                      <td className="text-fuchsia-300">{t.underlying} {t.short_strike}/{t.long_strike}p</td>
                      <td>{t.expiration}</td>
                      <td>${t.entry_credit?.toFixed(2)}</td>
                      <td className={(t.pnl ?? 0) >= 0 ? 'text-up' : 'text-down'}>{(t.pnl ?? 0) >= 0 ? '+' : ''}{Math.round(t.pnl ?? 0)}</td>
                      <td className="text-muted">{t.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
      </div>
    </div>
  );
}

const Stat = ({ label, val, color }) => (
  <div className="glass p-3 rounded-lg text-center">
    <div className="text-[10px] text-muted uppercase">{label}</div>
    <div className={`text-lg font-bold ${color || 'text-ink'}`}>{val}</div>
  </div>
);

function Step({ label, v, min, max, step, dp = 0, onSet }) {
  const clamp = (x) => Math.min(max, Math.max(min, +x.toFixed(dp)));
  const cur = v == null ? min : v;
  return (
    <div>
      <div className="text-[10px] text-muted uppercase mb-1">{label}</div>
      <div className="flex items-center gap-1">
        <button onClick={() => onSet(clamp(cur - step))} disabled={cur <= min}
          className="px-2 py-0.5 rounded bg-raised text-ink text-sm disabled:opacity-30">−</button>
        <span className="font-mono text-fuchsia-300 text-sm w-14 text-center">{dp ? cur.toFixed(dp) : cur}</span>
        <button onClick={() => onSet(clamp(cur + step))} disabled={cur >= max}
          className="px-2 py-0.5 rounded bg-raised text-ink text-sm disabled:opacity-30">+</button>
      </div>
    </div>
  );
}
