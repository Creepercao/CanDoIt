"""Centralised prompt templates — edit here to tune agent behaviour.

All supervisor, worker, and synthesizer prompts live in this file so they
can be tweaked without touching graph or routing logic.
"""

# ── Supervisor ────────────────────────────────────────────────────────

SUPERVISOR_PROMPT_TEMPLATE = """Route user request to agents. Output JSON only.

Agents:
- research: web search + scraping for real info/data/numbers
- analyst: extract structured numerical data from research text → produce table-ready data. Use AFTER research.
- chart: render data charts/tables (bar/line/pie/scatter) from analyst's structured data. Use AFTER analyst. For statistical/numerical data ONLY.
- image_gen: creative/artistic images, illustrations, photos
- video_gen: video, animation
- code: programming, scripts, HTML, app code
{skills_section}
Key rules:
- "搜索/查数据 + 图表" → research → analyst → chart (all 3, sequential)
- "图表 from data" without search → research → analyst → chart (to find + extract + render)
- photo/illustration/art → image_gen (parallel with research if both needed)
- 流程图/概念图/原理演示/AI模型可视化 → flowchart skill (parallel with other agents)
- 架构图/时序图/数据流图/状态机/工作流图 → dynamic-archify skill (parallel with other agents)
- 网络协议可视化(TCP/IP/HTTP/路由/数据包) → network-protocol-viz skill (parallel with other agents)
- PPT演示/幻灯片/翻页演示 → ppt-animation skill (parallel with other agents)
- 学习笔记/知识总结/漏洞笔记 → 学霸笔记 skill (parallel with other agents)
- NOTE: chart agent is ONLY for numerical data charts. For flowcharts, diagrams, architecture, protocol viz → use the corresponding skill agent.
- Simple chat → direct_response only.

{{
    "tasks": [{"agent": "...", "prompt": "..."}],
    "direct_response": "reply here if no tools needed, else empty"
}}

Request: {user_request}"""


# ── Research Worker ───────────────────────────────────────────────────

RESEARCH_QUERY_PROMPT = """Generate 3 optimized search queries for this request. \
Use different angles (English if original is Chinese, specific terms, site: filters). \
Output one query per line, no numbering.

{text}"""

RESEARCH_SYNTHESIS_PROMPT = """Synthesize key information from these search results. \
Include ALL specific numbers, statistics, data points, names, and facts found. \
Be exhaustive and cite which source each fact came from.

{text}"""


# ── Analyst Worker ────────────────────────────────────────────────────

ANALYST_PROMPT = """Extract structured numerical data from the text below. Output VALID JSON only.

Chart type selection rules (pick the BEST fit):
- "bar": comparing categories side-by-side (teams, products, countries, rankings)
- "horizontal_bar": bar chart with long category names (over 8 chars)
- "line": single trend over TIME (years, months, dates — ONE series)
- "multi_line": MULTIPLE trends over time (2+ lines on same chart)
- "pie": proportions, percentages, parts of a whole (values sum to ~100%)
- "scatter": correlation between two numeric variables
- "area": cumulative or stacked values over time

Return format:
{{
    "viable": true,
    "chart_type": "bar",
    "title": "descriptive chart title in Chinese",
    "x_label": "X axis label",
    "y_label": "Y axis label",
    "labels": ["Category A", "Category B", "Category C"],
    "datasets": [{{"label": "Data Series Name", "values": [10, 20, 30]}}]
}}

CRITICAL RULES:
- ONLY use numbers explicitly present in the text — NEVER invent or estimate
- labels and values arrays must be the SAME length
- Each dataset must have a "label" (string) and "values" (array of numbers)
- If no real numbers found → {{"viable": false}}
- Extract ALL available data points, not just the first few

Example for sports standings:
{{
    "viable": true, "chart_type": "bar", "title": "NBA西部排名积分",
    "x_label": "球队", "y_label": "胜场",
    "labels": ["雷霆", "掘金", "森林狼", "快船", "独行侠"],
    "datasets": [{{"label": "胜场", "values": [57, 56, 55, 51, 50]}},
                 {{"label": "负场", "values": [25, 26, 27, 31, 32]}}]
}}

Text to extract from:
{text}"""


# ── Image Worker ──────────────────────────────────────────────────────

IMAGE_ENHANCE_PROMPT = """Enhance to detailed image prompt. Output only prompt.
Input: "{prompt}\""""


# ── Code Worker ───────────────────────────────────────────────────────

CODE_WORKER_PROMPT = """Write clean code. Return only code.
Task: {prompt}"""


# ── Synthesizer ───────────────────────────────────────────────────────

SYNTHESIZER_PROMPT_TEMPLATE = """Synthesize agent results into a comprehensive markdown response.

Original request: {user_request}

Research data: {research}
Analyst structured data: {analyst}
Charts generated: {charts}
Images generated: {images}
Videos generated: {videos}
Code generated: {codes}
Skill agent outputs: {skill_outputs}

CRITICAL — Include these markdown tables VERBATIM in your response:
{table_blocks}

Instructions:
- Present findings clearly with markdown formatting
- If charts were generated, describe them and include links using ![](url) syntax
- If images were generated, include them using ![](url) syntax
- Include the data tables above EXACTLY AS-IS for precise numbers
- Cite sources from research data when available
- If skill agents generated HTML files, reference them with descriptive links using markdown link syntax [title](url)
- Be helpful and direct"""
