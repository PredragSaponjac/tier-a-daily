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

    parts = []
    parts.append(f"🎯 Tier A Daily Signal — ${c['ticker']}")
    parts.append(f"Conviction: {conviction} ({score}/4)")
    parts.append("")
    parts.append("UW composite filter — entry-day positioning:")
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
    parts.append("Bot auto-closes at T1. Track record updates publicly.")
    parts.append("")
    parts.append("⚠️ Quant research only. NOT financial advice.")
    return "\n".join(parts)


def format_close_for_x(ticker: str, entry: float, exit_price: float, reason: str) -> str:
    ret = (exit_price / entry - 1) * 100
    emoji = "📍" if reason == "TP1" else ("🛑" if reason == "STOP" else "⏱️")
    return (
        f"{emoji} ${ticker} — {reason} HIT\n"
        f"Entry ${entry:.2f} → Exit ${exit_price:.2f} ({ret:+.2f}%)\n\n"
        f"Tier A Daily auto-close. Track record updated."
    )


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    # Smoke test
    if all(os.environ.get(k) for k in ['X_API_KEY','X_API_SECRET','X_ACCESS_TOKEN','X_ACCESS_SECRET']):
        print('[x] credentials present — ready to post')
    else:
        print('[x] X credentials missing — set X_API_KEY etc in .env to enable')
