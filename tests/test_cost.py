import json

from gearbox import report
from gearbox.config import TierPrice, default_pricing, load_config, request_cost


def test_request_cost_includes_prompt_cache():
    r = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
         "cache_write_tokens": 1_000_000, "cache_read_tokens": 1_000_000}
    # input 1.0 + write 1.25 + read 0.1 = 2.35 x $3, plus $15 output
    assert abs(request_cost(r, TierPrice(3.0, 15.0)) - (2.35 * 3 + 15)) < 1e-9


def test_request_cost_none_without_usage():
    assert request_cost({"output_tokens": 5}, TierPrice(3.0, 15.0)) is None


def test_config_without_pricing_uses_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("policy: upgrade_only\n", encoding="utf-8")
    assert load_config(p).pricing == default_pricing()


def test_report_prints_savings(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("policy: upgrade_only\n", encoding="utf-8")
    log = tmp_path / "decisions.jsonl"
    rows = [{"classified": "haiku", "tier": "haiku", "input_tokens": 1_000_000, "output_tokens": 0},
            {"classified": "opus", "tier": "opus", "input_tokens": 1_000_000, "output_tokens": 0}]
    log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert report.main([str(log), "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    # actual $1 + $15 vs baseline $30 on opus
    assert "actual:   $16.0000" in out
    assert "baseline: $30.0000" in out
    assert "saved:    $14.0000  (47%)" in out
