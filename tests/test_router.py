import json

import pytest

from gearbox.classifier import Classifier
from gearbox.config import DEFAULT_CONFIG, load_config
from gearbox.heuristics import score_prompt
from gearbox.proxy import Router
from gearbox.request import new_user_prompt, session_key, strip_fields
from gearbox.session import apply_policy


class FakeJudge:
    def __init__(self, tier=None):
        self.tier = tier
        self.calls = []

    async def classify(self, text):
        self.calls.append(text)
        return self.tier


@pytest.fixture
def cfg():
    return load_config(DEFAULT_CONFIG)


def body(*messages, model="claude-opus-5-5", session="abc-123", **extra):
    return {"model": model, "max_tokens": 32000, "metadata": {"user_id": f"user_x_session_{session}"},
            "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"},
            "messages": list(messages), **extra}


def user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def tool_result():
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}


def assistant(text="ok"):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


# --- heuristics -------------------------------------------------------------

@pytest.mark.parametrize("prompt,tier", [
    ("popraw literówkę w README", "haiku"),
    ("fix the typo in main.py", "haiku"),
    ("wylistuj pliki w src", "haiku"),
    ("Zaprojektuj architekturę modułu płatności z kolejką zdarzeń i opisz kompromisy", "opus"),
    ("Refactor the auth layer and migrate sessions to Redis, watch for race conditions", "opus"),
])
def test_heuristics_obvious_cases(cfg, prompt, tier):
    assert score_prompt(prompt, cfg.heuristics).tier == tier


@pytest.mark.parametrize("prompt,tier", [
    ("popraw literowke w README", "haiku"),
    ("zaprojektuj architekture modulu platnosci i opisz kompromisy", "opus"),
])
def test_heuristics_ignore_missing_diacritics(cfg, prompt, tier):
    assert score_prompt(prompt, cfg.heuristics).tier == tier


def test_heuristics_unsure_for_normal_task(cfg):
    r = score_prompt("Dodaj endpoint GET /users/{id} który zwraca użytkownika z bazy, z walidacją id", cfg.heuristics)
    assert r.tier is None


def test_stem_matches_polish_inflection(cfg):
    assert any("architektur" in s for s in score_prompt("o architekturze systemu", cfg.heuristics).signals)


# --- classifier -------------------------------------------------------------

async def test_judge_called_only_when_unsure(cfg):
    judge = FakeJudge("sonnet")
    clf = Classifier(cfg, judge)
    assert (await clf.classify("popraw literówkę")).source == "heuristic"
    c = await clf.classify("Dodaj endpoint GET /users/{id} który zwraca użytkownika z bazy, z walidacją id")
    assert (c.tier, c.source) == ("sonnet", "judge")
    assert len(judge.calls) == 1


async def test_override(cfg):
    c = await Classifier(cfg, FakeJudge()).classify("!haiku zaprojektuj architekturę")
    assert (c.tier, c.source) == ("haiku", "override")


async def test_fallback_without_judge(cfg):
    c = await Classifier(cfg, FakeJudge(None)).classify("Dodaj endpoint GET /users/{id} z walidacją id")
    assert c.source == "fallback"


async def test_fallback_single_weak_keyword_is_not_haiku(cfg):
    # Real-API regression: "pokaż" alone sent a normal coding task to Haiku.
    c = await Classifier(cfg, FakeJudge(None)).classify(
        "Dodaj do router/session.py metodę count() zwracającą liczbę aktywnych sesji. "
        "Nie edytuj pliku, tylko pokaż kod w 5 linijkach.")
    assert (c.tier, c.source) == ("sonnet", "fallback")


# --- request parsing --------------------------------------------------------

def test_tool_result_is_not_a_new_prompt():
    assert new_user_prompt(body(user("hi"), assistant(), tool_result())) is None


def test_system_reminders_are_ignored():
    b = body({"role": "user", "content": "<system-reminder>lots of context</system-reminder>\npopraw typo"})
    assert new_user_prompt(b) == "popraw typo"


def test_session_key_from_claude_code_metadata():
    assert session_key(body(user("x"), session="deadbeef-1")) == "deadbeef-1"


def test_strip_fields_nested():
    b = strip_fields(body(user("x")), ["thinking", "output_config.effort"])
    assert "thinking" not in b and "output_config" not in b


# --- policy -----------------------------------------------------------------

