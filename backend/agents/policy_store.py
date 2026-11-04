"""Versioned JSON snapshots for offline policy experiments."""
from copy import deepcopy
from backend.agents.policy_features import FEATURE_NAMES


def snapshot(model):
    if model.dimension != len(FEATURE_NAMES):
        raise ValueError("unsupported feature dimension")
    return deepcopy({"version": 1, "features": list(FEATURE_NAMES),
                     "alpha": model.alpha, "arms": model.arms})
