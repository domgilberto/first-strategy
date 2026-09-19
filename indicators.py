"""
Pure indicator maths over OHLC bars. No I/O - bars arrive from alpaca.fetch_bars.

Only what the strategy actually uses lives here. ATR is the one volatility
measure the ladder is expressed in: spacing between levels, the hard-stop
distance, the take-profit target and (through worst-case sizing) the base
order all scale with it, so the same configuration means the same *risk* in
a quiet market and a violent one.
"""


def true_range(bar, prev_close):
    """Wilder's true range: the bar's own range widened by any gap from the
    previous close, so a gap down is not mistaken for a quiet bar."""
    high, low = float(bar["h"]), float(bar["l"])
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(bars, period=14):
    """Wilder's smoothed ATR over ascending bars.

    Seeded with the simple mean of the first `period` true ranges that have a
    previous close, then smoothed as ATR_t = (ATR_{t-1} * (n-1) + TR_t) / n.
    Returns None when there are not enough bars, so a caller cannot size a
    ladder off a number that does not exist.
    """
    if period < 1 or len(bars) < period + 1:
        return None

    trs = []
    prev_close = None
    for bar in bars:
        trs.append(true_range(bar, prev_close))
        prev_close = float(bar["c"])

    value = sum(trs[1:period + 1]) / period     # trs[0] has no previous close
    for tr in trs[period + 1:]:
        value = (value * (period - 1) + tr) / period
    return value


def last_close(bars):
    return float(bars[-1]["c"]) if bars else None
