"""Explicit reward definition for offline contextual-bandit experiments."""
import math


def reward(score, attempts, duration, duration_scale=60.0):
    values = (score, attempts, duration, duration_scale)
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError("reward inputs must be finite")
    if not 0 <= score <= 1 or attempts < 1 or duration < 0 or duration_scale <= 0:
        raise ValueError("invalid reward inputs")
    return score - .1 * (attempts - 1) - .05 * duration / duration_scale
