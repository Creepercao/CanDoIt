"""Exercise the public module in a fresh process without a model or database."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "backend" / "tools" / "examples"


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "backend.tools.retrieval_eval", *map(str, args)],
                          cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                          capture_output=True, text=True, encoding="utf-8", timeout=20)


def test_example_regression_is_detected_and_all_parsed_files_are_fingerprinted(tmp_path):
    candidate = EXAMPLES / "retrieval-candidate.json"
    baseline = EXAMPLES / "retrieval-baseline.json"
    gates = EXAMPLES / "retrieval-gates.json"
    output = tmp_path / "comparison.json"
    result = run_cli(candidate, "--baseline", baseline, "--gates", gates, "--output", output)
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert report["delta"]["recall@5"] == .25
    assert report["delta"]["mrr@5"] == 0
    assert report["slices"]["多意图"]["delta"]["mrr@5"] == -.5
    assert report["gate_result"]["checks"][-1]["passed"] is False
    for role, path in (("candidate", candidate), ("baseline", baseline), ("gates", gates)):
        raw = path.read_bytes()
        assert report["inputs"][role] == {
            "path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
        }


def test_example_markdown_records_failed_gate_and_fingerprint(tmp_path):
    output = tmp_path / "report.md"
    result = run_cli(EXAMPLES / "retrieval-candidate.json", "--baseline", EXAMPLES / "retrieval-baseline.json",
                     "--gates", EXAMPLES / "retrieval-gates.json", "--format", "markdown", "--output", output)
    assert result.returncode == 1, result.stderr
    assert "结果：未通过" in result.stdout
    assert "## 输入文件指纹" in result.stdout
    assert hashlib.sha256((EXAMPLES / "retrieval-gates.json").read_bytes()).hexdigest() in result.stdout
    assert output.read_text(encoding="utf-8") == result.stdout


@pytest.mark.parametrize("bom", [False, True])
def test_utf8_inputs_are_read_once_and_hash_raw_bytes(tmp_path, bom):
    source = tmp_path / "中文排名.json"
    raw = json.dumps([{"query": "中文问题", "relevant_ids": ["a"], "ranked_ids": ["a"]}],
                     ensure_ascii=False).encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    source.write_bytes(raw)
    result = run_cli(source)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["inputs"]["candidate"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["mrr@5"] == 1


def test_invalid_encoding_or_existing_output_is_usage_error(tmp_path):
    source = tmp_path / "bad.json"
    output = tmp_path / "report.json"
    source.write_bytes(b"\xff")
    result = run_cli(source, "--output", output)
    assert result.returncode == 2
    assert not output.exists()
    output.write_text("keep", encoding="utf-8")
    result = run_cli(EXAMPLES / "retrieval-candidate.json", "--output", output)
    assert result.returncode == 2
    assert result.stdout == ""
    assert output.read_text(encoding="utf-8") == "keep"
