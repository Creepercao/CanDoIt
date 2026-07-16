# Feishu/Lark Dedicated Backend

The Feishu/Lark integration now lives in a parallel backend variant:

```bash
python -m uvicorn backend_lark.main:app --host 0.0.0.0 --port 8001 --reload
```

Docker:

```bash
docker compose -f docker-compose.lark.yml up --build
```

This app reuses the core CanDoIt Agent Loop, Skills, PPT pipeline, PPTX export,
PPT run store, Redis cache, and `/outputs` static artifacts, but exposes a
Feishu/Lark-shaped API surface instead of the normal web app API.

## Runtime Split

Normal backend:

```text
backend.main:app
```

Feishu/Lark backend:

```text
backend_lark.main:app
```

They are parallel entry points. The Feishu/Lark backend is not mounted into the
normal backend.

## Endpoints

Health:

```http
GET /api/health
```

Agent catalog:

```http
GET /api/agents
```

Dedicated agents:

```http
POST /api/agents/research
POST /api/agents/document
POST /api/agents/ppt
POST /api/agents/meeting
POST /api/agents/data-report
POST /api/agents/automation
POST /api/dispatch
```

Feishu/Lark event adapters:

```http
POST /api/feishu/events
POST /api/feishu/dispatch
```

PPT support endpoints reused from the core backend:

```http
POST /api/skills/ppt-animation/export-pptx
GET  /api/ppt-runs/{deck_id}
POST /api/ppt-runs/{deck_id}/slides/{slide_index}/regenerate
```

## Event Callback

`POST /api/feishu/events` supports URL verification challenge payloads:

```json
{
  "challenge": "challenge-string",
  "token": "verification-token",
  "type": "url_verification"
}
```

It also accepts Feishu/Lark message event payloads and extracts:

- text content;
- chat id;
- message id;
- sender id/open id;
- tenant key.

The generated result is returned as:

```json
{
  "code": 0,
  "msg": "ok",
  "result": {},
  "publish_actions": []
}
```

## Publish Actions

The dedicated backend returns Feishu-oriented publish actions instead of sending
messages by itself:

```json
{
  "type": "send_markdown",
  "title": "ppt result",
  "text": "markdown content",
  "chat_id": "oc_xxx",
  "message_id": "om_xxx",
  "metadata": {}
}
```

Supported action types:

- `send_markdown`
- `send_text`
- `send_link`
- `upload_file`
- `noop`

For PPT generation, `publish_actions` usually include:

- a markdown summary;
- a `send_link` action for the generated HTML deck;
- `metadata.deck_id`;
- `metadata.status_endpoint`;
- `metadata.pptx_export_endpoint`.

The Feishu bot/gateway can then decide whether to send the HTML link directly or
call the PPTX export endpoint and upload the generated file to Feishu Drive.

## Environment

Optional variables:

```bash
PUBLIC_BASE_URL=http://localhost:8001
FEISHU_VERIFICATION_TOKEN=your-verification-token
REDIS_URL=redis://localhost:6379/0
```

`PUBLIC_BASE_URL` is used to convert local `/outputs/...` paths into links that
the Feishu-side sender can publish.

`FEISHU_VERIFICATION_TOKEN` enables simple callback token verification.

## Boundary

`backend_lark` is Feishu/Lark-shaped, but it still does not store Feishu app
credentials or directly call Feishu Open API. The publish step remains a thin
sender/gateway responsibility. This keeps the AI runtime independent from
Feishu app credential rotation and message-delivery permissions.
