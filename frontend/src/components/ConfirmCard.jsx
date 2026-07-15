// User-confirmation prompt for scalper entry/exit signals (manual mode).
// When autonomous is OFF, the engine parks a signal with the real strike / side /
// size / price and a short expiry window; this card pops with a live countdown
// ring and Confirm / Skip. Missed → it auto-expires (a stale scalp must not fill
// late). Risk stops are NOT shown here — they always auto-fire on the backend.
//
// Polls /scalper/status at 1s while mounted so the countdown is live even though
// the parent panel polls slower.
import { useEffect, useRef, useState } from 'react';

export default function ConfirmCard({ apiBaseUrl, confirmWindowS = 8, onAction }) {
  const [signals, setSignals] = useState([]);
  const busy = useRef(new Set());   // signal ids mid-request (avoid double-fire)

  useEffect(() => {
    let stop = false;
    const pull = async () => {
      try {
        const r = await fetch(`${apiBaseUrl}/scalper/status`);
        const d = await r.json();
        if (!stop) setSignals(d.pending_signals || []);
      } catch { /* keep last */ }
    };
    pull();
    const id = setInterval(pull, 1000);
    return () => { stop = true; clearInterval(id); };
  }, [apiBaseUrl]);

  const act = async (sig, action) => {
    if (busy.current.has(sig.id)) return;
    busy.current.add(sig.id);
    // optimistic: remove from view immediately
    setSignals((s) => s.filter((x) => x.id !== sig.id));
    try {
      await fetch(`${apiBaseUrl}/scalper/confirm`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ signal_id: sig.id, action }),
      });
      onAction?.();
    } finally {
      busy.current.delete(sig.id);
    }
  };

  if (!signals.length) return null;

  return (
    <div className="space-y-2">
      {signals.map((sig) => {
        const isEntry = sig.kind === 'entry';
        const isCall = sig.direction === 'call';
        const pct = Math.max(0, Math.min(1, (sig.seconds_left ?? 0) / confirmWindowS));
        return (
          <div
            key={sig.id}
            className={`glass rounded-xl p-3 border ${isCall ? 'border-green-500/50' : 'border-red-500/50'} flex items-center gap-3`}
          >
            {/* countdown ring */}
            <CountdownRing pct={pct} seconds={sig.seconds_left ?? 0} color={isCall ? '#16a34a' : '#dc2626'} />
            {/* signal detail */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 text-sm font-bold">
                <span className={isCall ? 'text-green-400' : 'text-red-400'}>
                  {isEntry ? 'ENTER' : 'EXIT'} {sig.direction?.toUpperCase()}
                </span>
                <span className="text-gray-300">{sig.symbol}</span>
                {sig.strike != null && (
                  <span className="text-gray-400 font-mono text-xs">${sig.strike} {isCall ? 'C' : 'P'}</span>
                )}
                <span className="text-[10px] uppercase tracking-wide text-gray-500">{sig.strategy || sig.reason}</span>
              </div>
              <div className="text-xs text-gray-400 font-mono mt-0.5">
                {isEntry
                  ? `${sig.qty} × @ $${sig.price?.toFixed?.(2) ?? sig.price}  (~$${(sig.qty * sig.price * 100).toFixed(0)})`
                  : `close ${sig.qty} ×  ${sig.pnl_pct != null ? `(${sig.pnl_pct > 0 ? '+' : ''}${sig.pnl_pct}%)` : ''}  · ${sig.reason}`}
              </div>
            </div>
            {/* actions */}
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={() => act(sig, 'skip')}
                className="px-3 py-1.5 rounded text-xs font-medium bg-gray-700 text-gray-200 hover:bg-gray-600"
              >Skip</button>
              <button
                onClick={() => act(sig, 'confirm')}
                className={`px-4 py-1.5 rounded text-xs font-bold text-black ${isCall ? 'bg-green-500 hover:bg-green-400' : 'bg-red-500 hover:bg-red-400'}`}
              >Confirm</button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function CountdownRing({ pct, seconds, color }) {
  const R = 16, C = 2 * Math.PI * R;
  return (
    <div className="relative w-11 h-11 shrink-0">
      <svg width="44" height="44" className="-rotate-90">
        <circle cx="22" cy="22" r={R} fill="none" stroke="#1e293b" strokeWidth="3" />
        <circle
          cx="22" cy="22" r={R} fill="none" stroke={color} strokeWidth="3" strokeLinecap="round"
          strokeDasharray={C} strokeDashoffset={C * (1 - pct)}
          style={{ transition: 'stroke-dashoffset 1s linear' }}
        />
      </svg>
      <div className="absolute inset-0 flex items-center justify-center text-xs font-mono font-bold text-gray-200">
        {seconds}
      </div>
    </div>
  );
}
