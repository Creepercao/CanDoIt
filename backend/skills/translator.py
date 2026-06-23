"""Translator skill — translates synthesis results to a target language.

Demonstrates the skill system pattern:
- Module-level ``SKILL`` instance is auto-discovered at startup
- Worker runs as an independent agent (no dependencies)
- Writes results to ``skill_outputs["translator"]``
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage

from backend.skills.base import Skill
from backend.models.provider import create_chat_model

logger = logging.getLogger("skill.translator")


async def translator_worker(state: dict) -> dict:
    """Translate accumulated text to a target language.

    Reads from research_results and any existing final_response,
    translates to the language specified in the task prompt.
    """
    tasks = [t for t in state.get("tasks", []) if t.get("agent") == "translator"]
    if not tasks:
        return {"skill_outputs": {}}

    # Gather text to translate
    texts: list[str] = []
    for r in state.get("research_results", []):
        syn = r.get("synthesis", "")
        if syn:
            texts.append(syn)
    if state.get("final_response"):
        texts.append(state["final_response"])

    # Also check analyst and chart results for structured data
    for r in state.get("analyst_results", []):
        sd = r.get("structured_data")
        if sd:
            import json
            texts.append(json.dumps(sd, ensure_ascii=False))

    if not texts:
        return {"skill_outputs": {"translator": [{"error": "No text to translate"}]}}

    llm = create_chat_model(
        model_id=state.get("chat_model_id", ""),
        temperature=0.3,
        max_tokens=4096,
    )

    results = []
    for task in tasks:
        target_lang = task.get("prompt", "Chinese")
        combined = "\n\n".join(texts)[:8000]
        try:
            resp = await llm.ainvoke([HumanMessage(content=(
                f"Translate the following text to {target_lang}. "
                f"Preserve markdown formatting, links, image syntax ![](url), "
                f"and data tables exactly as they are. "
                f"Only output the translated text, nothing else.\n\n{combined}"
            ))])
            translated = resp.content if hasattr(resp, "content") else str(resp)
            results.append({
                "task": target_lang,
                "translated_text": translated,
                "source_length": len(combined),
            })
            logger.info(f"Translator: translated {len(combined)} chars to {target_lang}")
        except Exception as e:
            logger.error(f"Translator error: {e}")
            results.append({"task": target_lang, "error": str(e)})

    return {"skill_outputs": {"translator": results}}


SKILL = Skill(
    name="translator",
    display_name="Translator",
    description="Translate the final output to a different language (e.g. Japanese, French, English)",
    emoji="🌐",
    enabled=True,
    worker=translator_worker,
    depends_on=[],  # Independent — runs in parallel with other agents
    prompt_contribution=(
        "- translator: translate final results to another language. "
        "Runs in parallel. Prompt should be the target language "
        "(e.g. 'Japanese', 'French', 'English')."
    ),
)
