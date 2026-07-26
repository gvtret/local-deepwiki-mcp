"""local-deepwiki HTTP server: MCP (/mcp) + health + Hub UI (/ui).

Contract mirrors doc-rag Streamable HTTP MCP (POST JSON-RPC + GET SSE),
without importing or depending on doc-rag code.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from deploy.http.repos import get_manager
from local_deepwiki.server import TOOL_HANDLERS, PROGRESS_ENABLED_TOOLS, call_tool
from local_deepwiki.tool_defs import TOOL_DEFINITIONS

_VERSION = "0.1.0"
_ROOT = Path(os.environ.get("LOCAL_DEEPWIKI_ROOT", Path.cwd())).resolve()
_TEMPLATES = Jinja2Templates(directory=str(_ROOT / "deploy" / "hub" / "templates"))
_TOOL_SEM = asyncio.Semaphore(int(os.environ.get("LOCAL_DEEPWIKI_TOOL_CONCURRENCY", "4")))
_PUBLIC_BASE = os.environ.get("LOCAL_DEEPWIKI_PUBLIC_BASE", "").rstrip("/")
_ACTIVE_REPO_COOKIE = "ldw_active_repo"

Json = dict[str, Any] | list[Any]


def _ok(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _is_notification(req: dict[str, Any]) -> bool:
    return ("id" not in req) or (req.get("id") is None)


def _origin_allowed(origin: str | None, request: Request | None = None) -> bool:
    if not origin:
        return True
    # Same-origin browser calls (Hub UI) are always allowed.
    if request is not None:
        host = request.headers.get("host", "").strip()
        if host and origin.rstrip("/") in (
            f"http://{host}",
            f"https://{host}",
            f"http://{host}/".rstrip("/"),
        ):
            return True
        if host and origin.rstrip("/") == f"{request.url.scheme}://{host}":
            return True
    allowed = [
        o.strip()
        for o in os.environ.get("LOCAL_DEEPWIKI_ALLOWED_ORIGINS", "").split(",")
        if o.strip()
    ]
    if not allowed:
        return False
    return origin in allowed


def _accepts_sse(request: Request) -> bool:
    return "text/event-stream" in request.headers.get("accept", "").lower()


def _public_base(request: Request) -> str:
    if _PUBLIC_BASE:
        return _PUBLIC_BASE
    proto = (request.headers.get("x-forwarded-proto") or "").strip() or request.url.scheme
    host = (request.headers.get("x-forwarded-host") or "").strip()
    if not host:
        host = request.headers.get("host", "").strip() or request.url.netloc
    return f"{proto}://{host}"


def _mcp_config_http(name: str, url: str) -> dict[str, Any]:
    return {"mcpServers": {name: {"transport": "streamableHttp", "url": url}}}


def _mcp_config_stdio(name: str, command: str, args: list[str] | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"command": command, "args": args or []}
    return {"mcpServers": {name: entry}}


def _tool_to_mcp(tool: Any) -> dict[str, Any]:
    schema = tool.inputSchema
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(by_alias=True, exclude_none=True)
    return {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": schema or {"type": "object", "properties": {}},
    }


def _text_contents_to_result(contents: list[Any]) -> dict[str, Any]:
    out = []
    for c in contents:
        text = getattr(c, "text", None)
        if text is None and isinstance(c, dict):
            text = c.get("text", str(c))
        out.append({"type": "text", "text": text if text is not None else str(c)})
    return {"content": out}


async def _dispatch_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    known = set(TOOL_HANDLERS) | set(PROGRESS_ENABLED_TOOLS)
    if name not in known:
        return {
            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
            "isError": True,
        }
    contents = await call_tool(name, arguments or {})
    return _text_contents_to_result(list(contents))


async def _handle_one(req: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    method = req.get("method", "")
    req_id = req.get("id", None)

    if method == "initialize":
        return 200, _ok(
            req_id,
            {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "local-deepwiki", "version": _VERSION},
                "capabilities": {"tools": {"listChanged": True}},
            },
        )

    if method == "notifications/initialized":
        return 202, None

    if method == "tools/list":
        tools = [_tool_to_mcp(t) for t in TOOL_DEFINITIONS]
        return 200, _ok(req_id, {"tools": tools})

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            result = await _dispatch_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001 — MCP boundary
            return 200, _ok(
                req_id,
                {
                    "content": [{"type": "text", "text": f"Tool error: {exc}"}],
                    "isError": True,
                },
            )
        return 200, _ok(req_id, result)

    if method == "ping":
        return 200, _ok(req_id, {})

    if _is_notification(req):
        return 202, None
    return 200, _err(req_id, -32601, f"Method not found: {method}")


async def _handle_jsonrpc(payload: Json) -> tuple[int, Json | None]:
    if isinstance(payload, list):
        responses: list[dict[str, Any]] = []
        status = 200
        for item in payload:
            if not isinstance(item, dict):
                continue
            st, resp = await _handle_one(item)
            status = max(status, st)
            if resp is not None:
                responses.append(resp)
        if not responses:
            return 202, None
        return 200, responses

    if not isinstance(payload, dict):
        return 400, _err(None, -32700, "Parse error: expected JSON object or array")

    status, resp = await _handle_one(payload)
    if resp is None:
        return status, None
    return status, resp


@dataclass
class _SseClient:
    queue: asyncio.Queue[str]
    created_ts: float


class _SseHub:
    def __init__(self) -> None:
        self._clients: list[_SseClient] = []
        self._lock = asyncio.Lock()

    async def add(self) -> _SseClient:
        client = _SseClient(queue=asyncio.Queue(), created_ts=time.time())
        async with self._lock:
            self._clients.append(client)
        return client

    async def remove(self, client: _SseClient) -> None:
        async with self._lock:
            self._clients = [c for c in self._clients if c is not client]


def _sse_frame(msg: dict[str, Any]) -> str:
    payload = json.dumps(msg, ensure_ascii=False).replace("\n", "\\n")
    return f"data: {payload}\n\n"


_sse_hub = _SseHub()

app = FastAPI(title="local-deepwiki MCP HTTP", version=_VERSION)


@app.on_event("startup")
async def _startup() -> None:
    from local_deepwiki.server import _validate_tool_handler_consistency, _log_security_posture

    _validate_tool_handler_consistency()
    _log_security_posture()
    get_manager()  # load registry


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> JSONResponse:
    mgr = get_manager()
    repos = mgr.list_repos()
    return JSONResponse(
        {
            "status": "ok",
            "service": "local-deepwiki",
            "version": _VERSION,
            "repos": len(repos),
            "ready_repos": sum(1 for r in repos if r.get("status") == "ready"),
        }
    )


@app.get("/health/live")
async def health_live() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    return JSONResponse({"status": "ok", "ready": True})


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


@app.get("/mcp")
async def mcp_get(request: Request) -> Response:
    origin = request.headers.get("origin")
    if not _origin_allowed(origin, request):
        return PlainTextResponse("Origin not allowed", status_code=403)
    if not _accepts_sse(request):
        return PlainTextResponse("Client must accept text/event-stream", status_code=406)

    client = await _sse_hub.add()

    async def event_stream():
        yield _sse_frame(
            {"jsonrpc": "2.0", "method": "local_deepwiki/ready", "params": {"status": "ok"}}
        )
        keepalive_sec = int(os.environ.get("LOCAL_DEEPWIKI_SSE_KEEPALIVE_SEC", "25"))
        last_keepalive = time.time()
        try:
            while True:
                timeout = max(1, keepalive_sec - int(time.time() - last_keepalive))
                try:
                    frame = await asyncio.wait_for(client.queue.get(), timeout=timeout)
                    yield frame
                except asyncio.TimeoutError:
                    last_keepalive = time.time()
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            await _sse_hub.remove(client)

    return StreamingResponse(
        event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@app.post("/mcp")
async def mcp_post(request: Request) -> Response:
    origin = request.headers.get("origin")
    if not _origin_allowed(origin, request):
        return PlainTextResponse("Origin not allowed", status_code=403)

    try:
        payload: Json = await request.json()
    except Exception:
        return JSONResponse(_err(None, -32700, "Parse error: invalid JSON"), status_code=400)

    timeout_sec = float(os.environ.get("LOCAL_DEEPWIKI_TOOL_TIMEOUT_SEC", "600"))
    async with _TOOL_SEM:
        try:
            status, out = await asyncio.wait_for(_handle_jsonrpc(payload), timeout=timeout_sec)
        except asyncio.TimeoutError:
            return JSONResponse(_err(None, -32001, "Request timed out"), status_code=504)

    if out is None:
        return Response(status_code=status)
    return JSONResponse(out, status_code=status)


# ---------------------------------------------------------------------------
# Drop-in Cursor / VS Code MCP configs
# ---------------------------------------------------------------------------


@app.get("/ui/mcp/cursor.json")
async def ui_mcp_cursor(request: Request) -> JSONResponse:
    base = _public_base(request)
    return JSONResponse(_mcp_config_http("local-deepwiki", f"{base}/mcp"))


@app.get("/ui/mcp/vscode.json")
async def ui_mcp_vscode(request: Request) -> JSONResponse:
    base = _public_base(request)
    return JSONResponse(_mcp_config_http("local-deepwiki", f"{base}/mcp"))


@app.get("/ui/mcp/cursor-stdio.json")
async def ui_mcp_cursor_stdio() -> JSONResponse:
    root = str(_ROOT)
    script = f"{root}/deploy/scripts/run_mcp_stdio.sh"
    return JSONResponse(_mcp_config_stdio("local-deepwiki-stdio", script, []))


@app.get("/ui/mcp/cursor-stdio-ssh.json")
async def ui_mcp_cursor_stdio_ssh() -> JSONResponse:
    host = os.environ.get("LOCAL_DEEPWIKI_SSH_TARGET", "deploy@192.168.1.165")
    script = f"{_ROOT}/deploy/scripts/run_mcp_stdio.sh"
    return JSONResponse(
        _mcp_config_stdio("local-deepwiki-stdio-ssh", "ssh", [host, script])
    )


# ---------------------------------------------------------------------------
# Hub UI + API
# ---------------------------------------------------------------------------


class AddRepoBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    git_url: str = Field(..., min_length=1, max_length=2048)


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/ui", status_code=302)


@app.get("/ui", response_class=HTMLResponse)
@app.get("/ui/", response_class=HTMLResponse)
async def ui_index(request: Request) -> HTMLResponse:
    repos = get_manager().list_repos()
    return _TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "repos": repos,
            "version": _VERSION,
            "public_base": _public_base(request),
        },
    )


@app.get("/ui/api/repos")
async def api_list_repos() -> JSONResponse:
    return JSONResponse({"ok": True, "repos": get_manager().list_repos()})


@app.post("/ui/api/repos")
async def api_add_repo(body: AddRepoBody) -> JSONResponse:
    try:
        rec = get_manager().add_repo(body.name.strip(), body.git_url.strip())
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "repo": rec.to_dict()}, status_code=201)


@app.post("/ui/api/repos/{name}/reindex")
async def api_reindex(name: str, full: bool = False) -> JSONResponse:
    try:
        rec = get_manager().reindex(name, full_rebuild=full)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "repo": rec.to_dict()})


@app.delete("/ui/api/repos/{name}")
async def api_delete_repo(name: str) -> JSONResponse:
    try:
        get_manager().delete_repo(name)
    except KeyError:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


def _repo_has_wiki(name: str) -> bool:
    rec = get_manager().get(name)
    if rec is None:
        return False
    return rec.wiki_path.is_dir() and (rec.wiki_path / "index.md").exists()


def _active_wiki_repo(request: Request) -> str | None:
    """Repo for bare /wiki|… links: cookie, else the only ready wiki."""
    cookie = (request.cookies.get(_ACTIVE_REPO_COOKIE) or "").strip()
    if cookie and _repo_has_wiki(cookie):
        return cookie
    ready = [
        r["name"]
        for r in get_manager().list_repos()
        if r.get("status") == "ready" and r.get("has_wiki")
    ]
    if len(ready) == 1:
        return ready[0]
    return None


def _rewrite_wiki_asset_urls(content: bytes, name: str, content_type: str | None) -> bytes:
    """Prefix absolute deepwiki paths so they stay under /r/{name}/."""
    ct = (content_type or "").lower()
    if "text/html" not in ct and "javascript" not in ct:
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    prefix = f"/r/{name}"
    # Longer prefixes first so "/chat" does not disturb already-rewritten paths.
    for abs_path in ("/wiki/", "/api/", "/search.json", "/architecture", "/codemap", "/chat"):
        for quote in ('"', "'"):
            text = text.replace(f"{quote}{abs_path}", f"{quote}{prefix}{abs_path}")
    # Nav brand / Wiki home: href="/" or action="/"
    text = re.sub(
        r"""\b(href|action)=(["'])/\2""",
        rf"\1=\2{prefix}/\2",
        text,
    )
    return text.encode("utf-8")


def _proxy_out_headers(upstream: httpx.Response, name: str) -> dict[str, str]:
    excluded = {"content-encoding", "transfer-encoding", "content-length", "connection"}
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
    loc = out_headers.get("location") or out_headers.get("Location")
    if loc and loc.startswith("/") and not loc.startswith(f"/r/{name}"):
        out_headers["location"] = f"/r/{name}{loc}"
        out_headers.pop("Location", None)
    out_headers.pop("content-length", None)
    out_headers.pop("Content-Length", None)
    return out_headers


def _set_active_repo_cookie(response: Response, name: str) -> None:
    response.set_cookie(
        _ACTIVE_REPO_COOKIE,
        name,
        max_age=60 * 60 * 24 * 30,
        httponly=False,
        samesite="lax",
        path="/",
    )


async def _proxy_wiki(name: str, request: Request, path: str = "") -> Response:
    """Reverse-proxy to per-repo `deepwiki serve` on loopback.

    HTML is buffered so absolute /wiki|/api|/chat links can be rewritten under
    /r/{name}/. SSE and other responses are streamed (chat/research break if
    buffered until completion).
    """
    mgr = get_manager()
    try:
        port = await asyncio.to_thread(mgr.ensure_wiki_server, name)
    except KeyError:
        return PlainTextResponse("repo not found", status_code=404)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=409)

    for _ in range(40):
        try:
            async with httpx.AsyncClient() as client:
                await client.get(f"http://127.0.0.1:{port}/", timeout=0.5)
            break
        except Exception:
            await asyncio.sleep(0.25)

    target_path = "/" + path if path else "/"
    url = f"http://127.0.0.1:{port}{target_path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }
    # So Flask CSRF can accept the browser Origin (public Hub URL) while the
    # upstream wiki listens on 127.0.0.1.
    if "x-forwarded-host" not in {k.lower() for k in headers}:
        headers["X-Forwarded-Host"] = request.headers.get("host", "")
    if "x-forwarded-proto" not in {k.lower() for k in headers}:
        headers["X-Forwarded-Proto"] = request.url.scheme
    client_host = request.client.host if request.client else ""
    if client_host:
        prior = headers.get("X-Forwarded-For") or headers.get("x-forwarded-for")
        headers["X-Forwarded-For"] = f"{prior}, {client_host}" if prior else client_host
    body = await request.body()
    timeout = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
    client = httpx.AsyncClient(follow_redirects=False, timeout=timeout)
    try:
        upstream = await client.send(
            client.build_request(request.method, url, headers=headers, content=body),
            stream=True,
        )
    except httpx.RequestError as exc:
        await client.aclose()
        return PlainTextResponse(f"wiki upstream error: {exc}", status_code=502)

    content_type = upstream.headers.get("content-type")
    ct = (content_type or "").lower()
    out_headers = _proxy_out_headers(upstream, name)

    # Buffer HTML/JS so we can rewrite absolute asset URLs.
    if "text/html" in ct or "javascript" in ct:
        try:
            raw = await upstream.aread()
        finally:
            await upstream.aclose()
            await client.aclose()
        content = _rewrite_wiki_asset_urls(raw, name, content_type)
        response = Response(
            content=content,
            status_code=upstream.status_code,
            headers=out_headers,
            media_type=content_type,
        )
        _set_active_repo_cookie(response, name)
        return response

    async def _stream_body():
        try:
            async for chunk in upstream.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response = StreamingResponse(
        _stream_body(),
        status_code=upstream.status_code,
        headers=out_headers,
        media_type=content_type,
    )
    _set_active_repo_cookie(response, name)
    return response


