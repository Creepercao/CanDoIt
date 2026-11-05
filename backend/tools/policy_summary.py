"""Describe observation coverage without claiming counterfactual performance."""
from backend.tools.policy_samples import samples


def summarize(rows):
    observations = samples(rows)
    agents = {}
    for agent, candidates, _, reward in observations:
        bucket = agents.setdefault(agent, {"observations": 0, "reward_sum": 0.})
        bucket["observations"] += 1
        bucket["reward_sum"] += reward
    return {"observations": len(rows),
            "single_candidate_rows": sum(len(set(c)) == 1 for _, c, _, _ in observations),
            "agents": {name: {"observations": bucket["observations"],
                              "observed_reward_mean": bucket["reward_sum"] / bucket["observations"]}
                       for name, bucket in sorted(agents.items())}}
