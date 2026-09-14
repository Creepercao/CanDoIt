"""Shared fixtures for the backend test suite."""

from __future__ import annotations

import pytest

from backend.safety import approvals as approvals_mod
from backend.safety import audit as audit_mod
from backend.safety import settings as settings_mod
from backend.safety.approvals import ApprovalStore
from backend.safety.audit import AuditLog
from backend.safety.settings import SafetySettings


@pytest.fixture
def safety_env(monkeypatch, tmp_path):
    """Isolate the safety singletons and pin an enforcing configuration.

    ``SAFETY_LLM_REVIEW`` / ``SAFETY_GOAL_VERIFICATION`` are off so tests never
    reach the network; the deterministic parts of both are still exercised.
    """
    monkeypatch.setenv("SAFETY_MODE", "enforce")
    monkeypatch.setenv("SAFETY_AUTO_APPROVE", "0")
    monkeypatch.setenv("SAFETY_LLM_REVIEW", "0")
    monkeypatch.setenv("SAFETY_GOAL_VERIFICATION", "0")
    monkeypatch.setenv("SAFETY_AUTO_REMEDIATE", "1")
    monkeypatch.setenv("SAFETY_NONSTREAM_REVIEW", "deny")
    monkeypatch.setenv("SAFETY_APPROVAL_TIMEOUT_SECONDS", "1")
    # Tests must never reach a provider; recovery gets its own stub-based tests.
    monkeypatch.setenv("AGENT_LLM_REPLAN", "0")

    settings_mod._settings = SafetySettings(path=tmp_path / "settings.json")
    audit_mod._audit_log = AuditLog(directory=tmp_path / "audit")
    approvals_mod._store = ApprovalStore()
    try:
        yield settings_mod._settings
    finally:
        settings_mod._settings = None
        audit_mod._audit_log = None
        approvals_mod._store = None


@pytest.fixture
def temp_audit(tmp_path):
    return AuditLog(directory=tmp_path / "audit")


@pytest.fixture
def fake_skills():
    """Register throwaway agents so orchestration tests need no providers."""
    from backend.skills.base import Skill
    from backend.skills.registry import skill_registry

    saved = dict(skill_registry._skills)

    async def _research(state):
        task = state["_current_task"]
        return {"research_results": [{
            "task": task["prompt"],
            "synthesis": "示例研究综述。" * 12,
            "sources": [{"url": "https://example.com/a", "title": "e"}],
        }]}

    async def _analyst(state):
        task = state["_current_task"]
        return {"analyst_results": [{
            "task": task["prompt"],
            "structured_data": {"viable": True, "labels": ["a"], "datasets": [{"label": "n", "values": [1]}]},
        }]}

    async def _chart(state):
        task = state["_current_task"]
        return {"chart_results": [{
            "task": task["prompt"],
            "table_markdown": "| 类别 | 值 |\n| --- | --- |\n| a | 1 |",
            "result": {"url": "/outputs/charts/fake.png"},
        }]}

    async def _code(state):
        task = state["_current_task"]
        # ``_test_secret`` lets a test assert the output-review redaction path
        # without every code task carrying a credential.
        body = ("key = 'sk-abcdefghijklmnopqrstuvwxyz'"
                if task.get("_test_secret") else "print('hello')")
        return {"code_results": [{"task": task["prompt"], "result": body}]}

    skill_registry.register(Skill(
        name="research", display_name="Research", description="检索",
        worker=_research, result_key="research_results",
    ))
    skill_registry.register(Skill(
        name="analyst", display_name="Analyst", description="结构化",
        worker=_analyst, depends_on=["research"], result_key="analyst_results",
    ))
    skill_registry.register(Skill(
        name="chart", display_name="Chart", description="图表",
        worker=_chart, depends_on=["analyst"], result_key="chart_results",
    ))
    skill_registry.register(Skill(
        name="code", display_name="Code", description="代码",
        worker=_code, result_key="code_results",
    ))
    try:
        yield skill_registry
    finally:
        skill_registry._skills.clear()
        skill_registry._skills.update(saved)
