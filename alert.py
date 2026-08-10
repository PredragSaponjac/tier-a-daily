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
    lines.append(f"• Spot ${entry:.2f} ({c['spot_return_pct']:+.1f}% / 5d)")
    lines.append(f"• skew_change_5d: {c['skew_change_5d']:+.1f} (Tier A bar ≤ −7)")
    lines.append(f"• near_skew: {c['near_skew']:+.1f} (Tier A bar ≤ −7)")
    lines.append(f"• near_dte: {c['near_dte']}")
    lines.append(f"• Put wall: ${pwall} (spot {'+' if cushion>=0 else ''}{cushion:.1f}% {'above' if cushion>=0 else 'below'})")
    lines.append(f"• Sector: {c.get('sector','—')}")
    if v and v.get('details', {}).get('earnings', {}).get('next_earnings'):
        lines.append(f"• Next earnings: {v['details']['earnings']['next_earnings']}")
    lines.append("")
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
    lines.append("")
    # Only make the strong statistical-pattern claim when the flow actually
    # confirms it (score >= 3). On weaker scores, state honestly that the skew
    # setup is present but flow is not confirming - never attach the p=0.0007
    # research claim to a setup that doesn't match the full pattern.
    if score >= 3:
        lines.append(f"→ Flow CONFIRMS ({score}/4): matches the 'structural unwind + capitulation'")
        lines.append("  research pattern (Bonferroni-significant, p=0.0007) — adds conviction on top.")
    else:
        lines.append(f"→ Flow {score}/4: no extra institutional confirmation today. This is a BONUS")
        lines.append("  layer, NOT a requirement — the Tier A skew setup above IS the signal and")
        lines.append("  stands on its own. (A 0/4 does not make the setup weak.)")
    lines.append("")
    lines.append(f"ENTRY: ${entry:.2f}")
    lines.append(f"🎯 T1 (default exit): ${T1:.2f} (+{tps['tp1']:.0f}%) — bot will auto-close here")
    lines.append(f"   T2 (hold longer): ${T2:.2f} (+{tps['tp2']:.0f}%) — optional")
    lines.append(f"   T3 (stretch):     ${T3:.2f} (+{tps['tp3']:.0f}%) — optional")
    lines.append(f"🛑 STOP: ${STOP:.2f} ({stop_p:+.0f}%) HARD")
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
