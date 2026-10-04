"""Format and send signal alerts to Telegram.

Two alert types:
- format_signal(candidate, all_candidates_today) -> str (markdown for Telegram)
- format_close(...) -> str (Phase 2: TP1 / stop hit alerts)
"""
import os
import datetime
import requests
import legs as LEGS
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
    "📊 Paper research record. Historical cohorts used different execution assumptions; "
    "a prospective net trading edge has not been established."
)

DISCLAIMER = (
    "⚠️ Quantitative research only. NOT financial advice. NOT a recommendation. "
    "Do your own due diligence. Size per your risk. Past patterns ≠ future results."
)


def format_signal(c: dict, day_pool: list[dict], taken: list[dict] | None = None) -> str:
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
    L = c.get('legs', {})
    # SETUP STRENGTH is driven by the SKEW SETUP itself (the actual signal) — a deep
    # skew capitulation leg and/or a big vol-adjusted cushion leg. UW flow is a
    # SEPARATE bonus-confirmation layer shown lower down; it is NOT the signal and a
    # 0/4 does NOT make the trade weak.
    # NO ADJECTIVES (fixed 2026-08-03). "SOLID"/"STRONG" told the reader nothing —
    # not which gate carried the trade, not how close anything was to its bar.
    # Print the actual measurements against the actual thresholds instead.
    _sel = P.selection_params()
    _nz = c.get('noise', {}) or {}
    _sk, _vc, _cp = c.get('skew'), L.get('vol_cushion'), L.get('cushion_pct')
    _std = _nz.get('skew_std')
    _yn = lambda ok: '✅' if ok else '❌'
    _f = lambda v, fmt: (fmt.format(v) if isinstance(v, (int, float)) else 'n/a')
    setup_lines = [
        "QUALIFYING LEGS — at least ONE must pass (this is what makes it tradeable):",
        f"  {_yn(L.get('strong_skew'))} structural skew {_f(_sk, '{:+.1f}')}"
        f"   (bar: ≤ {_sel['strong_skew_max']:.0f})",
        f"  {_yn(L.get('strong_cushion'))} vol-adj cushion {_f(_vc, '{:.1f}')}x"
        f"   (bar: ≥ {_sel['strong_vol_cushion_min']:.1f}x)",
        "HARD DISQUALIFIERS — both must be clear:",
        f"  {_yn(_cp is not None and _cp >= 0)} spot above put wall"
        f"   ({_f(_cp, '{:+.1f}')}% vs wall)",
        f"  {_yn(not _nz.get('noisy'))} chain noise std {_f(_std, '{:.1f}')}"
        f"   (bar: ≤ {_sel['skew_noise_std_max']:.0f})",
        f"→ TRADEABLE: {'YES' if L.get('tradeable') else 'NO'}",
    ]
    setup_str = '\n'.join(setup_lines)

    lines = []
    lines.append(f"🎯 NEW Tier A Signal — ${c['ticker']}")
    lines.append(setup_str)
    lines.append("")
    lines.append("")
    lines.append("SKEW SETUP — THE SIGNAL (Tier A gates passed):")
    lines.append(f"• Reference spot ${entry:.2f} ({c['spot_return_pct']:+.1f}% / 9 sessions)")
    lines.append(f"• Skew change / 9 sessions: {c['skew_change_5d']:+.1f} (Tier A bar ≤ −7)")
    lines.append(f"• near_skew: {c['near_skew']:+.1f} (Tier A bar ≤ −7)")
    lines.append(f"• near_dte: {c['near_dte']}")
    lines.append(f"• Put wall: ${pwall} (spot {'+' if cushion>=0 else ''}{cushion:.1f}% {'above' if cushion>=0 else 'below'})")
    lines.append(f"• Sector: {c.get('sector','—')}")
    if v and v.get('details', {}).get('earnings', {}).get('next_earnings'):
        lines.append(f"• Next earnings: {v['details']['earnings']['next_earnings']}")
    lines.append("")
    # UW SECTION IS OMITTED ENTIRELY when there is no UW data (subscription ended
    # 2026-08-13). Printing "0/4" would be a false claim (we never read the flow),
    # and printing "NOT MEASURED" just advertises a missing feed every single day.
    # The layer was always a bonus, so the honest and clean choice is silence.
    # It reappears automatically the moment UW_API_KEY works again — no code change.
    uw_missing = f.get('score') is None
    conds = {}
    if not uw_missing:
        lines.append(f"🔎 UW INSTITUTIONAL FLOW — supplementary confirmation only, NOT the signal ({score}/4):")
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
    if not uw_missing:
        lines.append("")
    # Report the measured conditions without an unvalidated significance claim.
    if uw_missing:
        pass                      # section omitted entirely — no UW data to report
    elif score >= 3:
        lines.append(f"→ UW conditions met: {score}/4. Used for ranking; prospective benefit remains unproven.")
    else:
        lines.append(f"→ UW conditions met: {score}/4. Supplementary ranking data; eligibility uses the gates above.")
    if not uw_missing:
        lines.append("")
    lines.append('PAPER ENTRY: next regular-session open; price pending.')
    lines.append(f"🎯 Target: +{tps['tp1']:.0f}% from that open. Stop: {stop_p:+.0f}% (gaps may lose more).")
    lines.append('Completed daily bars; a bar touching both barriers is flagged ambiguous and booked as a stop.')
    lines.append("")
    lines.append("⏱️ Short-term pullback play — exits on target (win) or stop (loss), no time limit.")
    # EDGE-VALIDATION block — research logging only, NOT part of the signal.
    e = c.get('edge')
    if e:
        lines.append("")
        lines.append("🔬 Edge-validation (research only — does NOT affect selection):")
        lines.append(f"   sector_iv_rank {e['sector_iv_rank']} | skew_slope {e['skew_slope']} | iv/hv {e['iv_hv_ratio']}")
        lines.append(f"   combo(rank≥60 & slope≤−1.6): {'✅ pass' if e['combo_pass'] else '— no'} | iv/hv≥1.1: {'✅ pass' if e['ivr_pass'] else '— no'}")
        if e.get('stop_atr') is not None:
            warn = '  ⚠️ stop inside 1 daily range' if e.get('tight_stop') else ''
            lines.append(f"   stop width: −7% = {e['stop_atr']}x ATR (ATR {e['atr_pct']}%)"
                         f" | a 2×ATR stop would be −{e['atr2x_stop_pct']}%{warn}")
    lines.append("")
    # Additional reservations use the same next-open assumption as the featured name.
    others = [t for t in (taken or []) if t.get('ticker') != c['ticker']]
    if others:
        lines.append(f"📋 ALSO TRACKED TODAY — same rules, equal weight ({len(others)} more):")
        for t in others:
            Lt = t.get('legs', {}) or {}
            lines.append(f"   ${t['ticker']}  next-open paper entry pending; T1 +{tps['tp1']:g}% / STOP {stop_p:g}%"
                         f"   skew {t.get('skew', 0):+.1f}  cushion {Lt.get('vol_cushion') or 0:.1f}x  legs {int(Lt.get('strong_skew', 0)) + int(Lt.get('strong_cushion', 0))}")
        cap = int(P.selection_params().get('max_concurrent', 6))
        lines.append(f"   Size EVERY position at 1/{cap} of the book ({cap} = the concurrent cap, not today's count) —")
        lines.append('   allocation is fixed per reserved slot. The take-all policy remains unproven.')
        lines.append("")
    elif day_pool and len(day_pool) > 1 and not taken:
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
        f"{n_candidates} Tier A candidate(s) surfaced, but none qualified — each was "
        f"either vetoed or WEAK ON BOTH legs (no deep skew capitulation AND no real "
        f"vol-adjusted cushion).\n"
        f"NO TRADE today — we don't force weak-on-both setups (the SYM/RGTI lesson).\n"
        f"{_next_trading_day_phrase(scan_date)}"
    )


