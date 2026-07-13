"""vouch-ui — a self-hosted console over a vouch knowledge base.

One FastAPI process, one static page. The browser talks only to this app;
this app talks only to `vouch serve --transport http` (POST /rpc) and, when
a key is configured, to one OpenAI-compatible chat endpoint.

The review gate stays exactly where vouch put it: this app can search, read,
and *propose* — the LLM is never given an approve tool, and no route here
calls kb.approve / kb.reject. Reviewing stays in `vouch review-ui` / the CLI.

Config (env, or a .env file loaded by run.sh):
  VOUCH_RPC_URL      default http://127.0.0.1:8731/rpc
  VOUCH_HTTP_TOKEN   bearer token if the vouch server requires one
  LLM_API_KEY        enables chat; without it the console is read-only
  LLM_BASE_URL       default https://api.openai.com/v1 (any OpenAI-compatible)
  LLM_MODEL          default gpt-5.4
  UI_HOST/UI_PORT    default 127.0.0.1:8900
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

VOUCH_RPC_URL = os.environ.get("VOUCH_RPC_URL", "http://127.0.0.1:8731/rpc")
VOUCH_HTTP_TOKEN = os.environ.get("VOUCH_HTTP_TOKEN", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-5.4")

STATIC_DIR = Path(__file__).parent / "static"
MAX_TOOL_ROUNDS = 6
MAX_HISTORY_MESSAGES = 24

app = FastAPI(title="vouch-ui")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# in-memory chat sessions; the durable record lives in vouch, not here
_sessions: dict[str, list[dict[str, Any]]] = {}


class RpcError(Exception):
    pass


async def rpc(method: str, params: dict[str, Any] | None = None) -> Any:
    headers = {"Content-Type": "application/json", "X-Vouch-Agent": "vouch-ui"}
    if VOUCH_HTTP_TOKEN:
        headers["Authorization"] = f"Bearer {VOUCH_HTTP_TOKEN}"
    envelope = {"id": str(uuid.uuid4()), "method": method, "params": params or {}}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(VOUCH_RPC_URL, json=envelope, headers=headers)
        except httpx.HTTPError as e:
            raise RpcError(f"vouch server unreachable at {VOUCH_RPC_URL}: {e}") from e
    if resp.status_code != 200:
        raise RpcError(f"vouch /rpc returned HTTP {resp.status_code}")
    body = resp.json()
    if not body.get("ok"):
        err = body.get("error") or {}
        raise RpcError(err.get("message") or "vouch rpc error")
    return body.get("result")


# --- read routes (work with no LLM key) ------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/overview")
async def overview() -> dict[str, Any]:
    try:
        status = await rpc("kb.status")
        digest = await rpc("kb.digest", {"since": "7d", "limit": 8})
    except RpcError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"status": status, "digest": digest, "llm_configured": bool(LLM_API_KEY),
            "llm_model": LLM_MODEL if LLM_API_KEY else None}


@app.get("/api/pending")
async def pending() -> Any:
    try:
        return await rpc("kb.list_pending")
    except RpcError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.get("/api/search")
async def search(q: str, limit: int = 12) -> Any:
    try:
        return await rpc("kb.search", {"query": q, "limit": limit})
    except RpcError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.get("/api/record/{record_id}")
async def record(record_id: str) -> dict[str, Any]:
    """Resolve an id against claims, pages, entities, then sources."""
    for kind, method, key in (
        ("claim", "kb.read_claim", "claim_id"),
        ("page", "kb.read_page", "page_id"),
        ("entity", "kb.read_entity", "entity_id"),
    ):
        try:
            body = await rpc(method, {key: record_id})
            result: dict[str, Any] = {"kind": kind, "record": body}
            if kind == "claim" and body.get("evidence"):
                sources = await rpc("kb.list_sources")
                wanted = set(body["evidence"])
                result["evidence_sources"] = [
                    s for s in sources if s.get("id") in wanted
                ]
            return result
        except RpcError:
            continue
    try:
        sources = await rpc("kb.list_sources")
    except RpcError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    for s in sources:
        if s.get("id") == record_id:
            return {"kind": "source", "record": s}
    raise HTTPException(status_code=404, detail=f"no record with id {record_id!r}")


# --- chat (needs LLM_API_KEY) ----------------------------------------------

SYSTEM_PROMPT = """You are the console over a review-gated team knowledge base.

Rules:
- Answer only from the KB via your tools. Cite every claim, page, or source
  you rely on as [its-id] inline. If the KB cannot support an answer, say so
  and show the closest results — never substitute general knowledge silently.
