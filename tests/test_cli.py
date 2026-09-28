import json

import pytest

from gearbox import cli


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(cli, "autostart_set", lambda enable: None)
    monkeypatch.setattr(cli, "cmd_ensure", lambda args: 0)
    monkeypatch.setattr(cli, "cmd_stop", lambda args: 0)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "model": "opus",
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}]},
    }))
    return settings


def run(*argv):
    return cli.main(list(argv))


def test_install_adds_env_and_hook_and_keeps_existing(env):
    assert run("install", "--settings-file", str(env), "--no-autostart") == 0
    s = json.loads(env.read_text())
    assert s["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8080"
    assert s["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "rtk hook claude"
    assert cli.HOOK_MARKER in s["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert s["model"] == "opus"
    assert env.with_suffix(".json.gearbox.bak").exists()


def test_install_is_idempotent(env):
    run("install", "--settings-file", str(env), "--no-autostart")
    run("install", "--settings-file", str(env), "--no-autostart")
    assert len(json.loads(env.read_text())["hooks"]["SessionStart"]) == 1


def test_install_refuses_foreign_base_url(env):
    s = json.loads(env.read_text())
    s["env"] = {"ANTHROPIC_BASE_URL": "https://gateway.example.com"}
    env.write_text(json.dumps(s))
    assert run("install", "--settings-file", str(env), "--no-autostart") == 1


def test_uninstall_restores_original(env):
    original = json.loads(env.read_text())
    run("install", "--settings-file", str(env), "--no-autostart")
    run("uninstall", "--settings-file", str(env))
    assert json.loads(env.read_text()) == original
