import { useState, useEffect, useCallback } from 'react'
import { BrowserRouter as Router, Routes, Route, NavLink, Navigate } from 'react-router-dom'
import ScalperPanel from './components/ScalperPanel'
import SpreadsPanel from './components/SpreadsPanel'
import ScannerPanel from './components/ScannerPanel'
import './App.css'

const API_BASE_URL = 'http://localhost:8080'

// ── persistent status rail: feed / account / day P&L / kill — always visible ──
function StatusRail({ st }) {
  const live = st?.live_trading
  const feed = st?.feed_running
  const pnl = st?.day_pnl ?? 0
  const kill = st?.kill_switch_tripped
  const lock = st?.profit_lock_tripped
  const state = st?.entry_state
  const pnlColor = pnl > 0 ? 'text-up' : pnl < 0 ? 'text-down' : 'text-muted'
  const stateMap = {
    active: ['ACTIVE', 'text-up'], paused: ['PAUSED', 'text-warn'],
    kill: ['KILL', 'text-down'], profit_lock: ['LOCKED', 'text-warn'],
    blackout: ['BLACKOUT', 'text-muted'], outside_window: ['OUT OF HRS', 'text-muted'],
    stopped: ['STOPPED', 'text-muted'],
  }
  const [stLabel, stColor] = stateMap[state] || [(state || '—').toUpperCase(), 'text-muted']
  return (
    <div className="sticky top-0 z-30 border-b border-line bg-base/90 backdrop-blur">
      <div className="max-w-[1400px] mx-auto px-4 h-12 flex items-center gap-5 text-sm">
        <div className="flex items-center gap-2 shrink-0">
          <span className="font-display font-bold text-ink tracking-tight">SCALPER</span>
          <span className="eyebrow hidden sm:inline">terminal</span>
        </div>
        <div className="h-5 w-px bg-line hidden sm:block" />
        <Stat label="Feed">
          <span className={`inline-flex items-center gap-1.5 ${feed ? 'text-up' : 'text-down'}`}>
            <span className={`w-1.5 h-1.5 rounded-full bg-current ${feed ? 'livedot' : ''}`} />
            {feed ? 'Live' : 'Down'}
          </span>
        </Stat>
        <Stat label="Account">
          <span className={live ? 'text-down font-semibold' : 'text-muted'}>{live ? 'LIVE $' : 'Paper'}</span>
        </Stat>
        <Stat label="Entries" className="hidden md:flex">
          <span className={stColor}>{stLabel}</span>
        </Stat>
        <div className="flex-1" />
        <div className="flex items-baseline gap-2">
          <span className="eyebrow">Day P&amp;L</span>
          <span className={`num text-lg font-semibold ${pnlColor}`}>
            {pnl >= 0 ? '+' : '−'}${Math.abs(pnl).toFixed(0)}
          </span>
        </div>
        {(kill || lock) && (
          <span className={`num text-[10px] px-2 py-0.5 rounded ${kill ? 'bg-down/15 text-down' : 'bg-warn/15 text-warn'}`}>
            {kill ? '⛔ KILL' : '🔒 LOCK'}
          </span>
        )}
      </div>
    </div>
  )
}

function Stat({ label, children, className = '' }) {
  return (
    <div className={`flex items-center gap-1.5 ${className}`}>
      <span className="eyebrow">{label}</span>
      <span className="num text-[13px]">{children}</span>
    </div>
  )
}

function TabNav() {
  const cls = ({ isActive }) =>
    `relative px-3 py-2.5 text-sm font-medium transition-colors ${
      isActive ? 'text-ink' : 'text-muted hover:text-ink'
    }`
  const underline = (isActive) =>
    isActive ? <span className="absolute left-3 right-3 -bottom-px h-0.5 bg-accent rounded-full" /> : null
  return (
    <nav className="border-b border-line">
      <div className="max-w-[1400px] mx-auto px-4 flex items-center gap-1">
        <NavLink to="/scalper" className={cls}>{({ isActive }) => (<>Scalper{underline(isActive)}</>)}</NavLink>
        <NavLink to="/spreads" className={cls}>{({ isActive }) => (<>Spreads{underline(isActive)}</>)}</NavLink>
        <NavLink to="/scanner" className={cls}>{({ isActive }) => (<>Scanner{underline(isActive)}</>)}</NavLink>
      </div>
    </nav>
  )
}

function App() {
  const [st, setSt] = useState(null)

  const refreshStatus = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE_URL}/scalper/status`)
      if (r.ok) setSt(await r.json())
    } catch { /* rail keeps last-known */ }
  }, [])

  useEffect(() => {
    refreshStatus()
    const id = setInterval(refreshStatus, 3000)
    return () => clearInterval(id)
  }, [refreshStatus])

  return (
    <Router>
      <div className="min-h-screen">
        <StatusRail st={st} />
        <TabNav />
        <main className="max-w-[1400px] mx-auto px-4 py-5">
          <Routes>
            <Route path="/" element={<Navigate to="/scalper" replace />} />
            <Route path="/scalper" element={<ScalperPanel apiBaseUrl={API_BASE_URL} />} />
            <Route path="/spreads" element={<SpreadsPanel apiBaseUrl={API_BASE_URL} />} />
            <Route path="/scanner" element={<ScannerPanel apiBaseUrl={API_BASE_URL} />} />
            <Route path="*" element={<Navigate to="/scalper" replace />} />
          </Routes>
        </main>
      </div>
    </Router>
  )
}

export default App
