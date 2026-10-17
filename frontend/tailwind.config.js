/** @type {import('tailwindcss').Config} */
export default {
  important: '#root',
  darkMode: 'class',
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // ── terminal slate surfaces (deep ink-blue, not flat black) ──
        base:   '#0B0E14',   // app background
        panel:  '#131722',   // cards / panels
        raised: '#1C2230',   // inputs / raised chips
        line:   '#2A3142',   // hairline dividers/borders
        // ── text ──
        ink:    '#E8EDF4',   // primary
        muted:  '#7D8799',   // secondary
        faint:  '#4B5468',   // tertiary / captions
        // ── data color (used ONLY for P&L, direction, state) ──
        up:     '#3FB68B',   // green / calls / profit
        down:   '#E5484D',   // red / puts / loss
        warn:   '#E0A458',   // amber / kill / live / caution
        // ── restrained interactive accent ──
        accent: '#5B8DEF',   // active tab, focus, primary action
        'accent-dim': '#2B3A5C',
        // legacy aliases so existing classes keep working during migration
        'dark-primary': '#0B0E14',
        'dark-secondary': '#131722',
        'dark-tertiary': '#1C2230',
        'accent-primary': '#5B8DEF',
        'accent-secondary': '#2B3A5C',
        'text-primary': '#E8EDF4',
        'text-secondary': '#7D8799',
      },
      fontFamily: {
        display: ['"Space Grotesk"', 'system-ui', 'sans-serif'],
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      borderRadius: {
        card: '10px',
      },
      boxShadow: {
        'panel': '0 1px 0 0 rgba(255,255,255,0.02) inset, 0 8px 24px -12px rgba(0,0,0,0.6)',
      },
      backgroundImage: {
        'gradient-radial': 'radial-gradient(var(--tw-gradient-stops))',
      },
    },
  },
  plugins: [],
}
