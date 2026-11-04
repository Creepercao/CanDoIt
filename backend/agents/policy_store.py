"""Versioned JSON snapshots for offline policy experiments."""
from copy import deepcopy
import math
from backend.agents.policy import LinUCB
from backend.agents.policy_features import FEATURE_NAMES


def snapshot(model):
    if model.dimension != len(FEATURE_NAMES):
        raise ValueError("unsupported feature dimension")
    return deepcopy({"version": 1, "features": list(FEATURE_NAMES),
                     "alpha": model.alpha, "arms": model.arms})


def restore(data):
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("features") != list(FEATURE_NAMES):
        raise ValueError("unsupported policy schema")
    try:
        model = LinUCB(alpha=data["alpha"])
        arms = data["arms"]
        if not isinstance(arms, dict):
            raise ValueError("invalid arms")
        for name, state in arms.items():
            model._arm(name)
            inverse, b = state
            n = model.dimension
            if len(inverse) != n or len(b) != n or any(len(row) != n for row in inverse):
                raise ValueError("invalid state dimensions")
            if any(not math.isfinite(v) for row in inverse for v in row) or any(not math.isfinite(v) for v in b):
                raise ValueError("non-finite state")
            # Cholesky validates symmetry and positive definiteness.
            lower = [[0.] * n for _ in range(n)]
            for i in range(n):
                for j in range(i + 1):
                    if abs(inverse[i][j] - inverse[j][i]) > 1e-9:
                        raise ValueError("asymmetric inverse")
                    value = inverse[i][j] - sum(lower[i][k]*lower[j][k] for k in range(j))
                    if i == j:
                        if value <= 0:
                            raise ValueError("inverse must be positive definite")
                        lower[i][j] = math.sqrt(value)
                    else:
                        lower[i][j] = value / lower[j][j]
            model.arms[name] = deepcopy((inverse, b))
        return model
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("invalid policy state") from exc
