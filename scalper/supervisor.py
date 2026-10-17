"""
Standalone Opus scalper supervisor — direct Anthropic SDK (no platform provider).

Ports the platform's _maybe_run_ai_supervisor: reads the recent tape + stats,
picks mode (momentum|fade|pause), and on the DEEP cycle tunes params + optionally
synthesizes a strategy spec. Uses adaptive thinking + output_config.effort +
streaming (the Opus 4.8 requirements established earlier in the platform).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"


def _adaptive_kwargs(effort: str, max_tokens: int) -> Dict[str, Any]:
    # Opus 4.8: adaptive thinking only, effort via output_config, NO temperature.
    return {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort if effort in ("low", "medium", "high") else "medium"},
        "max_tokens": max(8000, max_tokens),
    }


async def run_supervisor_call(api_key: str, prompt: str, deep: bool) -> Optional[Dict[str, Any]]:
    """One supervisor LLM call → parsed JSON dict (or None). Streams the response
    (adaptive thinking can exceed the non-streaming 10-min guard)."""
    try:
        import anthropic
    except Exception as e:
        logger.warning("anthropic SDK not installed: %s", e)
        return None
    client = anthropic.AsyncAnthropic(api_key=api_key)
    effort = "medium" if deep else "low"
    kwargs = _adaptive_kwargs(effort, 16000 if deep else 6000)
    sys = "Respond only with valid minified JSON. No markdown fences."
    try:
        async with client.messages.stream(
            model=MODEL,
            system=sys,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        ) as stream:
            msg = await stream.get_final_message()
    except Exception as e:
        logger.warning("Supervisor LLM call failed: %s", e)
        return None
    # Extract the first text block.
    raw = ""
    for block in (msg.content or []):
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            raw = block.text
            break
    return _parse_json(raw)


def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    txt = raw.strip()
    if "```" in txt and txt.count("```") >= 2:
        txt = txt.split("```")[1]
        if txt.lower().startswith("json"):
            txt = txt[4:]
    a, b = txt.find("{"), txt.rfind("}")
    if a != -1 and b != -1:
        txt = txt[a:b + 1]
    try:
        return json.loads(txt)
    except Exception:
        return None


def build_prompt(symbols: List[str], tape: Dict[str, Any], deep: bool, *,
                 tunable: Dict[str, Any], safe_bands: Dict[str, Any], bool_keys: List[str],
                 dsl_signals: List[str], recent_exits: List, day_pnl: float,
                 win_rate: Optional[float], exits_today: int, last_tune: Optional[Dict[str, Any]],
                 all_mode: bool = False) -> str:
    """Faithful port of the platform supervisor prompt."""
    all_note = (
        "MODE: you are running as ONE of THREE strategies being A/B-compared live "
        "(macross=full-gate EMA cross, macross_x=cross+L2 only, and YOU='ai'). YOUR "
        "trades use your OWN EMA pair — tune ma_fast_ai/ma_slow_ai (NOT ma_fast/ma_slow, "
        "which belong to the mechanical strategies). Your 'mode' (momentum/fade/pause) "
        "and your EMA lengths are your levers; make them count so we can see if the AI "
        "beats the mechanical strategies.\n"
    ) if all_mode else ""
    base = (
        all_note +
        f"You are the live supervisor of a fast 0DTE options scalper on {', '.join(symbols)}. "
        "You are given recent 1-minute OHLC price action. READ THE CHART LIKE A TRADER: identify "
        "support/resistance, structure (uptrend/downtrend/range/coil), and where price sits "
        "(at support, at resistance, breaking out, breaking down, mid-range).\n"
        "Choose the entry stance (only two — fade is DISABLED):\n"
        "- momentum: trade WITH the move (clean break / strong trend, riding VWAP)\n"
        "- pause: stand aside (chop, mid-range, no clean directional edge)\n"
        "Pick momentum ONLY when there is a real one-directional trend; otherwise pause. "
        "Do NOT return 'fade' — it is not traded.\n"
        "ALSO decide enable_macross (bool): turn ON the mechanical 8/21 EMA-cross "
        "strategy to run ALONGSIDE you ONLY when the tape is a CLEAN, SUSTAINED TREND "
        "(high efficiency, one-directional, riding VWAP) — that is its edge. Turn it OFF "
        "in chop/mid-range/low-efficiency tape (it churns and loses there). Default OFF.\n\n"
        f"Tape: {json.dumps(tape)}\n"
    )
    if not deep:
        return base + (
            f"Scalper today: realized_pnl=${round(day_pnl)}, "
            f"win_rate={round(win_rate,2) if win_rate is not None else 'n/a'}, exits={exits_today}.\n\n"
            "Reply ONLY compact minified JSON, max 2 support/2 resistance per symbol, assessment <= 2 "
            "sentences: {\"mode\":\"momentum|pause\",\"enable_macross\":false,\"levels\":{\"SYM\":{\"support\":[n,n],"
            "\"resistance\":[n,n],\"structure\":\"..\",\"position\":\"..\"}},\"assessment\":\"..\","
            "\"reason\":\"<10 words>\"}"
        )
    from collections import Counter
    # recent_exits rows are (reason, pnl) or the richer (reason, pnl, direction, with_trend)
    def _row(r):
        reason = r[0]; pnl = r[1]
        direction = r[2] if len(r) > 2 else None
        with_trend = r[3] if len(r) > 3 else None
        return reason, pnl, direction, with_trend
    rows = [_row(r) for r in recent_exits]
    reason_mix = dict(Counter(reason for reason, _, _, _ in rows))
    wins = [p for _, p, _, _ in rows if p > 0]
    losses = [p for _, p, _, _ in rows if p <= 0]
    # counter-trend leak detector: P&L of trades taken WITH vs AGAINST the slow-EMA trend
    with_pnl = sum(p for _, p, _, wt in rows if wt is True)
    against_pnl = sum(p for _, p, _, wt in rows if wt is False)
    with_n = sum(1 for _, _, _, wt in rows if wt is True)
    against_n = sum(1 for _, _, _, wt in rows if wt is False)
    # direction breakdown of LOSERS (e.g. "your losers are mostly calls")
    loser_dirs = dict(Counter(d for _, p, d, _ in rows if p <= 0 and d))
    return base + (
        f"Current tunable params: {json.dumps(tunable)}\n"
        f"Safe bands (stay inside; values outside are clamped): {json.dumps(safe_bands)}\n"
        f"Boolean toggles you may flip: {bool_keys}\n"
        f"Recent {len(rows)} exits — reason_mix={reason_mix}, "
        f"avg_win=${round(sum(wins)/len(wins),1) if wins else 0}, "
        f"avg_loss=${round(sum(losses)/len(losses),1) if losses else 0}, "
        f"win_rate={round(win_rate,2) if win_rate is not None else 'n/a'}, exits_today={exits_today}.\n"
        f"TREND ALIGNMENT of recent trades — WITH-trend: {with_n} trades, pnl=${round(with_pnl)}; "
        f"AGAINST-trend: {against_n} trades, pnl=${round(against_pnl)}. Losers by direction: {loser_dirs}.\n"
        "IF your against-trend trades are net-negative (or your losers cluster in one direction while "
        "the tape trends the other way), you are FIGHTING THE TREND — stop taking that side. Prefer "
        "WITH-trend entries; only counter-trend at a CLEAR rejected level with a real edge thesis.\n"
        + (f"Your last tuning: {json.dumps({k: last_tune.get(k) for k in ('param_changes','reasoning') if last_tune.get(k)})}\n" if last_tune else "")
        + "ADAPT the scalper to current conditions. In param_changes include ONLY knobs to change (diff), "
        "each within its safe band. You may NOT change order size, kill switch, symbols, or budget.\n"
        "ADAPTIVE RESOLUTION (important): you can tune regime_sample_seconds (5-60s) and er_window (8-40) "
        "to fit the timescale. Compare er_fast_8 vs er_slow_32 in the tape: if efficiency is LOW on the fine "
        "view but HIGH on the coarse view, it's a SLOW GRIND drowned by tick noise — COARSEN "
        "(raise regime_sample_seconds toward 30-45 and/or widen er_window) so the drift registers as a "
        "tradeable trend. If a FAST clean move, use finer sampling. Don't coarsen so much that real chop "
        "reads as trend.\n\n"
        "OPTIONALLY synthesize a custom entry STRATEGY as a rules spec. Each rule: "
        "{\"when\": <condition>, \"action\": \"call|put|with_momentum|fade|skip\"}. Rules evaluated in "
        "order; first match wins; none match → skip. Conditions use signals "
        f"{sorted(dsl_signals)}, ops "
        "{\">\",\"<\",\">=\",\"<=\",\"between\",\"==\",\"and\",\"or\",\"not\"}. "
        "at_support/at_resistance/breaking_out/breaking_down/mid_range are level booleans. mtf_dir is the "
        "higher-TF trend ('call'=up,'put'=down,'flat'); mtf_alignment 0..1. vwap_z = signed std-devs from "
        "session VWAP. book_imbalance (-1..1, Schwab L2) confirms direction (>0 buyers, <0 sellers). "
        "Only synthesize when you have a clear edge thesis; else set strategy_spec null.\n"
        "Reply ONLY compact minified JSON: {\"mode\":\"momentum|pause\",\"enable_macross\":false,\"param_changes\":{...},"
        "\"strategy_spec\":null|{\"rules\":[{\"when\":{...},\"action\":\"fade\"}]},\"levels\":{...},"
        "\"reasoning\":\"3-5 sentences\",\"assessment\":\"..\",\"reason\":\"<10 words>\"}"
    )
