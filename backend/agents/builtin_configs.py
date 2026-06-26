"""Built-in agent configurations as Skill instances.

Provides ``BUILTIN_SKILLS`` — a list of ``Skill`` dataclass instances that
describe the 6 core agents (research, analyst, chart, image_gen, video_gen, code).

Each entry declares its name, display metadata, and ``depends_on`` relationship.
The graph builder and skill registry use these to construct the multi-agent topology
without hardcoding agent names or dependency chains.

Worker functions are NOT stored here — they live in ``builtin_workers.WORKER_MAP``
and are wired up by the graph builder at node-registration time.
"""

from __future__ import annotations

from backend.skills.base import Skill

# NOTE: worker is set to None — actual worker functions are resolved from
# builtin_workers.WORKER_MAP at graph-build time.  This keeps the config
# data-only and avoids coupling to LLM / tool imports.

BUILTIN_SKILLS: list[Skill] = [
    Skill(
        name="research",
        display_name="Research",
        description="Web search + scrape for real information, data, and numbers",
        emoji="🔍",
        enabled=True,
        worker=None,  # resolved from WORKER_MAP
        depends_on=[],
        prompt_contribution=None,  # use default generation
        result_key="research_results",
    ),
    Skill(
        name="analyst",
        display_name="Analyst",
        description="Extract structured numerical data from research text",
        emoji="📊",
        enabled=True,
        worker=None,
        depends_on=["research"],
        prompt_contribution=None,
        result_key="analyst_results",
    ),
    Skill(
        name="chart",
        display_name="Chart Maker",
        description="Render analyst structured data as charts (bar/line/pie/scatter)",
        emoji="📈",
        enabled=True,
        worker=None,
        depends_on=["analyst"],
        prompt_contribution=None,
        result_key="chart_results",
    ),
    Skill(
        name="image_gen",
        display_name="Image Generator",
        description="Creative/artistic images, illustrations, photos",
        emoji="🎨",
        enabled=True,
        worker=None,
        depends_on=[],
        prompt_contribution="- image_gen: creative images, illustrations, photos",
        result_key="image_results",
    ),
    Skill(
        name="video_gen",
        display_name="Video Generator",
        description="Videos and animations",
        emoji="🎬",
        enabled=True,
        worker=None,
        depends_on=[],
        prompt_contribution="- video_gen: video, animation",
        result_key="video_results",
    ),
    Skill(
        name="code",
        display_name="Programmer",
        description="Programming, scripts, HTML, application code",
        emoji="💻",
        enabled=True,
        worker=None,
        depends_on=[],
        prompt_contribution="- code: programming, scripts, app code",
        result_key="code_results",
    ),
]
