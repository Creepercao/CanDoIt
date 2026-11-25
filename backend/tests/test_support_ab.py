import json
import os
import sys

import pytest

from backend.tools import support_ab


def test_offline_benchmark_ignores_environment_and_protects_existing_output(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    monkeypatch.setenv("SUPPORT_INTENT_MODEL", "not-an-offline-model")
    monkeypatch.setattr(sys, "argv", ["support_ab", "--offline-only", "--output", str(output)])
    support_ab.main()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["run_mode"] == "offline" and report["model"] is None
    assert "enabled_pass" not in report["summary"]
    assert all(turn["source"] == "rules" for row in report["rows"] for turn in row["offline"]["turns"])
    assert report["rows"][22]["offline"]["passed"]
    assert os.environ["SUPPORT_INTENT_MODEL"] == "not-an-offline-model"
    original = output.read_bytes()

    def should_not_run(_):
        pytest.fail("existing output must be rejected before the benchmark starts")

    monkeypatch.setattr(support_ab, "run_arm", should_not_run)
    with pytest.raises(SystemExit) as error:
        support_ab.main()
    assert error.value.code == 2 and output.read_bytes() == original
