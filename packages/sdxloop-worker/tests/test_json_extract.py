from sdxloop_worker._json import extract_json


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


def test_invalid_fence_and_invalid_whole_text_is_none() -> None:
    # The broken fence swallows its content and the whole text (fences
    # included) is not valid JSON either -> extraction fails cleanly; the
    # engine handles this by retrying the phase with the error appended.
    text = '```json\n{broken\n```\n{"ok": 1}'
    assert extract_json(text) is None
