"""Format and send signal alerts to Telegram.

Two alert types:
- format_signal(candidate, all_candidates_today) -> str (markdown for Telegram)
- format_close(...) -> str (Phase 2: TP1 / stop hit alerts)
"""
import os
import datetime
import requests
import parameters as P


def _next_trading_day_phrase(scan_date: str) -> str:
    """Return a calendar-aware 'next update' phrase.

    The bot only sends Telegram from the PM run, which fires Mon-Fri.
    So from Fri the next update is Monday (NOT 'tomorrow' = Saturday).
    Skips weekends. (US market holidays are not modeled here; on a holiday
    the GH run simply produces no new data, which is acceptable.)
    """
    try:
        d = datetime.date.fromisoformat(scan_date[:10])
    except Exception:
        return "Next update: next trading day."
    nxt = d + datetime.timedelta(days=1)
    while nxt.weekday() >= 5:          # 5 = Sat, 6 = Sun -> skip to Monday
        nxt += datetime.timedelta(days=1)
    if (nxt - d).days == 1:
        when = "tomorrow"
    else:
        when = nxt.strftime("%A")      # e.g. "Monday"
    return f"Next update: {when} after the 2:45 PM CT scan."


TRACK_RECORD_LINE = (
    "📊 Live track record (skew-tracker AR-style, since 4/20):\n"
    "AR ✅ +7% | CRSP ✅ TP1 | SMMT ✅ TP1 | SYM ❌ -7% | VKTX ✅ TP1\n"
    "Backtest n=63: 67% TP1 hit rate, 71% profitable, +5.2% avg/trade"
    # Sheet URL intentionally NOT included — Telegram subscribers don't need it.
    # X posts include sheet URL (public marketing/proof) — see x_post.py format_signal_for_x.
)

DISCLAIMER = (
    "⚠️ Quantitative research only. NOT financial advice. NOT a recommendation. "
    "Do your own due diligence. Size per your risk. Past patterns ≠ future results."
)


def format_signal(c: dict, day_pool: list[dict]) -> str:
    """Format a Telegram signal post."""
    f = c['filter']
    r = f['raw']
    v = c.get('vetoes', {})
    entry = c['spot_close']
    pwall = c['put_wall_strike']
    cushion = ((entry / pwall - 1) * 100) if pwall else 0
    tps = P.tp_pcts()
    stop_p = P.stop_pct()
    T1 = entry * (1 + tps['tp1'] / 100)
    T2 = entry * (1 + tps['tp2'] / 100)
    T3 = entry * (1 + tps['tp3'] / 100)
    STOP = entry * (1 + stop_p / 100)
    score = f.get('score', 0) or 0
    conviction = '⭐' * score + '☆' * (4 - score)

    lines = []
    lines.append(f"🎯 NEW Tier A Signal — ${c['ticker']}")
    lines.append(f"CONVICTION: {conviction} ({score}/4)")
    lines.append("")
    lines.append("")
    lines.append("SKEW SETUP (Tier A — all gates passed):")
    lines.append(f"• Spot ${entry:.2f} ({c['spot_return_pct']:+.1f}% / 5d)")
    lines.append(f"• skew_change_5d: {c['skew_change_5d']:+.1f} (Tier A bar ≤ −7)")
    lines.append(f"• near_skew: {c['near_skew']:+.1f} (Tier A bar ≤ −7)")
    lines.append(f"• near_dte: {c['near_dte']}")
    lines.append(f"• Put wall: ${pwall} (spot {'+' if cushion>=0 else ''}{cushion:.1f}% {'above' if cushion>=0 else 'below'})")
    lines.append(f"• Sector: {c.get('sector','—')}")
    if v and v.get('details', {}).get('earnings', {}).get('next_earnings'):
        lines.append(f"• Next earnings: {v['details']['earnings']['next_earnings']}")
    lines.append("")
    lines.append("UW INSTITUTIONAL POSITIONING (research-derived):")
    conds = f.get('conditions', {})
    for key, cond in conds.items():
        check = "✅" if cond['pass'] else "❌"
        val = cond.get('value')
        if val is None:
            val_str = "—"
        elif isinstance(val, float):
            val_str = f"{val:+.2f}" if 'z' in key else f"{val:+.1f}"
        else:
            val_str = str(val)
        # Friendly label (keys MUST match conditions dict in uw_filter.compute_score)
        label_map = {
            'ncp_entry_z':         f'net_call_premium entry z-score: {val_str}',
            'call_oi_10d_pct':     f'call_OI 10d change: {val_str}%',
            'put_oi_10d_pct':      f'put_OI 10d change: {val_str}%',
            'dp_large_blocks_10d': f'dark pool large blocks (10d): {val_str}',
        }
        lines.append(f"{check} {label_map.get(key, key+': '+val_str)}")
    lines.append("")
    # Only make the strong statistical-pattern claim when the flow actually
    # confirms it (score >= 3). On weaker scores, state honestly that the skew
    # setup is present but flow is not confirming - never attach the p=0.0007
    # research claim to a setup that doesn't match the full pattern.
    if score >= 3:
        lines.append("Matches the 'structural unwind + entry capitulation' pattern from")
        lines.append("research_uw_picker_v1 (Bonferroni-significant, p=0.0007).")
    else:
        lines.append("Skew setup present, but institutional flow is NOT confirming the")
        lines.append("full 'structural unwind + capitulation' pattern — lower conviction.")
    lines.append("")
    lines.append(f"ENTRY: ${entry:.2f}")
    lines.append(f"🎯 T1 (default exit): ${T1:.2f} (+{tps['tp1']:.0f}%) — bot will auto-close here")
    lines.append(f"   T2 (hold longer): ${T2:.2f} (+{tps['tp2']:.0f}%) — optional")
    lines.append(f"   T3 (stretch):     ${T3:.2f} (+{tps['tp3']:.0f}%) — optional")
    lines.append(f"🛑 STOP: ${STOP:.2f} ({stop_p:+.0f}%) HARD")
    lines.append("")
    lines.append("⏱️ Short-term pullback play — ~5-7 day trade (10-day max hold).")
    lines.append("")
    if day_pool and len(day_pool) > 1:
        runner_up = next((x for x in day_pool if x['ticker'] != c['ticker']), None)
        if runner_up:
            ru_f = runner_up['filter']
            lines.append(f"(Today's runner-up: ${runner_up['ticker']} score {ru_f.get('score','—')}/4)")
            lines.append("")
    # Live results line (auto, never stale) + heat & peak detail
    try:
        import excursions
        rline = excursions.format_results_line()
        lines.append(rline if rline else TRACK_RECORD_LINE)
        lines.append("")
        blk = excursions.format_excursion_block()
        if blk:
            lines.append(blk)
            lines.append("")
    except Exception as e:
        print(f'[telegram] track-record block skipped: {e}')
        lines.append(TRACK_RECORD_LINE)
        lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_no_signal(scan_date: str, n_candidates: int, n_vetoed: int) -> str:
    """Post when no Tier A signal qualifies."""
    if n_candidates == 0:
        return (
            f"📅 Tier A Daily — {scan_date}\n\n"
            f"No Tier A setups passed the screen today.\n"
            f"{_next_trading_day_phrase(scan_date)}"
        )
    return (
        f"📅 Tier A Daily — {scan_date}\n\n"
        f"{n_candidates} Tier A candidate(s) surfaced but {n_vetoed} vetoed and "
        f"none passed the composite filter strongly enough to alert.\n"
        f"NO TRADE today.\n"
        f"{_next_trading_day_phrase(scan_date)}"
    )


