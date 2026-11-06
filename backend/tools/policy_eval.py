"""Frozen-model descriptive evaluation; no updates or causal reward claims."""
from backend.tools.policy_samples import samples


def evaluate(model, rows):
    observations = samples(rows)
    details = []
    for agent, candidates, x, reward in observations:
        suggested = model.recommend(candidates, x)
        details.append({"logged_agent": agent, "suggested_agent": suggested,
                        "matched": agent == suggested, "observed_reward": reward})
    return {"observations": len(details),
            "agreement": sum(row["matched"] for row in details) / len(details),
            "warning": "Descriptive agreement only; no counterfactual reward estimate.",
            "details": details}
