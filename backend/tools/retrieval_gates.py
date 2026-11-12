"""Apply explicit quality requirements to a saved-ranking comparison."""

import math


def check_gates(comparison, rules):
    """Check all rules; undefined denominators fail instead of passing silently.

    Metrics use a higher-is-better direction. Rules can require a candidate
    minimum, a minimum candidate-minus-baseline delta, or both, per slice.
    """
    if not isinstance(rules, list) or not rules:
        raise ValueError("gates must be a non-empty array of rules")
    metrics = set(comparison["delta"]) - {"no_answer_retrieval_rate"}
    checks = []
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - {"metric", "slice", "min_value", "min_delta"}:
            raise ValueError("each gate must contain only metric, slice, min_value and min_delta")
        metric = rule.get("metric")
        if not isinstance(metric, str) or metric not in metrics:
            raise ValueError("gate metric must name a higher-is-better metric at the selected k")
        thresholds = {key: rule[key] for key in ("min_value", "min_delta") if key in rule}
        if not thresholds:
            raise ValueError("each gate needs min_value or min_delta")
        for key, value in thresholds.items():
            lower = 0 if key == "min_value" else -1
            if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= 1:
                raise ValueError(f"{key} must be a finite number between {lower} and 1")
        scope = comparison
        if "slice" in rule:
            tag = rule["slice"]
            if not isinstance(tag, str) or tag not in comparison["slices"]:
                raise ValueError("gate slice must name an existing business tag")
            scope = comparison["slices"][tag]
        values = {"baseline": scope["baseline"][metric], "candidate": scope["candidate"][metric],
                  "delta": scope["delta"][metric]}
        defined = all(value is not None for value in values.values())
        passed = defined and all(
            values["candidate" if key == "min_value" else "delta"] >= minimum
            or math.isclose(values["candidate" if key == "min_value" else "delta"], minimum,
                            rel_tol=0, abs_tol=1e-12)
            for key, minimum in thresholds.items()
        )
        checks.append({**rule, **values, "passed": passed,
                       "reason": "passed" if passed else ("below_threshold" if defined else "undefined_metric")})
    return {"passed": all(check["passed"] for check in checks), "checks": checks}
