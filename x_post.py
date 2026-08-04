"""X (Twitter) auto-posting via OAuth 1.0a.

Posts the same content as Telegram, just slightly tightened for X.
User has X Premium → 4000 char/post limit (no thread splitting needed for typical signal).

Required env vars (from MEMORY.md):
- X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET
"""
import os

try:
    from requests_oauthlib import OAuth1Session
    OAUTH_OK = True
except ImportError:
    OAUTH_OK = False

X_TWEET_ENDPOINT = 'https://api.twitter.com/2/tweets'


def _session():
    if not OAUTH_OK:
        raise RuntimeError('requests_oauthlib not installed — pip install requests_oauthlib')
    keys = {
        'client_key': os.environ.get('X_API_KEY'),
        'client_secret': os.environ.get('X_API_SECRET'),
        'resource_owner_key': os.environ.get('X_ACCESS_TOKEN'),
        'resource_owner_secret': os.environ.get('X_ACCESS_SECRET'),
    }
    if not all(keys.values()):
        raise RuntimeError('X API keys missing (X_API_KEY/X_API_SECRET/X_ACCESS_TOKEN/X_ACCESS_SECRET)')
    return OAuth1Session(**keys)


def post_to_x(text: str) -> bool:
    """Post a single tweet. Returns True on success."""
    if not all([os.environ.get(k) for k in
                ['X_API_KEY','X_API_SECRET','X_ACCESS_TOKEN','X_ACCESS_SECRET']]):
        print('[x] X API keys not set — skipping post')
        return False
    try:
        s = _session()
        resp = s.post(X_TWEET_ENDPOINT, json={'text': text}, timeout=15)
        if resp.status_code in (200, 201):
            return True
        print(f'[x] {resp.status_code}: {resp.text[:200]}')
        return False
    except Exception as e:
        print(f'[x] error: {e}')
        return False


def format_signal_for_x(c: dict, day_pool: list[dict]) -> str:
    """X-specific signal format. Slightly tighter than Telegram.

    X cashtag rule: only ONE $TICKER in post (X rejects 2+ cashtags as 403).
    """
    f = c['filter']
    r = f['raw']
    score = f.get('score', 0) or 0
    conviction = '⭐' * score + '☆' * (4 - score)
    entry = c['spot_close']
    import parameters as P
    tps = P.tp_pcts()
    T1 = entry * (1 + tps['tp1']/100)
    STOP = entry * (1 + P.stop_pct()/100)

    # NO ADJECTIVES (fixed 2026-08-03) — state the measurements, not "SOLID"/"STRONG".
    L = c.get('legs', {})
    _sel = P.selection_params()
    _nz = c.get('noise', {}) or {}
    _sk, _vc, _cp = c.get('skew'), L.get('vol_cushion'), L.get('cushion_pct')
    _yn = lambda ok: '✅' if ok else '❌'
    _f = lambda v, fmt: (fmt.format(v) if isinstance(v, (int, float)) else 'n/a')
    _setup = '\n'.join([
        "Gates (needs ≥1 qualifying leg, both disqualifiers clear):",
        f"  {_yn(L.get('strong_skew'))} structural skew {_f(_sk, '{:+.1f}')} "
        f"(bar ≤{_sel['strong_skew_max']:.0f})",
        f"  {_yn(L.get('strong_cushion'))} vol-adj cushion {_f(_vc, '{:.1f}')}x "
        f"(bar ≥{_sel['strong_vol_cushion_min']:.1f}x)",
        f"  {_yn(_cp is not None and _cp >= 0)} above put wall "
        f"({_f(_cp, '{:+.1f}')}%)",
        f"  {_yn(not _nz.get('noisy'))} chain noise {_f(_nz.get('skew_std'), '{:.1f}')} "
        f"(bar ≤{_sel['skew_noise_std_max']:.0f})",
    ])

    parts = []
    parts.append(f"🎯 Tier A Daily Signal — ${c['ticker']}")
    parts.append(_setup)
    parts.append("")
    # SKEW SETUP (the structural read — cushion above the put wall is the key risk metric)
    pwall = c.get('put_wall_strike')
    cushion = ((entry / pwall - 1) * 100) if pwall else None
    parts.append("Skew setup — THE SIGNAL (Tier A gates passed):")
    parts.append(f"  • Spot ${entry:.2f} ({(c.get('spot_return_pct') or 0):+.1f}% / 5d)")
    parts.append(f"  • skew_change_5d {(c.get('skew_change_5d') or 0):+.1f} · near_skew {(c.get('near_skew') or 0):+.1f}")
    if pwall:
        parts.append(f"  • Put wall ${pwall} → spot {cushion:+.1f}% {'above' if cushion >= 0 else 'below'} (cushion)")
    if c.get('sector'):
        parts.append(f"  • Sector: {c.get('sector')} · near-dte {c.get('near_dte', '?')}")
    parts.append("")
    parts.append(f"🔎 UW flow — bonus confirmation only, NOT the signal ({score}/4):")
    z = r.get('z_ncp'); coi = r.get('coi_pct'); poi = r.get('poi_pct'); dp = r.get('dp_blocks_10d')
    if z is not None:   parts.append(f"  • NCP entry z-score: {z:+.2f}")
    if coi is not None: parts.append(f"  • Call OI 10d: {coi:+.1f}%")
    if poi is not None: parts.append(f"  • Put OI 10d: {poi:+.1f}%")
    if dp is not None:  parts.append(f"  • Dark pool large blocks (10d): {dp}")
    parts.append("")
    parts.append(f"Entry: ${entry:.2f}")
    parts.append(f"T1 (default exit): ${T1:.2f} (+{tps['tp1']:.0f}%)")
    parts.append(f"Stop: ${STOP:.2f} ({P.stop_pct():+.0f}%)")
    parts.append("")
    parts.append("⏱️ Short-term pullback — exits on target (win) or stop (loss), no time limit.")
    parts.append("Bot auto-closes at T1 or stop.")
    parts.append("")
    # Live results line + heat & peak block (auto-updates as trades close)
    try:
        import excursions
        rline = excursions.format_results_line()
        if rline:
            parts.append(rline)
            parts.append("")
        blk = excursions.format_excursion_block()
        if blk:
            parts.append(blk)
            parts.append("")
    except Exception as e:
        print(f'[x] track-record block skipped: {e}')
    parts.append("📊 Live track record: https://docs.google.com/spreadsheets/d/1R-PafqOjeNbReaGuM5xv5YS3xf1EvwtichPQLUKwedA")
    parts.append("")
    parts.append("⚠️ Quant research only. NOT financial advice.")
    return "\n".join(parts)


