from backend.tools.policy_summary import summarize


def test_summary_reports_coverage_and_observed_rewards():
    rows = [dict(agent="a", candidates=c, score=score, attempts=1, duration=0)
            for c, score in [(["a"], 1), (["a", "b"], 0)]]
    result = summarize(rows)
    assert result["single_candidate_rows"] == 1
    assert result["agents"]["a"]["observed_reward_mean"] == .5
