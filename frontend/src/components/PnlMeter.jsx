// Live P&L thermometer for an open position — sits above its chart.
// A horizontal diverging meter centered at breakeven (0%): the fill grows right
// (green) into profit or left (red) into loss, the color deepens with magnitude,
// and tick marks pin the session HIGH-water (peak) and LOW-water (trough) reached.
// A moving thumb marks the current P&L. Data comes from status.open_positions.
//
// Diverging encoding: two poles (green profit / red loss) + neutral breakeven
// center — never a rainbow. Magnitude shown by fill length AND color depth.

// Meter spans ±SCALE% around breakeven; values clamp to the ends.
const SCALE = 60;   // full-scale deflection = ±60% option P&L

function clampPct(v) {
  if (v == null) return 0;
  return Math.max(-SCALE, Math.min(SCALE, v));
}
// map a signed pct (−SCALE..+SCALE) to a 0..100 left-offset on the track
function toPos(v) {
  return ((clampPct(v) + SCALE) / (2 * SCALE)) * 100;
}
// color deepens with magnitude; green for profit, red for loss, slate at flat
function pnlColor(v) {
  if (v == null) return '#64748b';
  const mag = Math.min(1, Math.abs(v) / 40);           // saturate by ~40%
  if (v > 0.2) return `rgba(22,163,74,${0.55 + 0.45 * mag})`;   // green
  if (v < -0.2) return `rgba(220,38,38,${0.55 + 0.45 * mag})`;  // red
  return '#64748b';
}

export default function PnlMeter({ position }) {
  if (!position) return null;
  const { direction, pnl_pct, pnl_usd, peak_pct, trough_pct, peak_usd, trough_usd } = position;
  const cur = pnl_pct ?? 0;
  const curPos = toPos(cur);
  const bePos = toPos(0);               // breakeven center
  const peakPos = toPos(peak_pct);
  const troughPos = toPos(trough_pct);
  const col = pnlColor(cur);
  const up = cur >= 0;

  // fill spans from breakeven to the current thumb (right for profit, left for loss)
  const fillLeft = Math.min(curPos, bePos);
  const fillWidth = Math.abs(curPos - bePos);

  return (
    <div className="glass rounded-lg px-3 py-2 mb-1.5">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[11px] font-bold">
          <span className={direction === 'call' ? 'text-green-400' : 'text-red-400'}>
            {direction?.toUpperCase()}
          </span>
          <span className="text-gray-400 ml-1.5">live P&amp;L</span>
        </span>
        <span className="text-sm font-bold font-mono" style={{ color: col }}>
          {pnl_usd != null ? `${up ? '+' : ''}$${Math.abs(pnl_usd)}` : '—'}
          <span className="text-[11px] ml-1.5 opacity-80">
            {pnl_pct != null ? `${cur > 0 ? '+' : ''}${cur}%` : ''}
          </span>
        </span>
      </div>

      {/* the meter track */}
      <div className="relative h-3 rounded-full bg-slate-800/80 overflow-visible">
        {/* breakeven center line */}
        <div className="absolute top-[-2px] bottom-[-2px] w-px bg-slate-500" style={{ left: `${bePos}%` }} />

        {/* current-P&L fill from breakeven → thumb */}
        <div
          className="absolute top-0 bottom-0 rounded-full transition-all duration-300"
          style={{ left: `${fillLeft}%`, width: `${fillWidth}%`, background: col }}
        />

        {/* HIGH-water mark (peak) */}
        {peak_pct != null && (
          <Mark pos={peakPos} color="#4ade80" label={`▲ +${Math.max(peak_pct, 0)}%`}
                title={`peak +$${Math.max(peak_usd ?? 0, 0)}`} above />
        )}
        {/* LOW-water mark (trough) */}
        {trough_pct != null && trough_pct < 0 && (
          <Mark pos={troughPos} color="#f87171" label={`▼ ${trough_pct}%`}
                title={`worst -$${Math.abs(trough_usd ?? 0)}`} />
        )}

        {/* current thumb */}
        <div
          className="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 w-3.5 h-3.5 rounded-full border-2 border-white shadow transition-all duration-300"
          style={{ left: `${curPos}%`, background: col }}
        />
      </div>

      {/* scale ends */}
      <div className="flex justify-between text-[9px] text-slate-600 font-mono mt-0.5">
        <span>-{SCALE}%</span><span>breakeven</span><span>+{SCALE}%</span>
      </div>
    </div>
  );
}

// a labeled water-mark tick on the track
function Mark({ pos, color, label, title, above = false }) {
  return (
    <div className="absolute" style={{ left: `${pos}%`, top: 0, bottom: 0 }} title={title}>
      <div className="absolute top-[-3px] bottom-[-3px] w-[2px] -translate-x-1/2" style={{ background: color }} />
      <div
        className={`absolute -translate-x-1/2 text-[8px] font-mono whitespace-nowrap ${above ? 'bottom-full mb-0.5' : 'top-full mt-0.5'}`}
        style={{ color }}
      >
        {label}
      </div>
    </div>
  );
}