def format_close_for_x(ticker: str, entry: float, exit_price: float, reason: str,
                       entry_date: str = None, exit_date: str = None,
                       mae_pct: float = None, mfe_pct: float = None) -> str:
    """Full-detail close post — WINS AND LOSSES REPORTED IDENTICALLY.

    Transparency rule: a public entry must always get a public outcome. A loss is
    posted with the same prominence and detail as a win; nothing is quietly dropped.
    """
    ret = (exit_price / entry - 1) * 100
    win = ret > 0
    if reason == 'TP1':
        head = f"📍 ${ticker} — TARGET HIT ✅"
    elif reason == 'STOP':
        head = f"🛑 ${ticker} — STOPPED OUT ❌"
    else:
        head = f"⏱️ ${ticker} — CLOSED ({reason})"

    parts = [head, ""]
    dates = ''
    if entry_date:
        dates = f" ({entry_date}{' → ' + exit_date if exit_date else ''})"
    parts.append(f"Entry ${entry:.2f} → Exit ${exit_price:.2f}{dates}")
    parts.append(f"Result: {ret:+.2f}%")
    parts.append("")

    if mae_pct is not None or mfe_pct is not None:
        if mae_pct is not None:
            parts.append(f"Worst drawdown held through: {mae_pct:+.1f}%")
        if mfe_pct is not None:
            parts.append(f"Best unrealised reached: {mfe_pct:+.1f}%")
        parts.append("")

    if reason == 'STOP':
        parts.append("Stop is hard at -7%. Taken without hesitation — "
                     "the losses we publish are the reason the wins mean anything.")
        parts.append("")

    # Updated record + heat/peak — recomputed AFTER this trade was logged, so it includes it
    try:
        import excursions
        rline = excursions.format_results_line()
        if rline:
            parts.append(rline); parts.append("")
        blk = excursions.format_excursion_block()
        if blk:
            parts.append(blk); parts.append("")
    except Exception as e:
        print(f'[x] close track-record block skipped: {e}')

    parts.append("📊 Live track record: https://docs.google.com/spreadsheets/d/1R-PafqOjeNbReaGuM5xv5YS3xf1EvwtichPQLUKwedA")
    parts.append("")
    parts.append("⚠️ Quant research only. NOT financial advice.")
    return "\n".join(parts)


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    # Smoke test
    if all(os.environ.get(k) for k in ['X_API_KEY','X_API_SECRET','X_ACCESS_TOKEN','X_ACCESS_SECRET']):
        print('[x] credentials present — ready to post')
    else:
        print('[x] X credentials missing — set X_API_KEY etc in .env to enable')
