// 1-min candlestick chart with 9/21/50 EMA overlays + 9x21 crossover markers.
// Data: GET /scalper/candles/{symbol} — closed 1-min OHLC + aligned EMAs + the
// forming (live) bar + cross markers, all on the closed-bar basis the engine
// trades on. Candles own green/red (up/down); the three EMAs get distinct,
// non-status hues (amber 9 / blue 21 / violet 50) so identity is never
// color-vs-green/red-ambiguous. Matches the app's dark theme (#0a0e17).
import { useEffect, useRef, useState } from 'react';
import { createChart } from 'lightweight-charts';

const EMA9_COLOR = '#eab308';   // amber — fast (leading) line
const EMA21_COLOR = '#3b82f6';  // blue  — mid line
const EMA50_COLOR = '#a855f7';  // violet — slow / trend line

export default function CandleChart({ apiBaseUrl, symbol, height = 340 }) {
  const el = useRef(null);
  const chart = useRef(null);
  const series = useRef({});
  const [empty, setEmpty] = useState(true);
  const [regime, setRegime] = useState(null);

  // Build the chart once.
  useEffect(() => {
    if (!el.current || chart.current) return;
    chart.current = createChart(el.current, {
      width: el.current.clientWidth, height,
      layout: { background: { color: '#0a0e17' }, textColor: '#9ca3af', fontSize: 11 },
      grid: { vertLines: { color: '#161c2b' }, horzLines: { color: '#161c2b' } },
      rightPriceScale: { borderColor: '#334155' },
      timeScale: {
        timeVisible: true, secondsVisible: false, borderColor: '#334155',
        tickMarkFormatter: (t) => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      },
      crosshair: { mode: 1 },
    });
    series.current.candles = chart.current.addCandlestickSeries({
      upColor: '#16a34a', downColor: '#dc2626',
      borderUpColor: '#16a34a', borderDownColor: '#dc2626',
      wickUpColor: '#16a34a', wickDownColor: '#dc2626',
    });
    series.current.ema9 = chart.current.addLineSeries({ color: EMA9_COLOR, lineWidth: 2, title: 'EMA 9', priceLineVisible: false, lastValueVisible: false });
    series.current.ema21 = chart.current.addLineSeries({ color: EMA21_COLOR, lineWidth: 2, title: 'EMA 21', priceLineVisible: false, lastValueVisible: false });
    series.current.ema50 = chart.current.addLineSeries({ color: EMA50_COLOR, lineWidth: 2, title: 'EMA 50', priceLineVisible: false, lastValueVisible: false });
    const resize = () => chart.current && el.current && chart.current.applyOptions({ width: el.current.clientWidth });
    const ro = new ResizeObserver(resize); ro.observe(el.current);
    return () => { ro.disconnect(); chart.current?.remove(); chart.current = null; series.current = {}; };
  }, [height]);

  // Poll the candles endpoint and repaint.
  useEffect(() => {
    if (!chart.current || !symbol) return;
    let stop = false;
    const pull = async () => {
      try {
        const r = await fetch(`${apiBaseUrl}/scalper/candles/${symbol}`);
        const d = await r.json();
        const bars = d.bars || [];
        if (stop) return;
        if (!bars.length) { setEmpty(true); return; }
        setEmpty(false);
        setRegime(d.ema_now?.regime ?? null);

        // Candles: closed bars + the forming live bar, de-duped + strictly ascending.
        const rows = [...bars];
        if (d.live && (!rows.length || d.live.t > rows[rows.length - 1].t)) rows.push(d.live);
        const seen = new Set();
        const candleData = [];
        for (const b of rows) {
          if (seen.has(b.t)) continue;
          seen.add(b.t);
          candleData.push({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c });
        }
        series.current.candles.setData(candleData);

        // EMA lines aligned 1:1 with the CLOSED bars (emas[i] ↔ bars[i]).
        const emas = d.emas || [];
        const n = Math.min(bars.length, emas.length);
        const t9 = [], t21 = [], t50 = [];
        for (let i = bars.length - n; i < bars.length; i++) {
          const e = emas[i - (bars.length - n)];
          const t = bars[i].t;
          if (e.ema9 != null) t9.push({ time: t, value: e.ema9 });
          if (e.ema21 != null) t21.push({ time: t, value: e.ema21 });
          if (e.ema50 != null) t50.push({ time: t, value: e.ema50 });
        }
        // extend each EMA to the live bar using current values so lines reach "now"
        if (d.live && d.ema_now) {
          if (d.ema_now.ema9 != null) t9.push({ time: d.live.t, value: d.ema_now.ema9 });
          if (d.ema_now.ema21 != null) t21.push({ time: d.live.t, value: d.ema_now.ema21 });
          if (d.ema_now.ema50 != null) t50.push({ time: d.live.t, value: d.ema_now.ema50 });
        }
        series.current.ema9.setData(dedupe(t9));
        series.current.ema21.setData(dedupe(t21));
        series.current.ema50.setData(dedupe(t50));

        // 9x21 crossover markers on the candles (semantic up/down — not color-alone).
        const markers = (d.markers || []).map((m) => (
          m.side === 'call'
            ? { time: m.t, position: 'belowBar', color: '#16a34a', shape: 'arrowUp', text: '9▲21' }
            : { time: m.t, position: 'aboveBar', color: '#dc2626', shape: 'arrowDown', text: '9▼21' }
        ));
        series.current.candles.setMarkers(markers);
      } catch {
        /* transient — keep last frame */
      }
    };
    pull();
    const id = setInterval(pull, 2000);
    return () => { stop = true; clearInterval(id); };
  }, [apiBaseUrl, symbol]);

  return (
    <div className="relative">
      {/* legend — identity is never color-alone: each EMA is labeled */}
      <div className="absolute z-10 top-1 left-2 flex items-center gap-3 text-[10px] font-mono">
        <Legend color={EMA9_COLOR} label="EMA 9" />
        <Legend color={EMA21_COLOR} label="EMA 21" />
        <Legend color={EMA50_COLOR} label="EMA 50" />
        {regime && (
          <span className={`ml-1 px-1.5 py-0.5 rounded ${regime === 'call' ? 'bg-green-500/15 text-green-400' : 'bg-red-500/15 text-red-400'}`}>
            {regime === 'call' ? '21 > 50 (bull)' : '21 < 50 (bear)'}
          </span>
        )}
      </div>
      <div ref={el} style={{ width: '100%', height }} />
      {empty && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-gray-500 pointer-events-none">
          Waiting for 1-min candles…
        </div>
      )}
    </div>
  );
}

function Legend({ color, label }) {
  return (
    <span className="inline-flex items-center gap-1 text-gray-400">
      <span className="inline-block w-3 h-[2px] rounded" style={{ background: color }} />
      {label}
    </span>
  );
}

function dedupe(arr) {
  const seen = new Set();
  const out = [];
  for (const p of arr) { if (!seen.has(p.time)) { seen.add(p.time); out.push(p); } }
  return out;
}
