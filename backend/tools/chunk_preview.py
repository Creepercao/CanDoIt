"""Preview hierarchical document chunks locally, without models or a database."""

import argparse
import hashlib
import json
from pathlib import Path

from backend.chunking import prepare_document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="UTF-8 Markdown or text file")
    parser.add_argument("--source", required=True, help="Stable logical document key")
    parser.add_argument("--max-chars", type=int, default=1000)
    parser.add_argument("--overlap-chars", type=int, default=0)
    parser.add_argument("--output", type=Path, help="Also save JSON; path must not exist")
    args = parser.parse_args()
    try:
        raw = args.input.read_bytes()
        result = prepare_document(raw.decode("utf-8-sig"), args.source, args.max_chars, args.overlap_chars)
        result["input"] = {"path": str(args.input.resolve()), "bytes": len(raw),
                           "sha256": hashlib.sha256(raw).hexdigest()}
        report = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(report + "\n")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(report)


if __name__ == "__main__":
    main()
