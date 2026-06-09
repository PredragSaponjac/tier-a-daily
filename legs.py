"""The "≥1 strong leg, else NO TRADE" gate — the rule earned by the SYM + RGTI losses.

Investigation (n=8, 2 losses) found NO single metric separates winners from losers
(thin-cushion winners CRSP/SMMT existed; weak-skew winner OSCR existed). But a
COMBINATION did: the two losses (SYM, RGTI) — and the correctly-skipped CAVA —
were the only candidates WEAK ON BOTH legs. Every winner had ≥1 strong leg:

  • STRONG SKEW leg     : structural skew ≤ STRONG_SKEW_MAX (deep fear-flip)
                          (CRSP −16, SMMT −7, VKTX −11)
  • STRONG CUSHION leg  : vol-adjusted cushion ≥ STRONG_VOL_CUSHION_MIN daily-moves
                          where vol_cushion = cushion% / (ATM_IV / √252)
                          (OSCR 8.4x, VKTX 3.6x)

Rule: a candidate is TRADEABLE only if strong_skew OR strong_cushion. Weak on
BOTH = NO TRADE, no matter that it cleared the mechanical Tier A screen.

CAVEAT: n=8 is tiny — this is a forward hypothesis, not a proven law. Falsifiable:
"≥1 strong leg" trades should outperform "neither" trades going forward.
"""
import math

SQRT_252 = math.sqrt(252)  # ≈ 15.87 — converts annualized IV to a 1-day move %


def assess(c: dict, strong_skew_max: float = -7.0, strong_vol_cushion_min: float = 3.0) -> dict:
    """Return leg assessment for a candidate dict.

    Needs: spot_close, put_wall_strike, atm_iv, skew (structural).
    Returns dict with cushion_pct, daily_move_pct, vol_cushion, strong_skew,
    strong_cushion, tradeable, reason.
    """
    spot = c.get('spot_close')
    pw = c.get('put_wall_strike')
    iv = c.get('atm_iv')
    skew = c.get('skew')

    cushion_pct = ((spot / pw - 1) * 100) if (spot and pw) else None
    daily_move = (iv / SQRT_252) if iv else None
    vol_cushion = (cushion_pct / daily_move) if (cushion_pct is not None and daily_move) else None

    # HARD disqualifier: spot already BELOW its put wall = no floor beneath it
    # (the falling-knife / below-the-wall structure). Violates the core premise
    # "spot holding ABOVE its put wall", so it's a no-trade regardless of skew.
    below_wall = (cushion_pct is not None and cushion_pct < 0)

    strong_skew = (skew is not None and skew <= strong_skew_max)
    strong_cushion = (vol_cushion is not None and vol_cushion >= strong_vol_cushion_min)
    tradeable = bool((strong_skew or strong_cushion) and not below_wall)

    if below_wall:
        reason = f"spot {cushion_pct:+.0f}% BELOW put wall (no floor) — NO TRADE"
    elif tradeable:
        legs = []
        if strong_skew:
            legs.append(f"skew {skew:+.0f}")
        if strong_cushion:
            legs.append(f"cushion {vol_cushion:.1f}x")
        reason = "strong leg: " + " + ".join(legs)
    else:
        reason = (f"WEAK ON BOTH (skew {skew:+.0f} > {strong_skew_max:.0f}, "
                  f"cushion {vol_cushion:.1f}x < {strong_vol_cushion_min:.0f}x) — NO TRADE"
                  if (skew is not None and vol_cushion is not None) else "insufficient data — NO TRADE")

    return {
        'cushion_pct': cushion_pct, 'daily_move_pct': daily_move, 'vol_cushion': vol_cushion,
        'strong_skew': strong_skew, 'strong_cushion': strong_cushion,
        'tradeable': tradeable, 'reason': reason,
    }
