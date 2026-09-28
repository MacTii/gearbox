"""Local proxy for Claude Code: set ANTHROPIC_BASE_URL=http://127.0.0.1:8080."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .classifier import Classifier
from .config import TIER_ORDER, Config, load_config
from .decision_log import DecisionLog
from .net import ssl_context
from .request import (demote_system_messages, dumps, estimate_tokens, is_fresh_conversation,
                      new_user_prompt, session_key, strip_fields)
from .session import SessionStore, apply_policy

log = logging.getLogger("gearbox")

HOP_BY_HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding", "keep-alive"}
RESP_DROP = {"content-length", "content-encoding", "transfer-encoding", "connection"}
HEALTH_PATH = "/_gearbox/health"
SERVED_MODEL_RE = re.compile(rb'"model"\s*:\s*"([^"]+)"')
INPUT_TOKENS_RE = re.compile(rb'"input_tokens"\s*:\s*(\d+)')
OUTPUT_TOKENS_RE = re.compile(rb'"output_tokens"\s*:\s*(\d+)')
CACHE_WRITE_RE = re.compile(rb'"cache_creation_input_tokens"\s*:\s*(\d+)')
CACHE_READ_RE = re.compile(rb'"cache_read_input_tokens"\s*:\s*(\d+)')
CONNECT_RETRIES = 3


@dataclass
class Route:
    body: bytes
    model: str
    changed: bool
    record: dict | None  # decision to log (only for new user prompts)
    session: str | None = None
    requested: str = ""


class Router:
    def __init__(self, cfg: Config, classifier: Classifier | None = None):
        self.cfg = cfg
        self.classifier = classifier or Classifier(cfg)
        self.sessions = SessionStore()

    def _intercepted(self, model: str) -> bool:
        return any(s in model for s in self.cfg.intercept_models)

    async def route(self, raw: bytes, header_session: str | None = None) -> Route:
        try:
            body = json.loads(raw)
        except ValueError:
            log.warning("request body is not JSON, passing through")
            return Route(raw, "", False, None)
        requested = body.get("model", "")
        if not self._intercepted(requested):
            return Route(raw, requested, False, None)

        key = session_key(body, header_session)
        if is_fresh_conversation(body):
            self.sessions.reset(key)
        current = self.sessions.get(key)
        prompt = new_user_prompt(body)

        record = None
        if prompt is None:
            # Tool-result continuation: stay on the session's model; unknown session -> leave as is.
            tier = current
        else:
            c = await self.classifier.classify(prompt)
            tier = apply_policy(self.cfg.policy, current, c.tier, forced=c.source == "override")
            record = {"session": key[:12], "prompt": prompt[:300], "chars": len(prompt),
                      "classified": c.tier, "source": c.source, "score": c.score,
                      "signals": c.signals, "previous": current}

        if tier is None:
            return Route(raw, requested, False, record)

        tokens = estimate_tokens(raw)
        reasons = []
        if tier == "haiku" and tokens > self.cfg.haiku_max_context_tokens:
            tier = "sonnet"
            reasons.append(f"context~{tokens}")
        self.sessions.set(key, tier)

        model = self.cfg.model_for(tier, requested)
        if record is not None:
            record.update(tier=tier, model=model, requested=requested, est_tokens=tokens,
                          constraints=reasons or None)
        if model == requested:
            return Route(raw, model, False, record)

        body["model"] = model
        body = strip_fields(body, self.cfg.strip_fields.get(tier, []))
        if tier in self.cfg.demote_system_messages:
            body = demote_system_messages(body)
        cap = self.cfg.max_output_tokens.get(tier)
        if cap and body.get("max_tokens", 0) > cap:
            body["max_tokens"] = cap
        return Route(dumps(body), model, True, record, key, requested)

    def pin_to_requested(self, route: Route) -> None:
        """After the rerouted model was rejected, keep this session on the client's model."""
        tier = next((t for t in reversed(TIER_ORDER)
                     if self.cfg.model_for(t, route.requested) == route.requested), "opus")
        self.sessions.set(route.session, tier)


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    router = Router(cfg)
    decisions = DecisionLog(cfg.log_file)
    client = httpx.AsyncClient(base_url=cfg.upstream, timeout=httpx.Timeout(600.0, connect=10.0),
                               verify=ssl_context())

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await client.aclose()

    app = FastAPI(lifespan=lifespan)

    async def send(method: str, path: str, params, headers: dict, content: bytes) -> httpx.Response:
        # A failed connect never reached the API, so retrying it costs no tokens.
        for attempt in range(CONNECT_RETRIES):
            req = client.build_request(method, path, params=params, headers=headers, content=content)
            try:
                return await client.send(req, stream=True)
            except httpx.ConnectError as e:
                if attempt == CONNECT_RETRIES - 1:
                    raise
                log.warning("connect to upstream failed (%s), retrying", e)
                await asyncio.sleep(0.2 * (attempt + 1))

    @app.get(HEALTH_PATH)
    async def health():
        return {"ok": True, "service": "gearbox", "pid": os.getpid(), "policy": cfg.policy,
                "judge": router.classifier.judge.enabled, "log_file": cfg.log_file}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy(path: str, request: Request):
        raw = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        # Otherwise httpx asks for gzip and we would relay compressed bytes without the header.
        headers["accept-encoding"] = "identity"
        started = time.perf_counter()

        route = Route(raw, "", False, None)
        if request.method == "POST" and path.rstrip("/") == "v1/messages":
            route = await router.route(raw, request.headers.get("x-claude-code-session-id"))
        route_ms = round((time.perf_counter() - started) * 1000)

        try:
            upstream = await send(request.method, "/" + path, request.query_params, headers, route.body)
        except httpx.ConnectError as e:
            log.error("upstream unreachable: %s", e)
            # Anthropic error shape, so Claude Code shows a readable message and retries on its own.
            return JSONResponse({"type": "error", "error": {"type": "api_error",
                                 "message": f"gearbox: cannot reach {cfg.upstream}: {e}"}}, status_code=502)

        fallback = None
        if route.changed and upstream.status_code == 400 and cfg.fallback_on_400:
            err = (await upstream.aread()).decode("utf-8", "replace")[:500]
            await upstream.aclose()
            fallback = {"from": route.model, "error": err}
            router.pin_to_requested(route)
            log.warning("rerouted request rejected (%s), retrying with original model", err)
            upstream = await send(request.method, "/" + path, request.query_params, headers, raw)

        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in RESP_DROP}
        if route.record is None and not fallback:
            return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                     headers=resp_headers, background=BackgroundTask(upstream.aclose))

        async def relay_and_log():
            # Log once the stream is done: model actually served, and real usage for cost reporting
            # (input_tokens sits in the early message_start event, output_tokens accumulates in
            # message_delta events through the stream - the last match is the final count).
            head, served, out_tok = b"", None, None
            usage = {"input_tokens": None, "cache_write_tokens": None, "cache_read_tokens": None}
            patterns = {"input_tokens": INPUT_TOKENS_RE, "cache_write_tokens": CACHE_WRITE_RE,
                        "cache_read_tokens": CACHE_READ_RE}
            try:
                async for chunk in upstream.aiter_raw():
                    if len(head) < 8192:
                        head += chunk
                        if served is None:
                            m = SERVED_MODEL_RE.search(head)
                            if m:
                                served = m.group(1).decode()
                        for key, rx in patterns.items():
                            if usage[key] is None:
                                m = rx.search(head)
                                if m:
                                    usage[key] = int(m.group(1))
                    m = OUTPUT_TOKENS_RE.findall(chunk)
                    if m:
                        out_tok = int(m[-1])
                    yield chunk
            finally:
                decisions.write(**(route.record or {}), served_model=served, route_ms=route_ms,
                                status=upstream.status_code, fallback=fallback,
                                output_tokens=out_tok, **usage)
                if route.record:
                    log.info("%s -> %s (%s), served by %s", route.record.get("classified"),
                             route.model, route.record.get("source"), served)

        return StreamingResponse(relay_and_log(), status_code=upstream.status_code,
                                 headers=resp_headers, background=BackgroundTask(upstream.aclose))

    return app
