"""Validated normalized observations; callers must export traces explicitly.

JSON list, chronological order: agent, candidates, score, attempts, duration,
and optional depth/upstream/budget_fraction. No inferred run-level credit.
"""
from backend.agents.policy_features import features
from backend.agents.policy_reward import reward


def samples(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("non-empty observation array required")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("observation must be an object")
        agent, candidates = row.get("agent"), row.get("candidates")
        if (not isinstance(candidates, list) or not candidates or
            any(not isinstance(a, str) or not a.strip() for a in candidates) or
            agent not in candidates):
            raise ValueError("logged agent must belong to candidate set")
        try:
            x = features(row.get("depth", 0), row.get("upstream", 0),
                         row.get("budget_fraction", 1))
            y = reward(row["score"], row["attempts"], row["duration"])
        except (KeyError, TypeError) as exc:
            raise ValueError("missing or invalid observation fields") from exc
        result.append((agent, candidates, x, y))
    return result
