// Premium-selling scanner — ranked put-credit-spread opportunities across the universe.
import { useState, useEffect, useCallback } from 'react';

export default function ScannerPanel({ apiBaseUrl }) {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const [scanning, setScanning] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${apiBaseUrl}/scanner/status`);
      setSt(await r.json()); setErr(null);
    } catch (e) { setErr(e.message); }
  }, [apiBaseUrl]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 10000);
    return () => clearInterval(id);
  }, [refresh]);

  const forceScan = async () => {
    setScanning(true);
    try { await fetch(`${apiBaseUrl}/scanner/scan`, { method: 'POST' }); await refresh(); }
    finally { setScanning(false); }
  };

  if (err) return <div className="glass p-6 rounded-xl text-down">Scanner API error: {err}</div>;
  if (!st) return <div className="text-muted p-6">Loading scanner…</div>;
  if (!st.available) return <div className="glass p-6 rounded-xl text-warn">Scanner not initialized — check API logs.</div>;

  const ops = st.opportunities || [];
  const tradeable = ops.filter((o) => o.tradeable);

  return (
    <div className="space-y-4">
      {/* NEW: 0DTE credit spreads ranked by probability of expiring OTM */}
      <ZdteSection apiBaseUrl={apiBaseUrl} />

      <div className="panel p-4 flex items-center justify-between">
        <div>
          <div className="font-display font-semibold text-ink">Premium Scanner <span className="eyebrow ml-1">30–45 DTE · IV-rank</span></div>
          <div className="text-xs text-muted mt-0.5">
            {st.universe_size} names · ranks richest put-credit-spread premium (IV vs realized + IV-rank) ·
            last scan {st.last_scan_age_s != null ? `${Math.round(st.last_scan_age_s)}s ago` : '—'}
          </div>
        </div>
        <button onClick={forceScan} disabled={scanning} className="btn disabled:opacity-40">
          {scanning ? 'Scanning…' : 'Scan now'}
        </button>
      </div>

      {/* top tradeable */}
      <div className="glass p-4 rounded-xl">
        <div className="eyebrow mb-2">🎯 Top tradeable opportunities ({tradeable.length})</div>
        {!tradeable.length
          ? <div className="text-xs text-muted">none clear all gates right now (IVR / liquidity / earnings / credit)</div>
          : (
            <div className="space-y-1 text-xs font-mono">
              {tradeable.slice(0, 8).map((o, i) => {
                const sp = o.spread || {};
                const sideTag = o.side === 'call' ? 'C' : 'P';
                return (
                  <div key={i} className="grid grid-cols-[48px_36px_70px_56px_92px_64px_1fr] gap-x-2 items-center border-b border-line py-0.5">
                    <span className="text-accent font-bold">{o.symbol}</span>
                    <span className="text-faint">{o.is_etf ? 'ETF' : 'stk'}</span>
                    <span className={o.side === 'call' ? 'text-down' : 'text-up'}>{o.trend === 'uptrend' ? '↑ put' : o.trend === 'downtrend' ? '↓ call' : o.side}</span>
                    <span className="text-up">IVR {o.ivr}</span>
                    <span className="text-muted">{sp.short_strike}/{sp.long_strike}{sideTag}</span>
                    <span className="text-muted">${sp.credit} cr</span>
                    <span className="text-faint">{sp.dte}d · liq {sp.short_spread_pct}% · sc {o.score}</span>
                  </div>
                );
              })}
            </div>
          )}
      </div>

      {/* full universe ranking */}
      <div className="glass p-4 rounded-xl">
        <div className="eyebrow mb-2">Full universe (ranked)</div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs font-mono">
            <thead className="text-muted text-left">
              <tr><th>Sym</th><th>Type</th><th>Trend</th><th>IVR</th><th>IV/RV</th><th>Spread</th><th>DTE</th><th>Credit</th><th>Earnings</th><th>OK?</th></tr>
            </thead>
            <tbody>
              {ops.map((o, i) => {
                const sp = o.spread || {};
                return (
                  <tr key={i} className={`border-t border-line ${o.tradeable ? '' : 'opacity-50'}`}>
                    <td className="text-accent">{o.symbol}</td>
                    <td className="text-faint">{o.is_etf ? 'ETF' : 'stk'}</td>
                    <td className={o.side === 'call' ? 'text-down' : o.side === 'put' ? 'text-up' : 'text-faint'}>{o.trend === 'uptrend' ? '↑ put' : o.trend === 'downtrend' ? '↓ call' : '—'}</td>
                    <td className={o.ivr != null && o.ivr >= 30 ? 'text-up' : 'text-muted'}>{o.ivr ?? '—'}</td>
                    <td className="text-faint" title={`IV ${o.atm_iv} vs realized ${o.realized_vol ?? '—'}`}>
                      {o.atm_iv}/{o.realized_vol ?? '—'}
                    </td>
                    <td className="text-muted">{sp.short_strike ? `${sp.short_strike}/${sp.long_strike}${o.side === 'call' ? 'C' : 'P'}` : '—'}</td>
                    <td className="text-faint">{sp.dte != null ? `${sp.dte}d` : '—'}</td>
                    <td className="text-muted">{sp.credit != null ? `$${sp.credit}` : '—'}</td>
                    <td className={o.earnings_ok ? 'text-faint' : 'text-down'}>{o.next_earnings || '—'}</td>
                    <td>{o.tradeable ? <span className="text-up">✓</span> : <span className="text-faint">·</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="mt-2 text-[11px] text-muted">
          IV/RV = implied vol vs realized vol — the edge exists only when IV &gt; RV (rich premium). IVR 0 means IV ≤ RV (cheap — skip, e.g. QQQ IV 0.25 &lt; RV 0.34). Dimmed rows fail a gate. All spreads ~30-45 DTE.
        </div>
      </div>
    </div>
  );
}

// ── 0DTE credit-spread scanner, ranked by probability of expiring OTM ──
// P(OTM) = 1 − |delta of the short strike| = the seller's probability of profit.
function ZdteSection({ apiBaseUrl }) {
  const [st, setSt] = useState(null);
  const [scanning, setScanning] = useState(false);

  const refresh = useCallback(async () => {
    try { const r = await fetch(`${apiBaseUrl}/zdte/status`); setSt(await r.json()); } catch { /* */ }
  }, [apiBaseUrl]);
  useEffect(() => { refresh(); }, [refresh]);

  const scan = async () => {
    setScanning(true);
    try { await fetch(`${apiBaseUrl}/zdte/scan`, { method: 'POST' }); await refresh(); }
    finally { setScanning(false); }
  };
  const setPop = async (v) => {
    await fetch(`${apiBaseUrl}/zdte/config`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pop_min: v }),
    });
    await refresh();
  };

  const ops = st?.opportunities || [];
  const p = st?.params || {};
  const popPct = p.pop_min != null ? Math.round(p.pop_min * 100) : 75;

  return (
    <div className="panel p-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <div className="font-display font-semibold text-ink">
            0DTE Credit Spreads <span className="eyebrow ml-1">by probability of expiring OTM</span>
          </div>
          <div className="text-xs text-muted mt-0.5">
            Ranks same-day defined-risk spreads by P(OTM) = 1 − |delta| (the seller's win probability) ·
            last scan {st?.last_scan_age_s != null ? `${Math.round(st.last_scan_age_s)}s ago` : '—'}
          </div>
        </div>
        <button onClick={scan} disabled={scanning} className="btn disabled:opacity-40">
          {scanning ? 'Scanning…' : 'Scan 0DTE'}
        </button>
      </div>

      {/* min-P(OTM) selector */}
      <div className="flex items-center gap-2 mb-3">
        <span className="eyebrow">Min win prob</span>
        {[70, 75, 80, 85, 90].map((v) => (
          <button key={v} onClick={() => setPop(v / 100)}
            className={`num text-[11px] px-2 py-0.5 rounded ${popPct === v ? 'bg-accent text-base font-bold' : 'bg-raised text-muted ring-1 ring-line hover:text-ink'}`}>
            {v}%
          </button>
        ))}
      </div>

      {!ops.length ? (
        <div className="text-xs text-muted">
          {st?.note || 'Click “Scan 0DTE” to rank today’s expiries. (0DTE needs a same-day expiration — SPY/QQQ/IWM/DIA.)'}
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="eyebrow text-left [&>th]:pb-1.5 [&>th]:font-normal">
                <th>Sym</th><th>Side</th><th className="num text-right">P(OTM)</th>
                <th className="num">Strikes</th><th className="num text-right">Credit</th>
                <th className="num text-right">Cr/W</th><th className="num text-right">Max loss</th>
                <th className="num text-right">B/E</th><th className="num text-right">δ</th>
              </tr>
            </thead>
            <tbody className="num">
              {ops.map((o, i) => (
                <tr key={i} className="border-t border-line [&>td]:py-1">
                  <td className="text-accent font-semibold">{o.symbol}</td>
                  <td className={o.side === 'call' ? 'text-down' : 'text-up'}>
                    {o.side === 'call' ? 'Call' : 'Put'}
                  </td>
                  <td className="text-right text-up font-semibold">{o.pop_pct}%</td>
                  <td className="text-muted">{o.short_strike}/{o.long_strike}</td>
                  <td className="text-right text-ink">${o.credit.toFixed(2)}</td>
                  <td className="text-right text-muted">{o.credit_pct_of_width}%</td>
                  <td className="text-right text-down">${o.max_loss}</td>
                  <td className="text-right text-faint">{o.breakeven}</td>
                  <td className="text-right text-faint">{o.short_delta}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-2 text-[11px] text-muted">
            Short strike is the closest-to-money strike still meeting the win prob (max credit for that probability); long leg ${p.width} beyond for defined risk.
            <span className="text-warn"> Higher P(OTM) = safer but less credit.</span> Scanner only — it does not trade these.
          </div>
        </div>
      )}
    </div>
  );
}
