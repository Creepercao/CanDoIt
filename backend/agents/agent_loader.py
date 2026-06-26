"""Load built-in sub-agent configs from agents.yaml and bridge to workers.

Reads the declarative YAML file, resolves each agent's ``worker`` string
to the actual callable from ``builtin_workers.WORKER_MAP``, and returns
a list of ``Skill`` instances ready for registration in the SkillRegistry.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from backend.skills.base import Skill
from backend.agents.builtin_workers import WORKER_MAP

logger = logging.getLogger("agent_loader")

_AGENTS_YAML = Path(__file__).parent / "agents.yaml"


def load_builtin_agents() -> list[Skill]:
    """Parse agents.yaml and return Skill instances with workers resolved.

    Each agent entry's ``worker`` field is a string key into
    ``WORKER_MAP``.  The loader validates that every referenced worker
    exists and raises ``ValueError`` on mismatch.

    Returns
    -------
    list[Skill]
        Fully wired Skill instances — ``worker`` is the actual async
        callable from ``builtin_workers.py``, not a string.
    """
    with open(_AGENTS_YAML, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    raw_list: list[dict] = data.get("agents", []) if isinstance(data, dict) else []

    skills: list[Skill] = []
    for item in raw_list:
        worker_name = item.pop("worker", None)
        if not worker_name:
            logger.warning(
                f"Agent '{item.get('name')}' has no 'worker' field — skipping"
            )
            continue

        worker_func = WORKER_MAP.get(worker_name)
        if worker_func is None:
            raise ValueError(
                f"Agent '{item.get('name')}' references worker '{worker_name}' "
                f"which is not in WORKER_MAP. "
                f"Available workers: {list(WORKER_MAP.keys())}"
            )

        skills.append(Skill(
            worker=worker_func,
            **item,
        ))

    logger.info(
        f"Loaded {len(skills)} built-in agent(s) from agents.yaml: "
        f"{[s.name for s in skills]}"
    )
    return skills
