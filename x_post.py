"""X (Twitter) auto-posting via OAuth 1.0a.

Posts the same content as Telegram, just slightly tightened for X.
User has X Premium → 4000 char/post limit (no thread splitting needed for typical signal).

Required env vars (from MEMORY.md):
- X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET
"""
import datetime as _dt
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
    return post_to_x_status(text)[0] == 'posted'


def already_posted(first_line: str, since: str | None = None) -> str | None:
    """Tweet id from x_posted.log of a post whose first line matches, logged on/after
    `since` (ISO date). Retry dedupe (re-audit R2/R4): a resumed announcement checks this
    BEFORE posting, so a post that succeeded before a crash is never published twice."""
    try:
        with open('x_posted.log', encoding='utf-8') as fh:
            for line in reversed(fh.read().splitlines()):
                parts = line.split('\t')
                if (len(parts) >= 3 and parts[2] == first_line[:80]
                        and (since is None or parts[0] >= since)):
                    return parts[1]
    except FileNotFoundError:
        pass
    return None


def post_to_x_status(text: str) -> tuple:
    """Post a single tweet. Returns (status, tweet_id):
      'posted'          2xx with a readable tweet ID; also logged when storage permits
      'rejected'        X answered with an error status: nothing was published, retry is safe
      'unknown'         no answer (timeout/connection error): it MAY have been published, so
                        it must not be retried blindly
      'not_configured'  no X credentials/client here: nothing was sent
    """
    if not all([os.environ.get(k) for k in
                ['X_API_KEY','X_API_SECRET','X_ACCESS_TOKEN','X_ACCESS_SECRET']]):
        print('[x] X API keys not set — skipping post')
        return 'not_configured', None
    try:
        s = _session()
    except Exception as e:
        print(f'[x] client unavailable: {type(e).__name__}')
        return 'not_configured', None
    try:
        resp = s.post(X_TWEET_ENDPOINT, json={'text': text}, timeout=15)
    except Exception as e:
        print(f'[x] no answer from X ({type(e).__name__}) — the post may or may not exist')
        return 'unknown', None
    if resp.status_code in (200, 201):
        # LOG THE TWEET ID (added 2026-08-10). Without it a posted call cannot be
        # found again to correct or delete: our API tier blocks timeline READS, so
        # the id returned here is the only record. The HUT retraction needed the
        # link hunted down by hand because this was never captured.
        try:
            tid = (resp.json().get('data') or {}).get('id')
        except Exception as e:
            print(f'[x] success response unreadable ({type(e).__name__}); receipt reconciliation required')
            return 'unknown', None
        if not tid:
            print('[x] success response lacks tweet ID; receipt reconciliation required')
            return 'unknown', None
        print(f'[x] posted: https://x.com/PredragSaponjac/status/{tid}')
        try:
            with open('x_posted.log', 'a', encoding='utf-8') as fh:
                fh.write(f'{_dt.datetime.utcnow().isoformat()}Z\t{tid}\t'
                         f'{text.splitlines()[0][:80]}\n')
        except Exception as e:
            print(f'[x] receipt log failed ({type(e).__name__}); retain ID in durable outbox')
        return 'posted', tid
    print(f'[x] HTTP {resp.status_code}')
    return ('rejected', None) if 400 <= resp.status_code < 500 else ('unknown', None)


