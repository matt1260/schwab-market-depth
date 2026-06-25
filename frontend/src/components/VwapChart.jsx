// Live price + session-mean (VWAP-ish) + ±2σ band chart for the scalper.
// NOTE: the "mean" is a volume-FREE session-anchored average (no volume feed in
// the sandbox), so it's labeled "Session mean (VWAP-ish)", not true VWAP.
import { useEffect, useRef, useState } from 'react';
import { createChart } from 'lightweight-charts';

export default function VwapChart({ apiBaseUrl, symbol, height = 320 }) {
  const el = useRef(null);
  const chart = useRef(null);
  const series = useRef({});
  const [empty, setEmpty] = useState(true);

  useEffect(() => {
    if (!el.current || chart.current) return;
    chart.current = createChart(el.current, {
      width: el.current.clientWidth, height,
      layout: { background: { color: '#0a0e17' }, textColor: '#9ca3af', fontSize: 11 },
      grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
      rightPriceScale: { borderColor: '#334155' },
      timeScale: { timeVisible: true, secondsVisible: true, borderColor: '#334155',
        tickMarkFormatter: (t) => new Date(t * 1000).toLocaleTimeString() },
      crosshair: { mode: 0 },
    });
    series.current.price = chart.current.addLineSeries({ color: '#60a5fa', lineWidth: 2, title: 'Price' });
    series.current.mean = chart.current.addLineSeries({ color: '#f59e0b', lineWidth: 1, title: 'Mean (VWAP-ish)' });
    series.current.upper = chart.current.addLineSeries({ color: '#475569', lineWidth: 1, lineStyle: 2, title: '+2σ' });
    series.current.lower = chart.current.addLineSeries({ color: '#475569', lineWidth: 1, lineStyle: 2, title: '-2σ' });
    const resize = () => chart.current && el.current && chart.current.applyOptions({ width: el.current.clientWidth });
    const ro = new ResizeObserver(resize); ro.observe(el.current);
    return () => { ro.disconnect(); chart.current?.remove(); chart.current = null; series.current = {}; };
  }, [height]);

  useEffect(() => {
    if (!chart.current || !symbol) return;
    let stop = false;
    const pull = async () => {
      try {
        const r = await fetch(`${apiBaseUrl}/scalper/chart/${symbol}`);
        const d = await r.json();
        const s = (d.series || []).filter((p) => p.price != null);
        if (stop || !s.length) { setEmpty(!s.length); return; }
        setEmpty(false);
        // de-dupe timestamps (lightweight-charts requires strictly ascending)
        const seen = new Set();
        const clean = [];
        for (const p of s) { if (!seen.has(p.t)) { seen.add(p.t); clean.push(p); } }
        series.current.price.setData(clean.map((p) => ({ time: p.t, value: p.price })));
        series.current.mean.setData(clean.filter((p) => p.mean != null).map((p) => ({ time: p.t, value: p.mean })));
        series.current.upper.setData(clean.filter((p) => p.upper != null).map((p) => ({ time: p.t, value: p.upper })));
        series.current.lower.setData(clean.filter((p) => p.lower != null).map((p) => ({ time: p.t, value: p.lower })));

        // entry/exit markers from trade history — ONLY within the chart's time
        // window, else lightweight-charts clamps off-window markers onto the last
        // bar (the "C/P stuck on current price" artifact).
        try {
          const tMin = clean.length ? clean[0].t : 0;
          const tMax = clean.length ? clean[clean.length - 1].t : 0;
          const inWin = (t) => t && t >= tMin && t <= tMax;
          const mr = await fetch(`${apiBaseUrl}/scalper/markers/${symbol}`);
          const md = await mr.json();
          const marks = [];
          for (const m of (md.markers || [])) {
            if (inWin(m.entry_t)) marks.push({
              time: m.entry_t, position: 'belowBar',
              color: m.direction === 'call' ? '#22c55e' : '#ef4444',
              shape: 'arrowUp', text: (m.direction === 'call' ? 'C' : 'P'),
            });
            if (inWin(m.exit_t)) marks.push({
              time: m.exit_t, position: 'aboveBar',
              color: (m.pnl ?? 0) >= 0 ? '#22c55e' : '#ef4444',
              shape: 'arrowDown', text: `${(m.pnl ?? 0) >= 0 ? '+' : ''}${Math.round(m.pnl ?? 0)} ${m.reason || ''}`,
            });
          }
          // markers must be sorted ascending by time + de-duped
          marks.sort((a, b) => a.time - b.time);
          const seenT = new Set(); const uniq = [];
          for (const mk of marks) { const k = mk.time + mk.position; if (!seenT.has(k)) { seenT.add(k); uniq.push(mk); } }
          series.current.price.setMarkers(uniq);
        } catch { /* ignore */ }
      } catch { /* ignore */ }
    };
    pull();
    const id = setInterval(pull, 1500);
    return () => { stop = true; clearInterval(id); };
  }, [symbol, apiBaseUrl]);

  return (
    <div className="relative w-full">
      <div ref={el} className="w-full rounded border border-slate-800 bg-[#0a0e17]" style={{ height }}>
        {empty && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-text-secondary">
            Warming up {symbol} chart… (mean/bands appear after ~3 min of samples)
          </div>
        )}
      </div>
      <div className="mt-1 text-[10px] text-text-secondary">
        <span className="text-blue-400">━ price</span> · <span className="text-amber-500">━ session mean (VWAP-ish, volume-free)</span> · <span className="text-slate-400">┈ ±2σ bands</span>
      </div>
    </div>
  );
}