def format_watch_list(watch: list) -> str:
    """Blocked-but-notable candidates — shown so the user can eyeball and decide
    manually. NOT auto-traded. Flow can be right even when structure says wait
    (e.g. CAVA 6/8 was UW 3/4 but weak-on-both → we skipped, it ran +11%)."""
    if not watch:
        return ''
    lines = ['', '— — — — — — — — — —',
             '📋 WATCH LIST — surfaced but NOT auto-traded (your call):',
             '(held back by the gate; flow can be right even when structure says wait — e.g. CAVA 6/8 +11%)']
    for c in watch:
        score = c['filter'].get('score') or 0
        conv = '⭐' * score + '☆' * (4 - score)
        L = c.get('legs', {})
        skew = c.get('skew')
        vc = L.get('vol_cushion')
        if c.get('noise', {}).get('noisy'):
            why = f"NOISY chain (skew std {c['noise']['skew_std']:.0f})"
        elif L.get('cushion_pct') is not None and L.get('cushion_pct') < 0:
            why = 'spot below put wall'
        elif L.get('cushion_pct') is not None and L.get('cushion_pct') > LEGS.MAX_CUSHION_PCT:
            # HUT 2026-08-10 read "weak on both legs", which hid the real reason.
            why = f"STALE WALL (spot +{L['cushion_pct']:.0f}% above it) — cushion void"
        else:
            why = 'weak on both legs'
        skew_s = f"{skew:+.0f}" if skew is not None else '?'
        vc_s = f"{vc:.1f}x" if vc is not None else '?'
        lines.append(f"  • {c['ticker']:<5s} UW {score}/4 {conv} | skew {skew_s}, cushion {vc_s} | {why}")
        # Edge-validation readout (research only — informational, still NOT auto-traded)
        e = c.get('edge')
        if e:
            atr_s = f" | stop {e['stop_atr']}xATR" if e.get('stop_atr') is not None else ''
            lines.append(f"      🔬 edge: rank {e['sector_iv_rank']} | slope {e['skew_slope']} | iv/hv {e['iv_hv_ratio']}"
                         f" → combo {'✅' if e['combo_pass'] else '—'}{atr_s}")
    return '\n'.join(lines)


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
    """Compatibility wrapper; durable outboxes use the structured result below."""
    return send_telegram_status(message, chat_id, bot_token)[0] == 'sent'


def send_telegram_status(message: str, chat_id: str = None, bot_token: str = None) -> tuple:
    """Return (sent/rejected/unknown/not_configured, receipt).

    A timeout, server error or malformed success response can follow acceptance.
    Unknown outcomes must be reconciled rather than blindly resent. Exception
    strings can contain the credential-bearing URL and are deliberately omitted.
    """
    bot_token = bot_token or os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = chat_id or os.environ.get('TELEGRAM_CHAT_ID')
    if not bot_token or not chat_id:
        print('[telegram] missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID')
        return 'not_configured', None
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    try:
        resp = requests.post(url, json={
            'chat_id': chat_id,
            'text': message,
            'disable_web_page_preview': True,
        }, timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            if result.get('ok') is True:
                return 'sent', (result.get('result') or {}).get('message_id')
            return ('rejected', None) if result.get('ok') is False else ('unknown', None)
        print(f'[telegram] HTTP {resp.status_code}')
        return ('rejected', None) if 400 <= resp.status_code < 500 else ('unknown', None)
    except Exception as e:
        print(f'[telegram] response unknown ({type(e).__name__}); reconciliation required')
        return 'unknown', None
