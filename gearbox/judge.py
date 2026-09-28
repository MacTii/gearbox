from __future__ import annotations

import hashlib
import logging
import os
from collections import OrderedDict

import anthropic
import httpx

from .config import Config
from .net import ssl_context

log = logging.getLogger("gearbox.judge")

JUDGE_SYSTEM = """You route coding-assistant requests to a model tier. Rate the complexity of the user's request:
1 = trivial or mechanical: typo fix, rename, formatting, listing files, a short factual question, running a command
2 = normal development: implementing a well-specified feature, fixing a clear bug, writing tests, explaining code
3 = hard: architecture or system design, large refactors or migrations, subtle debugging (concurrency, performance, security), open-ended planning with trade-offs
Reply with a single digit: 1, 2 or 3."""

SCORE_TO_TIER = {"1": "haiku", "2": "sonnet", "3": "opus"}


class Judge:
    def __init__(self, cfg: Config):
        self.cfg = cfg.judge
        key = os.environ.get(self.cfg.api_key_env) or os.environ.get("ANTHROPIC_API_KEY")
        self.enabled = self.cfg.enabled and bool(key)
        # base_url is explicit: if ANTHROPIC_BASE_URL points at this proxy, the judge must not call itself.
        self.client = anthropic.AsyncAnthropic(
            api_key=key, base_url=cfg.upstream, timeout=self.cfg.timeout_seconds, max_retries=0,
            http_client=httpx.AsyncClient(verify=ssl_context()),
        ) if self.enabled else None
        self._cache: OrderedDict[str, str] = OrderedDict()

    def _clip(self, text: str) -> str:
        # Classification only needs the gist: keep head and tail of very long prompts.
        n = self.cfg.max_prompt_chars
        if len(text) <= n:
            return text
        return text[: n * 3 // 4] + "\n[...]\n" + text[-n // 4:]

    async def classify(self, text: str) -> str | None:
        """Returns a tier name, or None if the judge is disabled or failed."""
        if not self.enabled:
            return None
        key = hashlib.sha256(text.encode()).hexdigest()
        if key in self._cache:
            return self._cache[key]
        try:
            resp = await self.client.messages.create(
                model=self.cfg.model,
                max_tokens=5,
                temperature=0,
                system=JUDGE_SYSTEM,
                messages=[{"role": "user", "content": f"<request>\n{self._clip(text)}\n</request>"}],
            )
        except anthropic.APIError as e:
            log.warning("judge failed: %s", e)
            return None
        answer = "".join(b.text for b in resp.content if b.type == "text").strip()
        tier = SCORE_TO_TIER.get(answer[:1])
        if tier is None:
            log.warning("judge gave unparseable answer: %r", answer)
            return None
        self._cache[key] = tier
        if len(self._cache) > 1000:
            self._cache.popitem(last=False)
        return tier
