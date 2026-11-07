"""Deterministic plan dependency features for advisory policy observation."""
from backend.agents.policy_features import features


def plan_contexts(dependencies, budget_fraction):
    """Dependencies must already include implicit skill prerequisites."""
    pending = {key: set(value) for key, value in dependencies.items()}
    if any(dep not in pending for deps in pending.values() for dep in deps):
        raise ValueError("unknown task dependency")
    depths = {}
    while pending:
        ready = [key for key, deps in pending.items() if deps <= depths.keys()]
        if not ready:
            raise ValueError("cyclic task dependencies")
        for key in ready:
            depths[key] = max((depths[dep] + 1 for dep in pending.pop(key)), default=0)
    return {key: {"depth": depths[key], "upstream": len(set(deps)),
                  "budget_fraction": budget_fraction,
                  "features": features(depths[key], len(set(deps)), budget_fraction)}
            for key, deps in dependencies.items()}
