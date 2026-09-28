from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from .config import HeuristicsConfig

FILE_RE = re.compile(r"[\w./\\-]+\.(?:py|ts|tsx|js|jsx|cs|java|go|rs|rb|php|cpp|c|h|sql|yaml|yml|json|md)\b")
NUMBERED_LINE_RE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+\S", re.MULTILINE)


@dataclass
class HeuristicResult:
    score: float
    tier: str | None  # None = not confident, ask the judge
    signals: list[str] = field(default_factory=list)


def fold(text: str) -> str:
    """Drop diacritics so "literowke" matches the stem "literówk" (ł has no decomposition)."""
    text = unicodedata.normalize("NFKD", text.replace("ł", "l").replace("Ł", "L"))
    return "".join(c for c in text if not unicodedata.combining(c))


@lru_cache(maxsize=512)
def _stem_re(stem: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(fold(stem)), re.IGNORECASE)


def score_prompt(text: str, cfg: HeuristicsConfig) -> HeuristicResult:
    score = 0.0
    signals: list[str] = []
    text = fold(text)

    for stem, w in cfg.opus_keywords.items():
        if _stem_re(stem).search(text):
            score += w
            signals.append(f"+{w} '{stem}'")
    for stem, w in cfg.haiku_keywords.items():
        if _stem_re(stem).search(text):
            score -= w
            signals.append(f"-{w} '{stem}'")

    words = len(text.split())
    if words > 300:
        score += 1.5
        signals.append(f"+1.5 long ({words} words)")
    elif words > 120:
        score += 0.5
        signals.append(f"+0.5 medium ({words} words)")
    elif words < 12:
        score -= 1.0
        signals.append(f"-1 short ({words} words)")

    files = set(FILE_RE.findall(text))
    if len(files) >= 3:
        score += 1.0
        signals.append(f"+1 files={len(files)}")

    if text.count("```") >= 2:
        score += 0.5
        signals.append("+0.5 code block")

    if len(NUMBERED_LINE_RE.findall(text)) >= 3:
        score += 1.0
        signals.append("+1 multi-step list")

    if score >= cfg.opus_threshold:
        tier = "opus"
    elif score <= cfg.haiku_threshold:
        tier = "haiku"
    else:
        tier = None
    return HeuristicResult(round(score, 2), tier, signals)


def tier_from_score(score: float) -> str:
    """Fallback mapping when the judge is unavailable. Deliberately leans to Sonnet:
    one weak keyword (e.g. "pokaż" in "add a method and show me the code") must not mean Haiku."""
    if score >= 2.0:
        return "opus"
    if score <= -1.5:
        return "haiku"
    return "sonnet"
