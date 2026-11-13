import hashlib

import pytest

from backend.tools.retrieval_io import read_json_input


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'[{"metric":"mrr","min_delta":0,"min_delta":-1}]',
                                 b'{"nested":{"x":1,"x":2}}', b'[NaN]', b'[Infinity]', b'[-Infinity]', b'[1e999]'])
def test_rejects_ambiguous_or_nonfinite_json_at_any_depth(tmp_path, raw):
    path = tmp_path / "input.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        read_json_input(path)


def test_repeated_keys_in_separate_objects_are_valid_and_bom_is_hashed(tmp_path):
    path = tmp_path / "input.json"
    raw = b'\xef\xbb\xbf[{"a":1},{"a":2}]'
    path.write_bytes(raw)
    value, metadata = read_json_input(path)
    assert value == [{"a": 1}, {"a": 2}]
    assert metadata["sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("tool", ["eval", "export", "review"])
def test_all_cli_inputs_use_strict_reader_and_do_not_write_output(tmp_path, monkeypatch, tool):
    from backend.tools.retrieval_eval import main as eval_main
    from backend.tools.retrieval_export import main as export_main
    from backend.tools.retrieval_review import main as review_main

    source, output = tmp_path / "input.json", tmp_path / "report.json"
    source.write_text('[{"query":"q","relevant_ids":["a"],"ranked_ids":[],"query":"changed"}]', encoding="utf-8")
    args = {"eval": [str(source), "--output", str(output)],
            "export": [str(source), str(output), "--threshold", ".3"],
            "review": [str(source), "--baseline", str(source), "--output", str(output)]}
    monkeypatch.setattr("sys.argv", [tool, *args[tool]])
    with pytest.raises(SystemExit) as exc:
        {"eval": eval_main, "export": export_main, "review": review_main}[tool]()
    assert exc.value.code == 2
    assert not output.exists()