def format_signal_for_x(c: dict, day_pool: list[dict], taken: list[dict] | None = None) -> str:
    """X-specific signal format. Slightly tighter than Telegram.

    X cashtag rule: only ONE $TICKER in post (X rejects 2+ cashtags as 403).
    `taken` (TAKE-ALL regime, 2026-09-02) = every other name tracked today; they are
    listed PLAIN (no $) so the post keeps exactly one cashtag. Every tracked entry is
    therefore public in the same post its close will later reference — the
    entry/close symmetry rule holds without one post per name.
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
    parts.append(f"  • Reference spot ${entry:.2f} ({(c.get('spot_return_pct') or 0):+.1f}% / 9 sessions)")
    parts.append(f"  • Skew change / 9 sessions {(c.get('skew_change_5d') or 0):+.1f} · near_skew {(c.get('near_skew') or 0):+.1f}")
    if pwall:
        parts.append(f"  • Put wall ${pwall} → spot {cushion:+.1f}% {'above' if cushion >= 0 else 'below'} (cushion)")
    if c.get('sector'):
        parts.append(f"  • Sector: {c.get('sector')} · near-dte {c.get('near_dte', '?')}")
    parts.append("")
    # UW section OMITTED ENTIRELY when there is no UW data — see alert.py. Never
    # publish a "0/4" we did not measure, and do not advertise a missing feed daily.
    # Reappears automatically when UW_API_KEY works again; no code change needed.
    if f.get('score') is not None:
        parts.append(f"🔎 UW flow — bonus confirmation only, NOT the signal ({score}/4):")
        z = r.get('z_ncp'); coi = r.get('coi_pct'); poi = r.get('poi_pct'); dp = r.get('dp_blocks_10d')
        if z is not None:   parts.append(f"  • NCP entry z-score: {z:+.2f}")
        if coi is not None: parts.append(f"  • Call OI 10d: {coi:+.1f}%")
        if poi is not None: parts.append(f"  • Put OI 10d: {poi:+.1f}%")
        if dp is not None:  parts.append(f"  • Dark pool large blocks (10d): {dp}")
        parts.append("")
    parts.append('Paper entry: next regular-session open; price pending.')
    parts.append(f"Target: +{tps['tp1']:.0f}% from that open; stop {P.stop_pct():+.0f}% (gaps may lose more).")
    parts.append('Completed daily bars; dual touches are flagged ambiguous and booked as stops.')
    parts.append("")
    parts.append("⏱️ Short-term pullback — exits on target (win) or stop (loss), no time limit.")
    parts.append("Bot auto-closes at T1 or stop.")
    parts.append("")
    # TAKE-ALL regime (2026-09-02): every gate-passing name is tracked, not just this one.
    # Named PLAIN (no $) — X rejects a second cashtag with 403. Each will get its own
    # $-tagged close post when it resolves, so entry and close stay symmetric.
    others = [t for t in (taken or []) if t.get('ticker') != c['ticker']]
    if others:
        parts.append(f"📋 Also tracked today, same rules, equal weight ({len(others)}):")
        for t in others:
            e = t.get('spot_close') or 0
            parts.append(f"  {t['ticker']}  reference {e:.2f}; next-session opening price pending")
        cap = int(P.selection_params().get('max_concurrent', 6))
        parts.append('The existing take-all policy is retained; its benefit remains unproven.')
        parts.append(f"Paper allocation per reserved slot is 1/{cap} of the book "
                     f"({cap} = the concurrent cap, not today's count).")
        parts.append("")
    # EDGE-VALIDATION block, published from 2026-08-10. Research only — it does NOT
    # affect selection. Posting it publicly turns every signal into a PRE-REGISTERED
    # prediction with a public timestamp: if the hypothesis holds we can point at
    # calls made before the outcomes existed, and if it fails we say so in public,
    # the same way we publish the losing trades.
    e = c.get('edge')
    if e:
        parts.append("🔬 Edge-validation (research only — does NOT affect selection):")
        parts.append(f"  sector_iv_rank {e['sector_iv_rank']} | skew_slope {e['skew_slope']} | iv/hv {e['iv_hv_ratio']}")
        parts.append(f"  combo(rank≥60 & slope≤−1.6): {'PASS' if e['combo_pass'] else 'no'}"
                     f" | iv/hv≥1.1: {'PASS' if e['ivr_pass'] else 'no'}")
        if e.get('stop_atr') is not None:
            warn = '  ⚠️ inside 1 daily range' if e.get('tight_stop') else ''
            parts.append(f"  stop width: −7% = {e['stop_atr']}x ATR{warn}")
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
                       mae_pct: float = None, mfe_pct: float = None,
                       excursion_bounds: dict = None, split_basis: str = None) -> str:
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

    if excursion_bounds:
        for label, prefix in [('Adverse excursion', 'mae'), ('Favorable excursion', 'mfe')]:
            lo, hi = excursion_bounds.get(prefix + '_pct_min'), excursion_bounds.get(prefix + '_pct_max')
            if lo is not None and hi is not None:
                parts.append(f'{label}: {lo:+.1f}% to {hi:+.1f}% (daily-bar bounds)')
        parts.append('Exit-bar order cannot be recovered from daily OHLC.')
        parts.append('')
    elif mae_pct is not None or mfe_pct is not None:
        parts.append('Legacy daily-bar excursions; held-period timing is unverified.')
        parts.append("")

    if split_basis:
        parts.append(f'Price basis: {split_basis}')

    if reason == 'STOP':
        parts.append('Paper barrier outcome; a gap through the stop fills at the session open.')
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
