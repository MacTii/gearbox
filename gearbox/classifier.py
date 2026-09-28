from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from .config import Config
from .heuristics import score_prompt, tier_from_score
from .judge import Judge

OVERRIDE_RE = re.compile(r"^\s*!(haiku|sonnet|opus)\b", re.IGNORECASE)


@dataclass
class Classification:
    tier: str
    source: str  # override | heuristic | judge | fallback
    score: float = 0.0
    signals: list[str] = field(default_factory=list)


class Classifier:
    """Heuristics settle the obvious cases, the Haiku judge decides the rest."""

    def __init__(self, cfg: Config, judge: Judge | None = None):
        self.cfg = cfg
        self.judge = judge if judge is not None else Judge(cfg)

    async def classify(self, text: str) -> Classification:
        m = OVERRIDE_RE.match(text)
        if m:
            return Classification(m.group(1).lower(), "override")

        h = score_prompt(text, self.cfg.heuristics)
        if h.tier:
            return Classification(h.tier, "heuristic", h.score, h.signals)

        judged = await self.judge.classify(text)
        if judged:
            return Classification(judged, "judge", h.score, h.signals)

        tier = tier_from_score(h.score) if h.signals else self.cfg.default_tier
        return Classification(tier, "fallback", h.score, h.signals)


_default: Classifier | None = None


async def choose_model_async(prompt: str, cfg: Config | None = None,
                             top_model: str = "claude-opus-5") -> str:
    """For your own app: pick a model ID for a prompt before calling messages.create.
    `top_model` is used for tiers configured as "passthrough"."""
    global _default
    if cfg is not None:
        clf = Classifier(cfg)
    else:
        if _default is None:
            from .config import load_config
            _default = Classifier(load_config())
        clf = _default
    tier = (await clf.classify(prompt)).tier
    return clf.cfg.model_for(tier, top_model)


def choose_model(prompt: str, cfg: Config | None = None, top_model: str = "claude-opus-5") -> str:
    return asyncio.run(choose_model_async(prompt, cfg, top_model))
