# sbxloop-worker

The in-sandbox runtime for [sbxloop](https://github.com/brettbergin/sbxloop):
shared host/worker protocol models, the job runner (`python -m sbxloop_worker`),
and agent backends. Install with the `copilot` extra inside agent sandboxes:

```bash
pip install "sbxloop-worker[copilot]"
```

You normally never install this directly — the sbxloop host package provisions
it into sandboxes automatically.
