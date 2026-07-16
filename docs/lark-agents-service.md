# Local Feishu/Lark Agent Service

This project can be used as a local service backend for Feishu/Lark bots,
workflows, or gateway adapters. The endpoints below do not call Feishu/Lark APIs
directly. They only accept normalized context and return structured local AI
results.

An external Feishu/Lark adapter is responsible for:

- listening to IM or event callbacks;
- reading Doc, Sheet, Base, Slides, Drive, or Minutes content;
- calling these local service endpoints;
- publishing returned text and artifact links back to Feishu/Lark.

## Agent Catalog

```http
GET /api/lark-agents
```

Returns the four local service agents and their endpoint contracts.

## Common Source Object

All endpoints accept an optional `source` object. The backend treats it as
opaque metadata and returns it unchanged.

```json
{
  "source_type": "im|doc|sheet|base|minutes|manual",
  "source_id": "opaque external id",
  "title": "source title",
  "url": "source url",
  "chat_id": "chat id",
  "message_id": "message id",
  "sender_id": "sender id"
}
```

Common request fields:

```json
{
  "query": "user request",
  "context_text": "text extracted by the external adapter",
  "chat_model_id": "",
  "router_model_id": "",
  "metadata": {}
}
```

## Research Agent

```http
POST /api/lark-agents/research
```

Use for group chat or document questions that need research and source-aware
summaries.

```json
{
  "query": "Research loop engineering for a product brief",
  "context_text": "optional text extracted by the adapter",
  "output_format": "report",
  "source": {
    "source_type": "im",
    "chat_id": "oc_xxx",
    "message_id": "om_xxx"
  }
}
```

Key outputs:

- `response`
- `research_results`
- `tasks`

## PPT Agent

```http
POST /api/lark-agents/ppt
```

Use for generating local HTML PPT decks from Feishu/Lark context. The caller can
publish the returned `html_results` link or call the existing PPTX export API.

```json
{
  "query": "Generate a project status deck",
  "context_text": "optional source document text",
  "slide_count": 6,
  "theme": "dark-tech",
  "export_pptx": false,
  "source": {
    "source_type": "doc",
    "url": "https://..."
  }
}
```

Key outputs:

- `response`
- `html_results`
- `skill_outputs`
- `deck_id` inside each HTML result when available

Related endpoints:

```http
POST /api/skills/ppt-animation/export-pptx
GET  /api/ppt-runs/{deck_id}
POST /api/ppt-runs/{deck_id}/slides/{slide_index}/regenerate
```

## Meeting Agent

```http
POST /api/lark-agents/meeting
```

Use for Minutes, video meeting records, or manually provided transcripts.

```json
{
  "meeting_title": "Project weekly meeting",
  "participants": ["Alice", "Bob"],
  "transcript": "meeting transcript or rough notes",
  "create_action_items": true,
  "source": {
    "source_type": "minutes",
    "source_id": "minutes_xxx"
  }
}
```

Key outputs:

- `response` markdown
- recommended action items inside `response`
- `tasks`

## Data Report Agent

```http
POST /api/lark-agents/data-report
```

Use for Sheets/Base data after the external adapter has read the table.

```json
{
  "table_title": "Weekly sales report",
  "table_text": "Region,Revenue\nNorth,1200\nSouth,980",
  "table_json": [
    {"Region": "North", "Revenue": 1200},
    {"Region": "South", "Revenue": 980}
  ],
  "chart_required": true,
  "source": {
    "source_type": "sheet",
    "url": "https://..."
  }
}
```

Key outputs:

- `response`
- `chart_results`
- `analyst_results`

## Boundary

These APIs deliberately do not:

- send IM messages;
- write Docs;
- upload Drive files;
- create Slides;
- create Tasks;
- call `lark-cli`;
- store Feishu/Lark credentials.

That boundary keeps this repository deployable as a local HTTP AI service.
Feishu/Lark communication remains the responsibility of a thin adapter service.
