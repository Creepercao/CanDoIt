"""LangGraph multi-agent state and graph definition.

Agents: supervisor, research, analyst, chart, image_gen, video_gen, code
        + skill agents (auto-discovered from backend.skills)

Data flow: research(scrape) → analyst(extract structured data) → chart(render)

Graph topology (dynamic):
  - Built-in chain: research → analyst → chart (skill chain agents inserted after deps)
  - Independent workers (image, video, code + skill independents) run in parallel via Send
  - All paths converge at synthesizer, then END.

The graph is built lazily via get_multi_agent_graph() so that skill
enable/disable toggles can trigger a rebuild.
"""
from __future__ import annotations

import operator
import json
import re
import logging
from typing import Annotated, Any, TypedDict

from langgraph.graph import StateGraph, END
from langgraph.constants import Send
from langchain_core.messages import HumanMessage, BaseMessage

from backend.models.provider import create_chat_model
from backend.cache import cache
from backend.skills.registry import skill_registry, _merge_skill_outputs
from backend.agents.builtin_workers import synthesizer_node
from backend.prompts import SUPERVISOR_PROMPT_TEMPLATE

logger = logging.getLogger("graph")


# ── State ──

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], operator.add]
    user_request: str
    tasks: list[dict[str, Any]]
    research_results: Annotated[list[dict[str, Any]], operator.add]
    analyst_results: Annotated[list[dict[str, Any]], operator.add]
    chart_results: Annotated[list[dict[str, Any]], operator.add]
    image_results: Annotated[list[dict[str, Any]], operator.add]
    video_results: Annotated[list[dict[str, Any]], operator.add]
    code_results: Annotated[list[dict[str, Any]], operator.add]
    skill_outputs: Annotated[dict[str, list[dict[str, Any]]], _merge_skill_outputs]
    final_response: str
    chat_model_id: str
    image_model_id: str
    video_model_id: str
    router_model_id: str
    _skip_synthesizer: bool  # If True, synthesizer_node is a no-op (streaming path handles it)


# ── Supervisor ──

def _build_supervisor_prompt() -> str:
    """Build the supervisor prompt dynamically, including skill agent descriptions
    and auto-generated routing rules from the skill registry."""
    skills_section = skill_registry.build_skills_section()
    routing_rules = skill_registry.build_routing_rules()
    prompt = SUPERVISOR_PROMPT_TEMPLATE.replace("{skills_section}", skills_section)
    return prompt.replace("{routing_rules}", routing_rules)


async def supervisor_node(state: AgentState) -> dict:
    user_req = state["user_request"]
    cache_key = f"route:{cache.hash_key(user_req)}"

    router_id = state.get("router_model_id", "") or state.get("chat_model_id", "")
    llm = create_chat_model(
        model_id=router_id or "deepseek-ai/DeepSeek-V3",
        temperature=0.1, max_tokens=256, provider_config=None,
    )

    prompt = _build_supervisor_prompt().replace("{user_request}", user_req)
    response = await llm.ainvoke([HumanMessage(content=prompt)])
    content = response.content if hasattr(response, "content") else str(response)

    json_match = re.search(r'\{.*\}', content, re.DOTALL)
    tasks = []
    direct = ""
    if json_match:
        try:
            data = json.loads(json_match.group())
            tasks = data.get("tasks", [])
            direct = data.get("direct_response", "")
        except json.JSONDecodeError:
            direct = content

    if direct and not tasks:
        return {"tasks": [], "final_response": direct}
    if tasks:
        await cache.set(cache_key, tasks, ttl=300)
    return {"tasks": tasks}


# ── Routing ──
# All agent metadata (names, dependencies, node mappings) is now sourced
# from the SkillRegistry — built-in agents are registered there at startup
# via skill_registry.register_builtins(BUILTIN_SKILLS).
#
# Worker functions live in builtin_workers.py and are imported directly.


def _get_agent_map() -> dict[str, str]:
    """Build agent→node mapping from registry (covers both built-in and skill agents)."""
    return skill_registry.get_node_for_agent()


def _get_independent_agents() -> set[str]:
    """All agent types that can run in parallel (no upstream dependencies)."""
    return skill_registry.get_independent_agents()


def _get_chain_agents() -> list[str]:
    """Ordered list of agent types in the sequential chain."""
    return skill_registry.get_chain_agents()


def _base_from_state(state: dict) -> dict:
    """Extract model config from state for Send payload."""
    return {
        "chat_model_id": state.get("chat_model_id", ""),
        "image_model_id": state.get("image_model_id", ""),
        "video_model_id": state.get("video_model_id", ""),
    }


