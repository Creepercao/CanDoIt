"""Run-history API — token usage, timings and outcomes per agent run.

``GET /api/runs``            recent run summaries (newest first)
``GET /api/runs/stats``      aggregate dashboard (tokens, failures, durations)
``GET /api/runs/{run_id}``   one full run record
``DELETE /api/runs/{run_id}`` remove a record
``GET /api/runs/pricing``    whether cost estimates are configured
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from backend.observability import delete_run, get_run, list_runs, load_pricing, overview

router = APIRouter(tags=["runs"])
logger = logging.getLogger("api.runs")


@router.get("/runs/stats")
async def runs_overview(limit: int = 200):
    """Aggregate view used by the run-history dashboard header."""
    return overview(limit=max(1, min(limit, 1000)))


@router.get("/runs/pricing")
async def runs_pricing():
    """Cost is only reported when the operator supplies prices."""
    pricing = load_pricing()
    return {
        "configured": bool(pricing),
        "models": sorted(pricing),
        "hint": (
            "设置 AGENT_MODEL_PRICING 后才会估算成本，"
            '例如 {"deepseek-ai/DeepSeek-V3": {"input": 0.27, "output": 1.1}}'
            "（单位：美元 / 百万 token）"
        ),
    }


@router.get("/runs")
async def runs_list(limit: int = 100, session_id: str = ""):
    limit = max(1, min(limit, 500))
    records = list_runs(limit=limit, session_id=session_id)
    return {"runs": records, "count": len(records)}


@router.get("/runs/{run_id}/chain")
async def run_chain(run_id: str, format: str = "json"):
    """Execution chain for one run — the ordered span tree of every step.

    ``format=text`` returns a plain-text tree, which is what you want to paste
    into an issue or a log.
    """
    record = get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"未找到运行记录 {run_id}")

    chain = record.get("chain")
    if not chain or not chain.get("spans"):
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 没有链路记录")

    if format == "text":
        return _render_chain_text(chain)
    return chain


def _render_chain_text(chain: dict) -> dict:
    """Rebuild the tree rendering from the stored span list."""
    from backend.observability.chain import RunChain

    rebuilt = RunChain(run_id=chain.get("run_id", ""))
    for raw in chain.get("spans") or []:
        if not isinstance(raw, dict):
            continue
        # Attributes were merged into the stored dict; strip the reserved keys
        # so they cannot collide with the explicit arguments.
        attributes = dict(raw.get("attributes") or {})
        for reserved in ("kind", "name", "agent", "status", "error", "parent_id"):
            attributes.pop(reserved, None)

        span = rebuilt.add(
            raw.get("kind", "step"),
            raw.get("name", ""),
            agent=raw.get("agent", ""),
            parent_id=raw.get("parent_id", ""),
            duration=float(raw.get("duration") or 0.0),
            status=raw.get("status", "ok"),
            error=raw.get("error", ""),
            **attributes,
        )
        span.span_id = raw.get("span_id", span.span_id)
    return {
        "run_id": chain.get("run_id", ""),
        "text": rebuilt.render_text(),
        "summary": chain.get("summary", {}),
    }


@router.get("/runs/{run_id}")
async def run_detail(run_id: str):
    record = get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"未找到运行记录 {run_id}")
    return record


@router.delete("/runs/{run_id}")
async def run_delete(run_id: str):
    if not delete_run(run_id):
        raise HTTPException(status_code=404, detail=f"未找到运行记录 {run_id}")
    return {"ok": True, "run_id": run_id}
