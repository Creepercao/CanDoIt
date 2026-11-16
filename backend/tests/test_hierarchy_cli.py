import json
import os
from pathlib import Path
import subprocess
import sys

from backend.chunking import prepare_document


ROOT = Path(__file__).resolve().parents[2]


def run_cli(store, *args):
    return subprocess.run([sys.executable, "-m", "backend.tools.hierarchy_store", "--store", str(store), *map(str, args)],
                          cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                          capture_output=True, text=True, encoding="utf-8", timeout=20)


def test_import_reopen_activate_and_context_in_separate_processes(tmp_path):
    snapshot = prepare_document("# 商品\n" + "原文" * 200 + "\n# 售后\n尾部", "manual", 50, 10)
    source, store = tmp_path / "snapshot.json", tmp_path / "store.sqlite"
    source.write_text(json.dumps(snapshot), encoding="utf-8")
    imported = run_cli(store, "import", source)
    assert imported.returncode == 0, imported.stderr
    snapshot_id = json.loads(imported.stdout)["snapshot_id"]
    assert json.loads(run_cli(store, "list").stdout)["versions"][0]["active"] is False
    assert run_cli(store, "activate", snapshot_id).returncode == 0
    ids = tmp_path / "ids.json"
    ids.write_text(json.dumps([snapshot["chunks"][-1]["id"]]), encoding="utf-8")
    output = tmp_path / "context.json"
    result = run_cli(store, "--output", output, "context", ids, "--max-chars", 100)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == json.loads(output.read_text(encoding="utf-8"))
    assert json.loads(result.stdout)["contexts"][0]["text"].endswith("尾部")
    assert run_cli(store, "--output", output, "context", ids).returncode == 2
    verified = run_cli(store, "verify")
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout) == {"valid": True, "snapshots": 1, "active_sources": 1}


def test_missing_or_invalid_input_does_not_create_a_store(tmp_path):
    store = tmp_path / "store.sqlite"
    assert run_cli(store, "list").returncode == 2
    assert not store.exists()
    source = tmp_path / "bad.json"
    source.write_text('{}', encoding="utf-8")
    assert run_cli(store, "import", source).returncode == 2
    assert not store.exists()