def _make_chain_router(
    my_agent_type: str,
    agent_map: dict[str, str],
    remaining_chain: list[str],
):
    """Create a routing function for a chain agent.

    After this agent completes, dispatch the next agent in the chain
    that has tasks. If no more chain agents have tasks, go to synthesizer.

    Returns a closure usable as a LangGraph conditional edge function.
    """
    async def router(state: AgentState):
        tasks = state.get("tasks", [])
        agent_types_in_tasks = set(t.get("agent", "") for t in tasks)
        base = _base_from_state(state)

        try:
            my_idx = remaining_chain.index(my_agent_type)
        except ValueError:
            return "synthesizer"

        for next_agent in remaining_chain[my_idx + 1:]:
            if next_agent in agent_types_in_tasks:
                next_node = agent_map.get(next_agent)
                if next_node:
                    # For skill agents, verify dependencies are met
                    skill = skill_registry.get(next_agent)
                    if skill and not skill.is_independent:
                        unmet = [d for d in skill.depends_on
                                 if d not in agent_types_in_tasks]
                        if unmet:
                            continue  # skip — deps not satisfied
                    return [Send(next_node, base)]

        return "synthesizer"

    return router


def route_after_supervisor(state: AgentState):
    """Fan out to entry-point workers only.

    Independent workers always get Send if tasks exist.
    The chain: only Send to the FIRST chain agent that has tasks.
    Subsequent chain agents are triggered by conditional edges from their upstream node.
    """
    tasks = state.get("tasks", [])
    if state.get("final_response") or not tasks:
        return END

    agent_types = set(t.get("agent", "") for t in tasks)
    base = _base_from_state(state)
    agent_map = _get_agent_map()
    sends = []

    # Independent workers — dispatch directly (parallel with chain)
    for agent in _get_independent_agents():
        if agent in agent_types:
            node = agent_map.get(agent)
            if node:
                sends.append(Send(node, base))

    # Chain entry: only Send to the earliest chain agent that has tasks
    for agent in _get_chain_agents():
        if agent in agent_types:
            node = agent_map.get(agent)
            if node:
                sends.append(Send(node, base))
            break  # Only the first in the chain gets dispatched

    if not sends:
        return END
    return sends




# ── Build Graph ──

def build_graph() -> StateGraph:
    """Build the multi-agent graph incorporating both built-in and skill agents.

    The graph topology adapts to the currently enabled skills:
    - Skill independent agents are added as parallel workers (like image/video/code)
    - Skill chain agents are inserted into the research→analyst→chart chain
    - All workers converge at synthesizer → END
    """
    workflow = StateGraph(AgentState)

    # ── Core nodes (always present) ──
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("synthesizer", synthesizer_node)

    # ── All worker nodes (built-in + skill) from the registry ──
    # Built-in agents now have workers populated via agent_loader, so
    # get_node_funcs() covers everything — no more manual WORKER_MAP iteration.
    all_worker_nodes_map = skill_registry.get_node_funcs()
    for name, func in all_worker_nodes_map.items():
        workflow.add_node(name, func)
    all_worker_nodes = list(all_worker_nodes_map.keys())

    workflow.set_entry_point("supervisor")

    # ── Supervisor fan-out ──
    workflow.add_conditional_edges(
        "supervisor", route_after_supervisor,
        {n: n for n in all_worker_nodes} | {END: END}
    )

    # ── Chain routing ──
    # Re-fetch agent_map (might have been updated by skill node registration)
    agent_map = _get_agent_map()
    chain = _get_chain_agents()

    for i, agent_type in enumerate(chain):
        node_name = agent_map.get(agent_type)
        if not node_name or node_name not in workflow.channels:
            continue

        # Build the set of possible next nodes for the edge map
        next_options: dict[str, str] = {}
        for j in range(i + 1, len(chain)):
            next_agent = chain[j]
            next_node = agent_map.get(next_agent)
            if next_node and next_node in workflow.channels:
                next_options[next_node] = next_node
        next_options["synthesizer"] = "synthesizer"

        router = _make_chain_router(agent_type, agent_map, chain)
        workflow.add_conditional_edges(node_name, router, next_options)

    # ── Terminal workers → synthesizer ──
    for agent in _get_independent_agents():
        node = agent_map.get(agent)
        if node and node in workflow.channels:
            workflow.add_edge(node, "synthesizer")

    # Synthesizer → END
    workflow.add_edge("synthesizer", END)

    return workflow.compile()


# Lazy graph — rebuilt when skills are toggled
_multi_agent_graph = None


def get_multi_agent_graph():
    """Return the compiled graph, building it on first access or after skill toggle."""
    global _multi_agent_graph
    if _multi_agent_graph is None:
        _multi_agent_graph = build_graph()
    return _multi_agent_graph


def rebuild_graph():
    """Force graph rebuild (call after enabling/disabling skills)."""
    global _multi_agent_graph
    _multi_agent_graph = None
    return get_multi_agent_graph()
