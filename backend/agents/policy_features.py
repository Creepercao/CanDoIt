"""Fixed, API-free context schema shared by training and scoring."""
import math

FEATURE_NAMES = ("bias", "dependency_depth", "upstream_count", "budget_fraction")


def features(depth=0, upstream=0, budget_fraction=1.0):
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (depth, upstream, budget_fraction)):
        raise ValueError("features must be finite")
    if depth < 0 or upstream < 0 or not 0 <= budget_fraction <= 1:
        raise ValueError("invalid context")
    return [1.0, math.log1p(depth), math.log1p(upstream), float(budget_fraction)]