def format_quota_blocked(scan_date: str, n_candidates: int, n_unscored: int) -> str:
    """Fail-safe post when UW's daily quota was exhausted mid-run.

    We do NOT claim 'no setups' — that would be a false negative. We say plainly
    that flow scoring was unavailable and no trade is posted as a safeguard.
    """
    return (
        f"📅 Tier A Daily — {scan_date}\n\n"
        f"⚠️ Flow scoring unavailable today — UW daily data limit was hit "
        f"before all candidates could be evaluated.\n"
        f"{n_candidates} Tier A skew candidate(s) surfaced; {n_unscored} could "
        f"not be flow-scored.\n"
        f"NO trade posted (fail-safe — we never fire without confirming flow).\n"
        f"{_next_trading_day_phrase(scan_date)}"
    )


def format_close(ticker: str, entry: float, exit_price: float, reason: str) -> str:
    """Phase 2: format close alert (TP1 hit, stop hit, timeout)."""
    ret = (exit_price / entry - 1) * 100
    emoji = "📍" if reason == "TP1" else ("🛑" if reason == "STOP" else "⏱️")
    reason_text = {
        "TP1": f"TP1 (+10%) HIT — scaled out / closed",
        "STOP": f"STOP (−7%) HIT — exited",
        "TIMEOUT": f"10-day timeout — closed at last price",
    }.get(reason, reason)
    return (
        f"{emoji} ${ticker} — {reason_text}\n"
        f"Entry ${entry:.2f} → Exit ${exit_price:.2f} ({ret:+.2f}%)"
    )


def send_telegram(message: str, chat_id: str = None, bot_token: str = None) -> bool:
    """Send to Telegram. Returns True on success."""
    bot_token = bot_token or os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = chat_id or os.environ.get('TELEGRAM_CHAT_ID')
    if not bot_token or not chat_id:
        print('[telegram] missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID')
        return False
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    try:
        resp = requests.post(url, json={
            'chat_id': chat_id,
            'text': message,
            'disable_web_page_preview': True,
        }, timeout=15)
        if resp.status_code == 200:
            return True
        print(f'[telegram] {resp.status_code}: {resp.text[:200]}')
        return False
    except Exception as e:
        print(f'[telegram] error: {e}')
        return False
