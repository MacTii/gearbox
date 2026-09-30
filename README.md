# gearbox

Automatic gear shifting for Claude Code: trivial prompts go to **Haiku**, everyday work to **Sonnet**,
hard problems to **Opus**, per request, without you touching `/model`.

```
you ──► Claude Code ──► gearbox (localhost:8080) ──► api.anthropic.com
                          │
                          ├─ heuristics: keywords, length, files, code blocks
                          ├─ Haiku judge for the grey zone (optional)
                          └─ rewrites `model` and passes the stream through untouched
```

## Why a proxy and not a hook?

A Claude Code hook can read the prompt but cannot change the model of the current turn.
So gearbox sits on the wire as a local proxy (`ANTHROPIC_BASE_URL`) and swaps the `model` field
of each request. A `SessionStart` hook only makes sure the proxy is running.

## Install

```powershell
irm https://raw.githubusercontent.com/MacTii/gearbox/main/install.ps1 | iex
```

That's it. `install.ps1` creates a venv, installs gearbox into it, and runs `gearbox install` (below).

`gearbox install`:
1. Adds `env.ANTHROPIC_BASE_URL=http://127.0.0.1:8080` and a `SessionStart` hook to
   `~/.claude/settings.json`. Any existing entries (other hooks, env vars) are kept, and the
   original file is backed up to `settings.json.gearbox.bak`.
2. Enables autostart at Windows logon (`HKCU\...\Run`, no admin needed).
3. Creates `~/.gearbox/config.yaml` and starts the proxy.

After that, every new Claude Code session is routed. The hook points at this checkout's `.venv`,
so run `gearbox uninstall` before moving or deleting the folder.

### Optional: Haiku judge

Without an API key gearbox uses heuristics only, and uncertain prompts go to Sonnet.
With a key, a Haiku call (a few tokens, sub-second) decides the grey zone:

```powershell
[Environment]::SetEnvironmentVariable("GEARBOX_JUDGE_API_KEY", "sk-ant-...", "User")
gearbox stop   # the next Claude Code session restarts it with the key
```

## Usage

```powershell
gearbox status      # running? hooked? where are the logs?
gearbox report      # tier distribution, models actually served, fallbacks, estimated $ saved, prompts to review
gearbox watch       # live-tail routing decisions (tier, source, served model, cost) as you work
gearbox stop        # stop the proxy (the next Claude Code session starts it again)
gearbox uninstall   # restore direct API access
gearbox serve       # run in the foreground, for debugging
```

To force a gear for one prompt, start it with `!haiku`, `!sonnet` or `!opus`.

Files in `~/.gearbox/`:
- `config.yaml`: rules and thresholds,
- `decisions.jsonl`: every routing decision, including the model the API actually served,
- `gearbox.log`: server log.

## How a gear is chosen

1. Only `POST /v1/messages` requests whose model matches `intercept_models` are routed.
   Claude Code's own background calls, which already use Haiku, pass through untouched.
2. Only **new user prompts** are classified. Tool-result continuations inside an agent loop stay
   on the session's current model. Injected `<system-reminder>` blocks and trailing
   `role: "system"` messages are ignored.
3. The heuristics score the prompt: PL/EN keyword stems with weights (so inflected forms match),
   length, number of files mentioned, code blocks and multi-step lists. A score above
   `opus_threshold` means Opus and below `haiku_threshold` means Haiku. Anything in between goes
   to the judge, which answers 1–3.
4. The session policy (`policy`) decides how the gear changes over a conversation:
   - `upgrade_only` (default): the gear only goes up within a session, which keeps prompt caching effective,
   - `sticky`: decided once, on the first prompt,
   - `per_prompt`: every prompt is reclassified.
5. The request is adapted to the target model. Each of these fixes came from a real rejection
   by the API:
   - Haiku: `thinking`, `effort` and `context_management` are stripped, and `max_tokens` is capped at 64K.
   - Sonnet/Haiku: mid-conversation `role: "system"` messages become `<system-reminder>` text in the user turn.
   - A context larger than `haiku_max_context_tokens` never goes to Haiku.
   - A 400 on the rerouted model is retried once on the original model, and the session is pinned to it.

## Use it in your own app

```python
from gearbox import choose_model   # or choose_model_async

model = choose_model(prompt, top_model="claude-opus-5")
client.messages.create(model=model, ...)
```

### Estimated savings

`gearbox report` prices every logged request at the tier that served it and compares that with
what the same tokens would have cost on the top tier (what Claude Code would have used without
gearbox). Token counts come from the API responses, prompt-cache writes (1.25x) and reads (0.1x)
included. It is an estimate: a real top-tier session would have had a different cache-hit pattern.
Prices (USD per million tokens) are in the `pricing` section of the config; built-in defaults
are used when it is missing. Update them if the price list changes.

## Tuning

Run `gearbox report` after a week. Prompts that the judge had to decide are candidates for new
keywords in `~/.gearbox/config.yaml`. The decision log is also a ready-made dataset if you later
want to train a real classifier (embeddings + logistic regression).

## Development

```powershell
.\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest -q
```

## Limitations

- When the proxy is down and `ANTHROPIC_BASE_URL` is set, Claude Code cannot reach the API.
  The hook and the autostart keep the proxy running, and `gearbox uninstall` reverts everything.
- Switching models mid-conversation invalidates the prompt cache. That is why `upgrade_only` is the default.
- With `opus: passthrough`, gearbox never shifts above the model Claude Code asked for,
  so run Claude Code on Opus and let gearbox shift down.
- TLS is verified against the operating-system trust store (like Claude Code does), so antivirus
  HTTPS scanning (e.g. Kaspersky) that installs its own root certificate does not break connections.
  Failed connects are retried up to 3 times. Such a request never reached the API, so a retry costs no tokens.
- Session state lives in memory. After a restart, continuations stay on the requested model
  until the next prompt.
