"""Chronological advisory replay, not causal off-policy reward estimation."""
from backend.agents.policy import LinUCB
from backend.tools.policy_samples import samples


def replay(rows, alpha=1.0):
    observations = samples(rows)
    model = LinUCB(alpha=alpha)
    matches, matched_reward, details = 0, 0., []
    for agent, candidates, x, observed_reward in observations:
        suggestion = model.recommend(candidates, x)
        matched = suggestion == agent
        matches += matched
        if matched:
            matched_reward += observed_reward
        details.append({"logged_agent": agent, "suggested_agent": suggestion,
                        "observed_reward": observed_reward, "matched": matched})
        model.update(agent, x, observed_reward)
    return {"observations": len(details), "agreement": matches / len(details),
            "matched_reward_mean": matched_reward / matches if matches else None,
            "warning": "Descriptive replay only; unchosen agents have no observed reward. "
                       "Agreement and matched rewards do not prove policy improvement.",
            "details": details}
