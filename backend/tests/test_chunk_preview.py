import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "backend.tools.chunk_preview", *map(str, args)],
                          cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                          capture_output=True, text=True, encoding="utf-8", timeout=20)


def test_cli_keeps_bom_fingerprint_crlf_offsets_and_long_tail(tmp_path):
    source, output = tmp_path / "文档.md", tmp_path / "chunks.json"
    text = "# 商品\r\n" + "原文" * 3000 + "\r\n# 售后\r\n末尾"
    raw = b"\xef\xbb\xbf" + text.encode("utf-8")
    source.write_bytes(raw)
    result = run_cli(source, "--source", "product-guide", "--max-chars", 200,
                     "--overlap-chars", 20, "--output", output)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == json.loads(output.read_text(encoding="utf-8"))
    assert report["document"]["text"] == text
    assert report["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["document"]["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert report["chunks"][-1]["text"].endswith("末尾")
    assert all(text[item["start"]:item["end"]] == item["text"] for item in report["chunks"])
    output.write_text("keep manual artifact", encoding="utf-8")
    repeat = run_cli(source, "--source", "product-guide", "--output", output)
    assert repeat.returncode == 2
    assert repeat.stdout == ""
    assert output.read_text(encoding="utf-8") == "keep manual artifact"


def test_invalid_configuration_and_encoding_do_not_create_artifacts(tmp_path):
    source, output = tmp_path / "text.md", tmp_path / "output.json"
    source.write_text("# heading\ntext", encoding="utf-8")
    result = run_cli(source, "--source", "guide", "--max-chars", 5, "--overlap-chars", 5, "--output", output)
    assert result.returncode == 2
    assert not output.exists()
    source.write_bytes(b"\xff")
    result = run_cli(source, "--source", "guide", "--output", output)
    assert result.returncode == 2
    assert not output.exists()
