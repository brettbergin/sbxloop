# sbxloop-worker

The in-sandbox runtime for [sbxloop](https://github.com/brettbergin/sbxloop):
shared host/worker protocol models, the job runner (`python -m sbxloop_worker`),
and agent backends. Install with the `copilot` extra inside agent sandboxes:

```bash
pip install "sbxloop-worker[copilot]"
```

You normally never install this directly — the sbxloop host package provisions
it into sandboxes automatically.

The `claude` and `codex` extras select the other agent backends. `codex`
installs the Python `openai-codex` SDK and its matching Codex CLI runtime;
it does not require Node. The worker uses `OPENAI_API_KEY` and exposes its
own governed local tools plus the host's event/file tool relay. See the
[Codex backend design](../../docs/codex-backend.md) for capabilities and
verification limits.
