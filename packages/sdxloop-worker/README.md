# sdxloop-worker

The in-sandbox runtime for [sdxloop](https://github.com/brettbergin/sdxloop):
shared host/worker protocol models, the job runner (`python -m sdxloop_worker`),
and agent backends. Install with the `copilot` extra inside agent sandboxes:

```bash
pip install "sdxloop-worker[copilot]"
```

You normally never install this directly — the sdxloop host package provisions
it into sandboxes automatically.
