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

from backend.models.provider import get_default_chat_model, create_chat_model
from backend.models.registry import registry
from backend.tools.image_gen import generate_image
from backend.tools.video_gen import generate_video
from backend.tools.chart_gen import generate_chart
from backend.tools.data_scraper import search_and_scrape
from backend.cache import cache
from backend.skills.registry import skill_registry, _merge_skill_outputs
from backend.prompts import (
    SUPERVISOR_PROMPT_TEMPLATE,
    RESEARCH_QUERY_PROMPT,
    RESEARCH_SYNTHESIS_PROMPT,
    ANALYST_PROMPT,
    IMAGE_ENHANCE_PROMPT,
    CODE_WORKER_PROMPT,
)

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
    """Build the supervisor prompt dynamically, including skill agent descriptions."""
    skills_section = skill_registry.build_skills_section()
    return SUPERVISOR_PROMPT_TEMPLATE.replace("{skills_section}", skills_section)


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

# Built-in agent→node mapping (skill agents are merged in dynamically)
_BUILTIN_NODE_FOR_AGENT = {
    "research": "research_worker", "analyst": "analyst_worker",
    "chart": "chart_worker", "image_gen": "image_worker",
    "video_gen": "video_worker", "code": "code_worker",
}

# Built-in independent (parallel) agents
_BUILTIN_INDEPENDENT = {"image_gen", "video_gen", "code"}

# Built-in chain base order
_BUILTIN_CHAIN = ["research", "analyst", "chart"]


def _get_agent_map() -> dict[str, str]:
    """Merge built-in and skill agent→node mappings."""
    result = dict(_BUILTIN_NODE_FOR_AGENT)
    result.update(skill_registry.get_node_for_agent())
    return result


def _get_independent_agents() -> set[str]:
    """All agent types that can run in parallel (no upstream dependencies)."""
    return _BUILTIN_INDEPENDENT | skill_registry.get_independent_agents()


def _get_chain_agents() -> list[str]:
    """Ordered list of agent types in the sequential chain (built-in + skill)."""
    return skill_registry.get_chain_agents()


def _get_all_worker_nodes() -> set[str]:
    """All worker node names (built-in + skill)."""
    nodes = {
        "research_worker", "analyst_worker", "chart_worker",
        "image_worker", "video_worker", "code_worker",
    }
    nodes.update(skill_registry.get_node_funcs().keys())
    return nodes


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


# ── Research Worker (search + scrape raw pages) ──

_RESEARCH_AGENT_TYPES = {"research"}


async def research_worker(state: AgentState) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "research"]
    if not my_tasks:
        return {"research_results": []}

    llm = _get_chat_llm(state)
    results = []

    for task in my_tasks:
        prompt_text = task.get("prompt", "")
        try:
            # Generate multiple search queries for better coverage
            search_queries = [prompt_text[:80]]  # Original query first
            if len(prompt_text) > 20:
                kw_resp = await llm.ainvoke([HumanMessage(
                    content=RESEARCH_QUERY_PROMPT.format(text=prompt_text))])
                extra = kw_resp.content if hasattr(kw_resp, "content") else str(kw_resp)
                for line in extra.strip().split("\n"):
                    q = line.strip().lstrip("0123456789.-) ").strip()
                    if q and len(q) > 5:
                        search_queries.append(q[:120])

            # Search with multiple queries and merge results
            all_sources = []
            all_synthesis = []
            seen_urls = set()

            for query in search_queries[:4]:  # Max 4 queries
                scraped = await search_and_scrape(query, llm, max_pages=3)
                raw_text = scraped.get("synthesis", "")
                sources = scraped.get("sources", [])
                structured_data = scraped.get("structured_data")

                # Deduplicate sources
                for src in sources:
                    url = src.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_sources.append(src)

                if raw_text and len(raw_text) > 50:
                    all_synthesis.append(raw_text)

            # Merge and re-synthesize
            combined_text = "\n\n---\n\n".join(all_synthesis) if all_synthesis else ""
            if combined_text:
                synth = await llm.ainvoke([HumanMessage(
                    content=RESEARCH_SYNTHESIS_PROMPT.format(text=combined_text[:8000]))])
                synthesis = synth.content if hasattr(synth, "content") else str(synth)
            else:
                synthesis = "No data found from web search."

            results.append({
                "task": prompt_text, "sources": all_sources[:8],
                "synthesis": synthesis, "raw_text": combined_text[:4000],
                "structured_data": structured_data,
            })
        except Exception as e:
            logger.error(f"Research worker error: {e}")
            results.append({"task": prompt_text, "error": str(e), "synthesis": f"Search error: {e}"})

    return {"research_results": results}


