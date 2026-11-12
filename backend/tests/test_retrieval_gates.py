import json

import pytest

from backend.tools.retrieval_eval import compare, main
from backend.tools.retrieval_gates import check_gates


def comparison():
    baseline = [{"query": "售后", "relevant_ids": ["a"], "ranked_ids": [], "tags": ["售后"]},
                {"query": "unknown", "answerable": False, "relevant_ids": [], "ranked_ids": [], "tags": ["未知"]}]
    candidate = [{**baseline[0], "ranked_ids": ["x", "a"]}, baseline[1]]
    return compare(baseline, candidate, k=1)


def test_overall_and_slice_requirements_are_all_enforced():
    rules = [{"metric": "mrr", "min_value": .5, "min_delta": .5},
             {"metric": "recall@1", "slice": "售后", "min_delta": 0}]
    assert check_gates(comparison(), rules)["passed"] is True
    failure = check_gates(comparison(), [{"metric": "mrr@1", "min_value": .1}])
    assert failure["passed"] is False
    assert failure["checks"][0]["reason"] == "below_threshold"
    result = comparison()
    result["delta"]["mrr"] = .3 - .2
    assert check_gates(result, [{"metric": "mrr", "min_delta": .1}])["passed"]


def test_empty_denominator_fails_closed_even_with_zero_requirement():
    result = check_gates(comparison(), [{"metric": "mrr", "slice": "未知", "min_value": 0}])
    assert result["passed"] is False
    assert result["checks"][0]["reason"] == "undefined_metric"


@pytest.mark.parametrize("rules", [[], {}, [None],
    [{"metric": "mrr"}], [{"metric": "mrr", "min_delta": True}],
    [{"metric": "mrr", "min_value": float("nan")}],
    [{"metric": "mrr", "min_value": -1}], [{"metric": "mrr", "min_delta": -2}],
    [{"metric": "mrr", "min_delta": "0"}],
    [{"metric": "recall@5", "min_delta": 0}],
    [{"metric": "no_answer_retrieval_rate", "min_delta": 0}],
    [{"metric": "mrr", "slice": "typo", "min_delta": 0}],
    [{"metric": "mrr", "slice": [], "min_delta": 0}],
    [{"metric": "mrr", "min_delta": 0, "typo": 1}],
])
def test_invalid_rules_are_errors(rules):
    with pytest.raises(ValueError):
        check_gates(comparison(), rules)


@pytest.mark.parametrize("minimum,exit_code", [(0, None), (1, 1)])
def test_cli_gate_failure_keeps_report_and_uses_distinct_exit_code(tmp_path, monkeypatch, capsys, minimum, exit_code):
    rows = [{"query": "q", "relevant_ids": ["a"], "ranked_ids": []}]
    source, gates, output = (tmp_path / name for name in ("rankings.json", "gates.json", "report.json"))
    source.write_text(json.dumps(rows), encoding="utf-8")
    gates.write_text(json.dumps([{"metric": "recall@5", "min_value": minimum}]), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["retrieval_eval", str(source), "--baseline", str(source),
                                    "--gates", str(gates), "--output", str(output)])
    if exit_code:
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == exit_code
    else:
        main()
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved == json.loads(capsys.readouterr().out)
    assert saved["gate_result"]["passed"] == (exit_code is None)


def test_cli_gates_require_baseline_and_invalid_rules_never_create_output(tmp_path, monkeypatch):
    source, gates, output = (tmp_path / name for name in ("rankings.json", "gates.json", "report.json"))
    source.write_text('[{"query":"q","relevant_ids":["a"],"ranked_ids":[]}]', encoding="utf-8")
    gates.write_text('[{"metric":"typo","min_value":0}]', encoding="utf-8")
    args = ["retrieval_eval", str(source), "--gates", str(gates), "--output", str(output)]
    for suffix in ([], ["--baseline", str(source)]):
        monkeypatch.setattr("sys.argv", args + suffix)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
        assert not output.exists()
