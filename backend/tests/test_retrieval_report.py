import json

import pytest

from backend.tools.retrieval_eval import compare, evaluate, main
from backend.tools.retrieval_gates import check_gates
from backend.tools.retrieval_report import render_markdown


def test_single_score_report_escapes_queries_and_keeps_undefined_metrics():
    result = evaluate([{"query": "护肤|产品\r\n<script>&[链接](x)", "relevant_ids": ["a"],
                        "ranked_ids": ["x", "a"], "tags": ["售后|咨询"]}], k=1)
    report = render_markdown(result)
    assert "| mrr@1 | 0 |" in report
    assert "| mrr | 0.5 |" in report
    assert r"| no\_answer\_empty\_rate | 不适用 |" in report
    assert "## 业务分组：售后&#124;咨询" in report
    assert "护肤&#124;产品<br>&lt;script&gt;&amp;\\[链接\\](x)" in report
    assert "<script>" not in report


def test_comparison_report_exposes_regressions_gates_and_group_denominators():
    baseline = [{"query": "退步", "relevant_ids": ["a"], "ranked_ids": ["a"], "tags": ["售后"]},
                {"query": "未知", "answerable": False, "relevant_ids": [], "ranked_ids": []}]
    result = compare(baseline, [{**baseline[0], "ranked_ids": []}, baseline[1]])
    result["gate_result"] = check_gates(result, [{"metric": "mrr", "min_delta": 0}])
    report = render_markdown(result)
    assert "| mrr | 1 | 0 | -1 |" in report
    assert "问题数：1；有答案：1；无答案：0" in report
    assert "结果：未通过" in report
    assert r"below\_threshold" in report
    assert "| mrr | 1 | 0 | 1 | 0 | 退步 |" in report
    assert "| 未知 | 无答案 | 不适用 | 不适用 | 不适用 | 不适用 | 0 |" in report


def test_markdown_cli_output_matches_saved_file_and_never_overwrites(tmp_path, monkeypatch, capsys):
    source, output = tmp_path / "rankings.json", tmp_path / "report.md"
    source.write_text(json.dumps([{"query": "中文问题", "relevant_ids": ["a"], "ranked_ids": ["a"]}]), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["retrieval_eval", str(source), "--format", "markdown", "--output", str(output)])
    main()
    saved = output.read_text(encoding="utf-8")
    assert saved == capsys.readouterr().out
    assert "中文问题" in saved
    assert saved.startswith("# 检索评估报告\n")
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert output.read_text(encoding="utf-8") == saved