# ── Analyst Worker (extract structured data from research) ──

async def analyst_worker(state: AgentState) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "analyst"]
    if not my_tasks:
        return {"analyst_results": []}

    # Build data source from research results (now available since analyst runs after research)
    research_data = state.get("research_results", [])
    data_source = ""
    if research_data:
        data_source = "\n\n".join(
            r.get("synthesis", "") + "\n" + r.get("raw_text", "")[:2000]
            for r in research_data
        )

    llm = _get_chat_llm(state)
    results = []

    for task in my_tasks:
        prompt_text = task.get("prompt", "")
        # Use research data if available, otherwise fall back to task prompt
        source = data_source or prompt_text
        try:
            prompt = ANALYST_PROMPT.format(text=source[:6000])
            resp = await llm.ainvoke([HumanMessage(content=prompt)])
            content = resp.content if hasattr(resp, "content") else str(resp)

            structured = None
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group())
                    if data.get("viable") and data.get("labels") and data.get("datasets"):
                        structured = data
                except json.JSONDecodeError:
                    pass

            results.append({
                "task": prompt_text,
                "structured_data": structured,
                "source_data": source[:500],
            })
        except Exception as e:
            logger.error(f"Analyst worker error: {e}")
            results.append({"task": prompt_text, "error": str(e)})

    return {"analyst_results": results}


# ── Chart Worker (render only — takes analyst's structured data) ──

async def chart_worker(state: AgentState) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "chart"]
    if not my_tasks:
        return {"chart_results": []}

    # Get structured data from analyst first, then fall back to research
    chart_spec = None
    for r in state.get("analyst_results", []):
        if r.get("structured_data"):
            chart_spec = r["structured_data"]
            break
    if not chart_spec:
        for r in state.get("research_results", []):
            if r.get("structured_data"):
                chart_spec = r["structured_data"]
                break

    results = []
    for task in my_tasks:
        prompt_text = task.get("prompt", "")
        try:
            if chart_spec:
                result = await generate_chart(
                    chart_type=chart_spec["chart_type"], title=chart_spec["title"],
                    labels=chart_spec["labels"], datasets=chart_spec["datasets"],
                    x_label=chart_spec.get("x_label", ""), y_label=chart_spec.get("y_label", ""),
                )
                table_md = _build_table_md(chart_spec)
                results.append({
                    "task": prompt_text, "chart_spec": chart_spec,
                    "result": result, "table_markdown": table_md,
                })
            else:
                results.append({
                    "task": prompt_text,
                    "result": {"error": "No structured data from analyst/research"},
                    "fallback": True,
                })
        except Exception as e:
            logger.error(f"Chart worker error: {e}")
            results.append({"task": prompt_text, "result": {"error": str(e)}})

    return {"chart_results": results}


# ── Image Worker ──

async def image_worker(state: AgentState) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "image_gen"]
    if not my_tasks:
        return {"image_results": []}

    image_model = state.get("image_model_id", "stabilityai/stable-diffusion-3-5-large")
    llm = _get_chat_llm(state)
    results = []

    for task in my_tasks:
        prompt_text = task.get("prompt", "")
        try:
            enhanced = await llm.ainvoke([HumanMessage(
                content=IMAGE_ENHANCE_PROMPT.format(prompt=prompt_text))])
            enhanced_text = enhanced.content if hasattr(enhanced, "content") else str(enhanced)
            result = await generate_image(prompt=enhanced_text.strip(), model_id=image_model)
            results.append({"task": prompt_text, "prompt_used": enhanced_text.strip(), "result": result})
        except Exception as e:
            logger.error(f"Image worker error: {e}")
            results.append({"task": prompt_text, "error": str(e)})

    return {"image_results": results}


# ── Video Worker ──

async def video_worker(state: AgentState) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "video_gen"]
    if not my_tasks:
        return {"video_results": []}

    video_model = state.get("video_model_id", "")
    results = []

    for task in my_tasks:
        prompt_text = task.get("prompt", "")
        try:
            result = await generate_video(prompt=prompt_text, model_id=video_model)
            if result.get("status") == "unsupported":
                img_result = await generate_image(
                    prompt=f"Key frame of video: {prompt_text}",
                    model_id=state.get("image_model_id", "stabilityai/stable-diffusion-3-5-large"))
                results.append({"task": prompt_text, "result": result, "fallback_image": img_result})
            else:
                results.append({"task": prompt_text, "result": result})
        except Exception as e:
            logger.error(f"Video worker error: {e}")
            results.append({"task": prompt_text, "error": str(e)})

    return {"video_results": results}


