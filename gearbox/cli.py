"""gearbox CLI: run the proxy in the background and wire it into Claude Code.

    gearbox install     # once: Claude Code settings + autostart, like `rtk init`
    gearbox status
    gearbox report
    gearbox watch
    gearbox uninstall
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import DEFAULT_CONFIG, HOME_DIR, Config, load_config, request_cost

HOOK_MARKER = "gearbox.cli ensure"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "gearbox"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"


# --- process management -----------------------------------------------------

def base_url(cfg: Config) -> str:
    return f"http://{cfg.host}:{cfg.port}"


def health(cfg: Config, timeout: float = 0.5) -> dict | None:
    from .proxy import HEALTH_PATH
    try:
        with urllib.request.urlopen(base_url(cfg) + HEALTH_PATH, timeout=timeout) as r:
            data = json.loads(r.read())
            return data if data.get("service") == "gearbox" else None
    except (OSError, ValueError):
        return None


def python_exe(windowless: bool) -> str:
    exe = Path(sys.executable)
    if windowless and os.name == "nt":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


def spawn_background(config_arg: str | None) -> None:
    cmd = [python_exe(windowless=True), "-m", "gearbox.cli", "serve"]
    if config_arg:
        cmd += ["--config", config_arg]
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, close_fds=True, cwd=str(HOME_DIR))
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            # Break out of the hook's job object so the proxy outlives the hook process.
            subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            return
        except OSError:
            kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def cmd_serve(args) -> int:
    import uvicorn
    from .proxy import create_app

    HOME_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(HOME_DIR / "gearbox.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr:  # None under pythonw
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("gearbox")

    cfg = load_config(args.config)
    if health(cfg):
        log.info("already running on %s", base_url(cfg))
        return 0
    if cfg.judge.enabled and not (os.environ.get(cfg.judge.api_key_env) or os.environ.get("ANTHROPIC_API_KEY")):
        log.warning("judge disabled (no %s / ANTHROPIC_API_KEY) - heuristics only", cfg.judge.api_key_env)
    log.info("listening on %s -> %s (policy=%s, log=%s)", base_url(cfg), cfg.upstream, cfg.policy, cfg.log_file)
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_config=None, log_level="warning")
    return 0


def cmd_ensure(args) -> int:
    """Start the proxy in the background unless it is already up. Used by the SessionStart hook."""
    cfg = load_config(args.config)
    if health(cfg):
        return 0
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    spawn_background(args.config)
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if health(cfg):
            if not args.quiet:
                print(f"gearbox started on {base_url(cfg)}")
            return 0
        time.sleep(0.2)
    print(f"gearbox failed to start on {base_url(cfg)} - see {HOME_DIR / 'gearbox.log'}. "
          f"Claude Code requests will fail until it runs; `gearbox uninstall` restores direct access.",
          file=sys.stderr)
    return 1


def cmd_stop(args) -> int:
    cfg = load_config(args.config)
    h = health(cfg)
    if not h:
        print("not running")
        return 0
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(h["pid"]), "/F"], capture_output=True)
    else:
        os.kill(h["pid"], 15)
    print(f"stopped (pid {h['pid']})")
    return 0


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    h = health(cfg)
    settings = read_settings(Path(args.settings_file))
    installed = HOOK_MARKER in json.dumps(settings.get("hooks", {}))
    env_url = settings.get("env", {}).get("ANTHROPIC_BASE_URL")
    print(f"proxy:      {'running (pid %s)' % h['pid'] if h else 'NOT running'} at {base_url(cfg)}")
    if h:
        print(f"judge:      {'on' if h['judge'] else 'off (no API key, heuristics only)'}")
    print(f"policy:     {cfg.policy}")
    print(f"claude:     {'hooked' if installed else 'not installed'}; ANTHROPIC_BASE_URL={env_url}")
    print(f"autostart:  {'on' if autostart_get() else 'off'}")
    print(f"config:     {HOME_DIR / 'config.yaml' if (HOME_DIR / 'config.yaml').exists() else DEFAULT_CONFIG}")
    print(f"decisions:  {cfg.log_file}")
    print(f"server log: {HOME_DIR / 'gearbox.log'}")
    return 0


# --- Claude Code integration ------------------------------------------------

def read_settings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def hook_command() -> str:
    exe = python_exe(windowless=False).replace("\\", "/")  # hooks may run under Git Bash on Windows
    return f'"{exe}" -m {HOOK_MARKER} --quiet'


def autostart_get() -> str | None:
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_VALUE)[0]
    except OSError:
        return None


def autostart_set(enable: bool) -> None:
    if os.name != "nt":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ,
                              f'"{python_exe(windowless=True)}" -m gearbox.cli serve')
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except OSError:
                pass


def cmd_install(args) -> int:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    user_cfg = HOME_DIR / "config.yaml"
    if not user_cfg.exists():
        shutil.copy(DEFAULT_CONFIG, user_cfg)
        print(f"config:    created {user_cfg}")
    cfg = load_config(args.config)
    url = base_url(cfg)

    path = Path(args.settings_file)
    settings = read_settings(path)
    existing = settings.get("env", {}).get("ANTHROPIC_BASE_URL")
    if existing and existing != url and not args.force:
        print(f"ANTHROPIC_BASE_URL is already set to {existing} in {path}.\n"
              f"Set `upstream: {existing}` in {user_cfg} to chain the router in front of it, "
              f"then rerun with --force.", file=sys.stderr)
        return 1
    if path.exists():
        backup = path.with_suffix(".json.gearbox.bak")
        if not backup.exists():
            shutil.copy(path, backup)
            print(f"backup:    {backup}")

    settings.setdefault("env", {})["ANTHROPIC_BASE_URL"] = url
    session_start = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    session_start[:] = [e for e in session_start if HOOK_MARKER not in json.dumps(e)]
    session_start.append({"hooks": [{"type": "command", "command": hook_command(), "timeout": 20}]})
    write_settings(path, settings)
    print(f"claude:    {path} -> ANTHROPIC_BASE_URL={url} + SessionStart hook")

    if not args.no_autostart:
        autostart_set(True)
        print("autostart: on (starts at Windows logon)" if os.name == "nt"
              else "autostart: not available on this OS, the SessionStart hook starts the proxy")

    rc = cmd_ensure(argparse.Namespace(config=args.config, wait=10, quiet=False))
    if rc == 0:
        print("done. New Claude Code sessions go through the router; `gearbox status` to check.")
    return rc


def cmd_uninstall(args) -> int:
    path = Path(args.settings_file)
    settings = read_settings(path)
    env = settings.get("env", {})
    if env.get("ANTHROPIC_BASE_URL", "").startswith(("http://127.0.0.1", "http://localhost")):
        env.pop("ANTHROPIC_BASE_URL")
        if not env:
            settings.pop("env")
    hooks = settings.get("hooks", {})
    if "SessionStart" in hooks:
        hooks["SessionStart"] = [e for e in hooks["SessionStart"] if HOOK_MARKER not in json.dumps(e)]
        if not hooks["SessionStart"]:
            hooks.pop("SessionStart")
    if "hooks" in settings and not settings["hooks"]:
        settings.pop("hooks")
    if path.exists():
        write_settings(path, settings)
    autostart_set(False)
    cmd_stop(args)
    print(f"removed from {path} and autostart; config and logs kept in {HOME_DIR}")
    return 0


def cmd_report(args) -> int:
    from . import report
    cfg = load_config(args.config)
    argv = [cfg.log_file, "--show", str(args.show)]
    if args.config:
        argv += ["--config", args.config]
    return report.main(argv)


def cmd_watch(args) -> int:
    """Tail decisions.jsonl and print one line per routed prompt as it happens."""
    cfg = load_config(args.config)
    path = Path(cfg.log_file)
    print(f"watching {path} (Ctrl+C to stop)")
    pos = path.stat().st_size if path.exists() else 0
    try:
        while True:
            if not path.exists():
                time.sleep(0.5)
                continue
            with path.open("r", encoding="utf-8") as f:
                f.seek(pos)
                for line in f:
                    pos += len(line.encode("utf-8"))
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if "classified" not in r:
                        continue
                    cost = ""
                    p = cfg.pricing.get(r.get("tier"))
                    usd = request_cost(r, p) if p else None
                    if usd is not None:
                        cost = f"  ${usd:.4f}"
                    flag = "  [FALLBACK]" if r.get("fallback") else ""
                    prompt = r.get("prompt", "").replace("\n", " ")[:70]
                    print(f"{r.get('ts', ''):19}  {r.get('tier', '?'):7} <- {r.get('source', '?'):9}"
                          f"  {r.get('served_model', '?'):28}{cost}{flag}  {prompt!r}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gearbox", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config file (default: ~/.gearbox/config.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the proxy in the foreground").set_defaults(fn=cmd_serve)
    p = sub.add_parser("ensure", help="start in the background if not running")
    p.add_argument("--wait", type=float, default=10)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_ensure)
    sub.add_parser("stop", help="stop the background proxy").set_defaults(fn=cmd_stop)
    for name, fn, help_ in [("install", cmd_install, "hook into Claude Code and enable autostart"),
                            ("uninstall", cmd_uninstall, "undo install"),
                            ("status", cmd_status, "show proxy / install state")]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--settings-file", default=str(CLAUDE_SETTINGS),
                       help="Claude Code settings.json to modify (default: ~/.claude/settings.json)")
        if name == "install":
            p.add_argument("--no-autostart", action="store_true")
            p.add_argument("--force", action="store_true")
        p.set_defaults(fn=fn)
    p = sub.add_parser("report", help="summarize routing decisions")
    p.add_argument("--show", type=int, default=15)
    p.set_defaults(fn=cmd_report)
    sub.add_parser("watch", help="live-tail routing decisions as they happen").set_defaults(fn=cmd_watch)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
