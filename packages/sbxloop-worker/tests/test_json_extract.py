from sbxloop_worker._json import extract_json


def test_fenced_json_block() -> None:
    text = 'thinking...\n```json\n{"a": 1}\n```\n'
    assert extract_json(text) == {"a": 1}


def test_last_fenced_block_wins() -> None:
    text = '```json\n{"draft": true}\n```\nrevised:\n```json\n{"final": true}\n```'
    assert extract_json(text) == {"final": True}


def test_plain_fence_without_language() -> None:
    text = "```\n[1, 2, 3]\n```"
    assert extract_json(text) == [1, 2, 3]


def test_whole_text_json() -> None:
    assert extract_json('  {"x": [1]}  ') == {"x": [1]}


def test_scalar_json_rejected() -> None:
    assert extract_json("42") is None
    assert extract_json('"just a string"') is None


def test_no_json_returns_none() -> None:
    assert extract_json("no structured data here") is None


def test_broken_fence_recovers_embedded_value() -> None:
    # Behavior change with the embedded-JSON fallback (0.5.x field fix):
    # a broken fence no longer poisons the reply — the valid object
    # outside it is salvaged. Schema validation still guards the shape,
    # and the engine retries on a schema mismatch.
    text = '```json\n{broken\n```\n{"ok": 1}'
    assert extract_json(text) == {"ok": 1}
