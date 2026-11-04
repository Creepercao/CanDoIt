import pytest

from backend.agents.policy import LinUCB
from backend.tools.policy_train import replay
from backend.tools.policy_train import fit


def test_fit_learns_logged_arm():
    model = fit([dict(agent="b", candidates=["a", "b"], score=1, attempts=1, duration=0)], alpha=0)
    assert model.recommend(["a", "b"], [1, 0, 0, 1]) == "b"


def test_ridge_solution_and_arm_isolation():
    model = LinUCB(2, 0)
    model.update("a", [1, 0], 1)
    model.update("a", [1, 0], 1)
    assert model.score("a", [1, 0]) == pytest.approx(2 / 3)
    assert model.score("b", [1, 0]) == 0
    assert model.recommend(["b", "a"], [1, 0]) == "a"


def test_uncertainty_decreases_with_observations():
    model = LinUCB(1)
    before = model.score("a", [1])
    model.update("a", [1], 0)
    assert model.score("a", [1]) < before


def test_replay_predicts_before_learning():
    rows = [dict(agent="b", candidates=["b", "a"], score=1, attempts=1, duration=0)]
    result = replay(rows, alpha=0)
    assert result["details"][0]["suggested_agent"] == "a"
    assert result["matched_reward_mean"] is None


@pytest.mark.parametrize("row", [{}, dict(agent="a", candidates=["b"]),
    dict(agent="a", candidates=["a"], score=2, attempts=1, duration=0)])
def test_bad_observations_rejected(row):
    with pytest.raises(ValueError):
        replay([row])


def test_cli(tmp_path, monkeypatch, capsys):
    import json
    from backend.tools.policy_train import main
    path = tmp_path / "samples.json"
    path.write_text(json.dumps([dict(agent="a", candidates=["a"], score=1,
                                    attempts=1, duration=0)]))
    monkeypatch.setattr("sys.argv", ["policy_train", str(path)])
    main()
    assert json.loads(capsys.readouterr().out)["observations"] == 1
