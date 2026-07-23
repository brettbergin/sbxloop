from sdxloop.events import Event, EventBus, Hook


def test_subscribe_publish_and_unsubscribe() -> None:
    bus = EventBus()
    seen: list[str] = []
    unsubscribe = bus.subscribe(lambda e: seen.append(e.type))

    bus.emit("run.start", "r1")
    assert seen == ["run.start"]

    unsubscribe()
    unsubscribe()  # idempotent
    bus.emit("run.end", "r1")
    assert seen == ["run.start"]


def test_emit_returns_stamped_event() -> None:
    bus = EventBus()
    event = bus.emit("task.start", "r1", job_id="j1", task="t1")
    assert event.run_id == "r1"
    assert event.job_id == "j1"
    assert event.data == {"task": "t1"}
    assert event.ts > 0


def test_subscriber_exception_is_isolated() -> None:
    bus = EventBus()
    seen: list[str] = []

    def bad(_: Event) -> None:
        raise RuntimeError("subscriber bug")

    bus.subscribe(bad)
    bus.subscribe(lambda e: seen.append(e.type))
    bus.emit("run.start", "r1")
    assert seen == ["run.start"]


def test_hook_protocol_attach() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.types: list[str] = []

        def on_event(self, event: Event) -> None:
            self.types.append(event.type)

    recorder = Recorder()
    assert isinstance(recorder, Hook)

    bus = EventBus()
    detach = bus.attach_hook(recorder)
    bus.emit("phase.start", "r1")
    detach()
    bus.emit("phase.end", "r1")
    assert recorder.types == ["phase.start"]