def test_policies():
    assert apply_policy("upgrade_only", "opus", "haiku", False) == "opus"
    assert apply_policy("upgrade_only", "haiku", "opus", False) == "opus"
    assert apply_policy("sticky", "sonnet", "opus", False) == "sonnet"
    assert apply_policy("per_prompt", "opus", "haiku", False) == "haiku"
    assert apply_policy("upgrade_only", "opus", "haiku", True) == "haiku"


# --- router -----------------------------------------------------------------

async def route(router, b):
    r = await router.route(json.dumps(b).encode())
    return r, json.loads(r.body)


async def test_router_downgrades_and_strips_for_haiku(cfg):
    r, sent = await route(Router(cfg, Classifier(cfg, FakeJudge())), body(user("popraw literówkę w README")))
    assert r.changed and sent["model"] == "claude-haiku-4-5"
    assert "thinking" not in sent and "output_config" not in sent
    assert r.record["source"] == "heuristic"


async def test_router_passthrough_for_opus(cfg):
    r, sent = await route(Router(cfg, Classifier(cfg, FakeJudge())),
                          body(user("Zaprojektuj architekturę systemu kolejek i opisz kompromisy")))
    assert not r.changed and sent["model"] == "claude-opus-5-5"


async def test_continuation_keeps_session_model_and_is_not_logged(cfg):
    router = Router(cfg, Classifier(cfg, FakeJudge()))
    await route(router, body(user("popraw literówkę w README")))
    r, sent = await route(router, body(user("popraw literówkę w README"), assistant(), tool_result()))
    assert sent["model"] == "claude-haiku-4-5" and r.record is None


async def test_upgrade_only_within_session(cfg):
    router = Router(cfg, Classifier(cfg, FakeJudge()))
    history = [user("Zaprojektuj architekturę modułu płatności i opisz kompromisy"), assistant()]
    await route(router, body(history[0]))
    r, sent = await route(router, body(*history, user("popraw literówkę")))
    assert sent["model"] == "claude-opus-5-5" and r.record["classified"] == "haiku"


async def test_background_haiku_requests_untouched(cfg):
    router = Router(cfg, Classifier(cfg, FakeJudge()))
    raw = json.dumps(body(user("Zaprojektuj architekturę"), model="claude-haiku-4-5")).encode()
    r = await router.route(raw)
    assert r.body == raw and r.record is None


async def test_pin_after_fallback(cfg):
    router = Router(cfg, Classifier(cfg, FakeJudge()))
    r, _ = await route(router, body(user("popraw literówkę w README")))
    router.pin_to_requested(r)
    _, sent = await route(router, body(user("popraw literówkę w README"), assistant(), tool_result()))
    assert sent["model"] == "claude-opus-5-5"


def sysmsg(text):
    return {"role": "system", "content": [{"type": "text", "text": text}]}


def test_prompt_found_before_trailing_system_message():
    assert new_user_prompt(body(user("popraw typo"), sysmsg("# Environment ..."))) == "popraw typo"


def test_session_key_from_json_user_id():
    b = body(user("x"))
    b["metadata"]["user_id"] = json.dumps({"device_id": "d", "session_id": "sess-42"})
    assert session_key(b) == "sess-42"
    assert session_key(b, "hdr-1") == "hdr-1"


async def test_real_claude_code_shape_is_downgraded_and_system_demoted(cfg):
    router = Router(cfg, Classifier(cfg, FakeJudge()))
    r, sent = await route(router, body(user("popraw literówkę w README"), sysmsg("# Environment\nwin32")))
    assert sent["model"] == "claude-haiku-4-5"
    assert sent["max_tokens"] == 32000  # under the cap, untouched
    assert [m["role"] for m in sent["messages"]] == ["user"]
    assert "<system-reminder>\n# Environment\nwin32" in sent["messages"][0]["content"][-1]["text"]


async def test_max_tokens_clamped_for_haiku(cfg):
    router = Router(cfg, Classifier(cfg, FakeJudge()))
    _, sent = await route(router, {**body(user("popraw literówkę w README")), "max_tokens": 128000})
    assert sent["max_tokens"] == 64000


async def test_big_context_never_goes_to_haiku(cfg):
    router = Router(cfg, Classifier(cfg, FakeJudge()))
    huge = [user("x" * 700_000), assistant()]
    r, sent = await route(router, body(*huge, user("popraw literówkę")))
    assert sent["model"] == "claude-sonnet-5"
    assert r.record["constraints"]
