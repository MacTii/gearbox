from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

TIER_ORDER = ["haiku", "sonnet", "opus"]
HOME_DIR = Path(os.environ.get("GEARBOX_HOME", Path.home() / ".gearbox"))
DEFAULT_CONFIG = Path(__file__).with_name("default_config.yaml")


@dataclass
class HeuristicsConfig:
    opus_threshold: float = 3.0
    haiku_threshold: float = -2.0
    opus_keywords: dict[str, float] = field(default_factory=dict)
    haiku_keywords: dict[str, float] = field(default_factory=dict)


@dataclass
class TierPrice:
    input: float = 0.0
    output: float = 0.0


def default_pricing() -> dict[str, TierPrice]:
    """USD per million tokens; used when the config has no pricing section (older user configs)."""
    return {"haiku": TierPrice(1.0, 5.0), "sonnet": TierPrice(3.0, 15.0), "opus": TierPrice(15.0, 75.0)}


def request_cost(record: dict, price: TierPrice) -> float | None:
    """USD for one logged request at the given price; None without usage data.
    Prompt-cache writes bill at 1.25x input, reads at 0.1x input."""
    if record.get("input_tokens") is None or record.get("output_tokens") is None:
        return None
    inp = (record["input_tokens"] + 1.25 * (record.get("cache_write_tokens") or 0)
           + 0.1 * (record.get("cache_read_tokens") or 0))
    return (inp * price.input + record["output_tokens"] * price.output) / 1_000_000


@dataclass
class JudgeConfig:
    enabled: bool = True
    model: str = "claude-haiku-4-5"
    timeout_seconds: float = 3.0
    max_prompt_chars: int = 4000
    api_key_env: str = "GEARBOX_JUDGE_API_KEY"


@dataclass
class Config:
    upstream: str = "https://api.anthropic.com"
    host: str = "127.0.0.1"
    port: int = 8080
    tiers: dict[str, str] = field(default_factory=lambda: {
        "haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-5", "opus": "passthrough"})
    intercept_models: list[str] = field(default_factory=lambda: ["opus", "sonnet"])
    policy: str = "upgrade_only"
    default_tier: str = "sonnet"
    haiku_max_context_tokens: int = 150_000
    fallback_on_400: bool = True
    strip_fields: dict[str, list[str]] = field(default_factory=dict)
    demote_system_messages: list[str] = field(default_factory=lambda: ["haiku", "sonnet"])
    max_output_tokens: dict[str, int] = field(default_factory=lambda: {"haiku": 64000})
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    heuristics: HeuristicsConfig = field(default_factory=HeuristicsConfig)
    pricing: dict[str, TierPrice] = field(default_factory=default_pricing)
    log_file: str = "decisions.jsonl"

    def model_for(self, tier: str, requested_model: str) -> str:
        model = self.tiers[tier]
        return requested_model if model == "passthrough" else model


def resolve_config_path(path: str | Path | None = None) -> Path:
    """Explicit path > $GEARBOX_CONFIG > ~/.gearbox/config.yaml > bundled default."""
    if path:
        return Path(path)
    if os.environ.get("GEARBOX_CONFIG"):
        return Path(os.environ["GEARBOX_CONFIG"])
    user = HOME_DIR / "config.yaml"
    return user if user.exists() else DEFAULT_CONFIG


def load_config(path: str | Path | None = None) -> Config:
    path = resolve_config_path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    listen = raw.pop("listen", {}) or {}
    judge = JudgeConfig(**(raw.pop("judge", {}) or {}))
    heur = HeuristicsConfig(**(raw.pop("heuristics", {}) or {}))
    pricing = {tier: TierPrice(**p) for tier, p in (raw.pop("pricing", {}) or {}).items()} or default_pricing()
    cfg = Config(**raw, judge=judge, heuristics=heur, pricing=pricing)
    cfg.host = listen.get("host", cfg.host)
    cfg.port = listen.get("port", cfg.port)
    if cfg.policy not in ("per_prompt", "upgrade_only", "sticky"):
        raise ValueError(f"unknown policy: {cfg.policy}")
    if cfg.default_tier not in TIER_ORDER:
        raise ValueError(f"unknown default_tier: {cfg.default_tier}")
    # Decisions from the bundled default config go to the user dir, not into the package.
    log_base = HOME_DIR if path.resolve() == DEFAULT_CONFIG.resolve() else path.parent
    cfg.log_file = str((log_base / cfg.log_file).resolve())
    return cfg