@app.api_route("/r/{name}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.api_route("/r/{name}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def wiki_proxy(name: str, request: Request, path: str = "") -> Response:
    return await _proxy_wiki(name, request, path)


async def _wiki_bare_path(request: Request, path: str) -> Response:
    """Map absolute deepwiki URLs (/wiki/…, /architecture, …) onto an active repo."""
    name = _active_wiki_repo(request)
    if not name:
        return RedirectResponse(url="/ui", status_code=302)
    # Prefer canonical /r/{name}/… URLs (rewritten HTML links use this form).
    dest = f"/r/{name}{path}"
    if request.url.query:
        dest = f"{dest}?{request.url.query}"
    if request.method.upper() in ("GET", "HEAD"):
        resp = RedirectResponse(url=dest, status_code=307)
        resp.set_cookie(
            _ACTIVE_REPO_COOKIE,
            name,
            max_age=60 * 60 * 24 * 30,
            httponly=False,
            samesite="lax",
            path="/",
        )
        return resp
    # Non-GET (API posts from unre written clients): proxy in place.
    rel = path.lstrip("/")
    return await _proxy_wiki(name, request, rel)


@app.api_route("/wiki", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
@app.api_route("/wiki/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def wiki_bare_wiki(request: Request, path: str = "") -> Response:
    full = "/wiki" if not path else f"/wiki/{path}"
    return await _wiki_bare_path(request, full)


@app.api_route("/codemap", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
@app.api_route("/codemap/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def wiki_bare_codemap(request: Request, path: str = "") -> Response:
    full = "/codemap" if not path else f"/codemap/{path}"
    return await _wiki_bare_path(request, full)


@app.api_route("/chat", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
@app.api_route("/chat/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def wiki_bare_chat(request: Request, path: str = "") -> Response:
    full = "/chat" if not path else f"/chat/{path}"
    return await _wiki_bare_path(request, full)


@app.api_route("/architecture", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
@app.api_route(
    "/architecture/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
)
async def wiki_bare_architecture(request: Request, path: str = "") -> Response:
    full = "/architecture" if not path else f"/architecture/{path}"
    return await _wiki_bare_path(request, full)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def wiki_bare_api(request: Request, path: str) -> Response:
    return await _wiki_bare_path(request, f"/api/{path}")


@app.api_route("/search.json", methods=["GET", "HEAD"])
async def wiki_bare_search(request: Request) -> Response:
    return await _wiki_bare_path(request, "/search.json")