# ── Code Worker ──

async def code_worker(state: AgentState) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "code"]
    if not my_tasks:
        return {"code_results": []}

    llm = _get_chat_llm(state)
    results = []

    for task in my_tasks:
        prompt_text = task.get("prompt", "")
        try:
            resp = await llm.ainvoke([HumanMessage(
                content=CODE_WORKER_PROMPT.format(prompt=prompt_text))])
            content = resp.content if hasattr(resp, "content") else str(resp)
            results.append({"task": prompt_text, "code": content})
        except Exception as e:
            logger.error(f"Code worker error: {e}")
            results.append({"task": prompt_text, "error": str(e)})

    return {"code_results": results}


# ── Synthesizer ──

async def synthesizer_node(state: AgentState) -> dict:
    """Synthesize all worker results into a markdown response.

    When _skip_synthesizer is True (streaming path), this is a no-op —
    the streaming orchestrator handles synthesis with token-level streaming.
    """
    if state.get("final_response") or state.get("_skip_synthesizer"):
        return {}

    llm = _get_chat_llm(state)

    # Collect markdown tables
    table_blocks = ""
    for cr in state.get("chart_results", []):
        if cr.get("table_markdown"):
            title = cr.get("chart_spec", {}).get("title", "数据表")
            table_blocks += f"\n\n**{title}**\n\n{cr['table_markdown']}\n"

    prompt = f"""Synthesize agent results.

Original request: {state["user_request"]}

Research: {_fmt(state.get("research_results", []))}
Analyst data: {_fmt(state.get("analyst_results", []))}
Charts: {_fmt(state.get("chart_results", []))}
Images: {_fmt(state.get("image_results", []))}
Videos: {_fmt(state.get("video_results", []))}
Code: {_fmt(state.get("code_results", []))}
Skill outputs: {_fmt(state.get("skill_outputs", {}))}

Include these tables in response (copy verbatim):
{table_blocks}

Create comprehensive markdown response with data tables and chart descriptions."""

    # Use astream for token-level streaming via astream_events
    full = ""
    async for chunk in llm.astream([HumanMessage(content=prompt)]):
        text = chunk.content if hasattr(chunk, "content") else str(chunk)
        if text:
            full += text

    return {"final_response": full}


# ── Helpers ──

def _get_chat_llm(state: dict) -> Any:
    model_id = state.get("chat_model_id", "")
    return create_chat_model(model_id or "deepseek-ai/DeepSeek-V3", temperature=0.7, max_tokens=4096)

def _fmt(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) if data else "None"

def _build_table_md(spec: dict) -> str:
    labels = spec.get("labels", [])
    datasets = spec.get("datasets", [])
    if not labels or not datasets:
        return ""
    headers = ["类别"] + [ds.get("label", "值") for ds in datasets]
    header_line = "| " + " | ".join(headers) + " |"
    align_line = "| " + " | ".join(["---"] * len(headers)) + " |"
    rows = []
    for i, label in enumerate(labels):
        row = [str(label)]
        for ds in datasets:
            vals = ds.get("values", [])
            row.append(str(vals[i]) if i < len(vals) else "-")
        rows.append("| " + " | ".join(row) + " |")
    return header_line + "\n" + align_line + "\n" + "\n".join(rows)


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

    # ── Built-in worker nodes ──
    builtin_nodes = {
        "research_worker": research_worker,
        "analyst_worker": analyst_worker,
        "chart_worker": chart_worker,
        "image_worker": image_worker,
        "video_worker": video_worker,
        "code_worker": code_worker,
    }
    for name, func in builtin_nodes.items():
        workflow.add_node(name, func)

    # ── Skill worker nodes ──
    skill_nodes = skill_registry.get_node_funcs()
    for name, func in skill_nodes.items():
        workflow.add_node(name, func)

    workflow.set_entry_point("supervisor")

    # ── Supervisor fan-out ──
    all_worker_nodes = list(builtin_nodes.keys()) + list(skill_nodes.keys())
    workflow.add_conditional_edges(
        "supervisor", route_after_supervisor,
        {n: n for n in all_worker_nodes} | {END: END}
    )

    # ── Chain routing ──
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
            # Only add edge if not already added via chain routing
            workflow.add_edge(node, "synthesizer")

    # Built-in terminal edges (belt-and-suspenders)
    workflow.add_edge("chart_worker", "synthesizer")
    workflow.add_edge("image_worker", "synthesizer")
    workflow.add_edge("video_worker", "synthesizer")
    workflow.add_edge("code_worker", "synthesizer")

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