- "What needs attention" -> kb_digest.
- When asked to remember or record something: kb_register_source with the
  user's words, then kb_propose_claim / kb_propose_page citing that source
  id. Report the proposal ids and note they await human review.
- You have no approve tool, and that is by design: reviewing is the human's
  job. Do not offer to approve anything.
- Be terse. Ids in brackets, no restating tool output verbatim.
"""

TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "kb_search",
        "description": "Search the KB (claims, pages, entities). Returns scored hits.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "kb_context",
        "description": "Assemble a cited context pack for a task or question.",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
            "required": ["task"]}}},
    {"type": "function", "function": {
        "name": "kb_read_claim",
        "description": "Read one claim by id.",
        "parameters": {"type": "object", "properties": {
            "claim_id": {"type": "string"}}, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "kb_read_page",
        "description": "Read one page by id.",
        "parameters": {"type": "object", "properties": {
            "page_id": {"type": "string"}}, "required": ["page_id"]}}},
    {"type": "function", "function": {
        "name": "kb_list_pages",
        "description": "List pages, filterable by kind and frontmatter "
                       "(e.g. type='followup', meta={'followup_status': 'open'}).",
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string"},
            "meta": {"type": "object"},
            "meta_before": {"type": "object"},
            "meta_after": {"type": "object"}}}}},
    {"type": "function", "function": {
        "name": "kb_digest",
        "description": "Reviewer briefing: pending queue, recent decisions, stale claims, followups due.",
        "parameters": {"type": "object", "properties": {
            "since": {"type": "string", "default": "7d"},
            "limit": {"type": "integer", "default": 10}}}}},
    {"type": "function", "function": {
        "name": "kb_list_pending",
        "description": "List proposals awaiting human review.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "kb_register_source",
        "description": "Register text as a content-addressed evidence source; returns its id.",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string"}, "title": {"type": "string"},
            "source_type": {"type": "string", "default": "message"}},
            "required": ["content", "title"]}}},
    {"type": "function", "function": {
        "name": "kb_propose_claim",
        "description": "File a claim PROPOSAL citing evidence source ids. Lands pending; a human approves.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}}},
            "required": ["text", "evidence"]}}},
    {"type": "function", "function": {
        "name": "kb_propose_page",
        "description": "File a page PROPOSAL (optionally a typed kind with metadata frontmatter). Lands pending.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}, "body": {"type": "string"},
            "page_type": {"type": "string", "default": "concept"},
            "metadata": {"type": "object"},
            "source_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "body"]}}},
]

_TOOL_TO_METHOD = {
    "kb_search": "kb.search",
    "kb_context": "kb.context",
    "kb_read_claim": "kb.read_claim",
    "kb_read_page": "kb.read_page",
    "kb_list_pages": "kb.list_pages",
    "kb_digest": "kb.digest",
    "kb_list_pending": "kb.list_pending",
    "kb_register_source": "kb.register_source",
    "kb_propose_claim": "kb.propose_claim",
    "kb_propose_page": "kb.propose_page",
}


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


async def _llm(messages: list[dict[str, Any]]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={"model": LLM_MODEL, "messages": messages, "tools": TOOLS},
        )
    if resp.status_code != 200:
        detail = resp.text[:300]
        raise HTTPException(status_code=502, detail=f"model endpoint error: {detail}")
    return resp.json()["choices"][0]["message"]


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    if not LLM_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="no model configured — set LLM_API_KEY (and optionally "
                   "LLM_BASE_URL / LLM_MODEL) to enable chat. Search and the "
                   "gate panel work without it.",
        )
    history = _sessions.setdefault(req.session_id, [])
    history.append({"role": "user", "content": req.message})
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history[-MAX_HISTORY_MESSAGES:],
    ]
    trace: list[dict[str, Any]] = []

    for _ in range(MAX_TOOL_ROUNDS):
        msg = await _llm(messages)
        calls = msg.get("tool_calls")
        if not calls:
            reply = msg.get("content") or "(no reply)"
            history.append({"role": "assistant", "content": reply})
            return {"reply": reply, "trace": trace}
        messages.append(msg)
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            method = _TOOL_TO_METHOD.get(name)
            if method is None:
                payload: Any = {"error": f"unknown tool {name}"}
            else:
                try:
                    payload = await rpc(method, args)
                except RpcError as e:
                    payload = {"error": str(e)}
            trace.append({"tool": name, "args": args})
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(payload, default=str)[:20000],
            })

    history.append({"role": "assistant", "content": "(stopped: tool-round limit)"})
    return {"reply": "stopped after too many tool rounds — try a narrower question",
            "trace": trace}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("UI_HOST", "127.0.0.1"),
        port=int(os.environ.get("UI_PORT", "8900")),
    )
