"""Strict UTF-8 JSON input and fingerprints shared by retrieval tools."""

import hashlib
import json
import math


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number exceeds finite float range")
    return number


def read_json_input(path):
    """Parse once, rejecting ambiguous keys and non-finite numbers at any depth."""
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_object,
                       parse_constant=_constant, parse_float=_float)
    return value, {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
