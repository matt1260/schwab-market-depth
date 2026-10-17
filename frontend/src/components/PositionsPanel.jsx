// Live open-position panel + options ladder for the scalper cockpit.
//  * PositionsPanel — each open scalp with live P&L $/%, peak, held time, the
//    active stop, and a big Close Now button (POST /scalper/close).
//  * OptionsLadder — the ATM±2 strikes the engine is currently eyeing per symbol
//    (side = the EMA regime), with live mid/bid/ask and the size it would take.
// Both read from /scalper/status (polled by the parent) — passed in as props.

export function PositionsPanel({ apiBaseUrl, positions = [], onClose }) {
  if (!positions.length) {
    return (
      <div className="glass p-3 rounded-xl text-xs text-muted">
        No open positions.
      </div>
    );
  }

  const closeNow = async (p) => {
    await fetch(`${apiBaseUrl}/scalper/close`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scalp_key: p.scalp_key || p.occ }),
    });
    onClose?.();
  };

  return (
    <div className="space-y-2">
      {positions.map((p) => {
        const isCall = p.direction === 'call';
        const pnlUp = (p.pnl_usd ?? 0) >= 0;
        return (
          <div
            key={p.scalp_key || p.occ}
            className={`glass rounded-xl p-3 border ${pnlUp ? 'border-green-500/40' : 'border-red-500/40'} flex items-center gap-3`}
          >
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 text-sm">
                <span className={`font-bold ${isCall ? 'text-up' : 'text-down'}`}>
                  {p.direction?.toUpperCase()}
                </span>
                <span className="text-muted font-medium">{p.underlying}</span>
                <span className="text-faint font-mono text-xs truncate">{p.occ}</span>
                <span className="text-[10px] uppercase tracking-wide text-faint">{p.strategy}</span>
              </div>
              <div className="flex items-center gap-3 mt-1 text-xs font-mono text-muted">
                <span>×{p.qty}</span>
                <span>in ${p.entry_mid}</span>
                {p.mid != null && <span>now ${p.mid}</span>}
                {p.peak_pct != null && <span className="text-faint">peak {p.peak_pct > 0 ? '+' : ''}{p.peak_pct}%</span>}
                <span className="text-faint">{fmtHeld(p.held_s)}</span>
                {p.stop_pct != null && <span className="text-faint">stop {p.stop_pct}%</span>}
              </div>
            </div>
            {/* live P&L */}
            <div className={`text-right shrink-0 ${pnlUp ? 'text-up' : 'text-down'}`}>
              <div className="text-lg font-bold font-mono leading-none">
                {p.pnl_usd != null ? `${pnlUp ? '+' : ''}$${Math.abs(p.pnl_usd)}` : '—'}
              </div>
              <div className="text-xs font-mono">
                {p.pnl_pct != null ? `${p.pnl_pct > 0 ? '+' : ''}${p.pnl_pct}%` : ''}
              </div>
            </div>
            <button
              onClick={() => closeNow(p)}
              className="px-4 py-2 rounded font-bold text-xs bg-white text-black hover:bg-gray-200 shrink-0"
            >
              Close Now
            </button>
          </div>
        );
      })}
    </div>
  );
}

export function OptionsLadder({ ladder = {} }) {
  const syms = Object.keys(ladder);
  if (!syms.length) return null;
  return (
    <div className="glass p-3 rounded-xl">
      <div className="eyebrow mb-2">Options ladder — strikes the algo is selecting (ATM ± 2)</div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {syms.map((sym) => {
          const l = ladder[sym];
          const isCall = l.side === 'call';
          return (
            <div key={sym}>
              <div className="flex items-center gap-2 mb-1 text-[11px]">
                <span className="font-bold text-accent">{sym}</span>
                <span className="text-faint font-mono">spot ${l.spot}</span>
                <span className={`px-1.5 py-0.5 rounded ${isCall ? 'bg-up/15 text-up' : 'bg-down/15 text-down'}`}>
                  eyeing {l.side?.toUpperCase()}
                </span>
              </div>
              <table className="w-full text-xs font-mono">
                <thead className="text-faint">
                  <tr className="text-left">
                    <th className="font-normal py-0.5">Strike</th>
                    <th className="font-normal">Bid</th>
                    <th className="font-normal">Mid</th>
                    <th className="font-normal">Ask</th>
                    <th className="font-normal text-right">Qty</th>
                  </tr>
                </thead>
                <tbody>
                  {(l.strikes || []).map((s) => (
                    <tr key={s.occ} className={s.atm ? (isCall ? 'text-up' : 'text-down') : 'text-muted'}>
                      <td className="py-0.5">
                        {s.atm && <span className="mr-1 text-[9px] text-faint">ATM</span>}
                        ${s.strike}{isCall ? 'C' : 'P'}
                      </td>
                      <td>{s.bid != null ? s.bid : '—'}</td>
                      <td className="font-bold">{s.mid != null ? s.mid : '—'}</td>
                      <td>{s.ask != null ? s.ask : '—'}</td>
                      <td className="text-right">{s.qty || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function fmtHeld(s) {
  if (s == null) return '';
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m ? `${m}m${sec}s` : `${sec}s`;
}
