"""`sbxloop daemon notify` — one message to the control channel, from the
host, without the daemon (#639)."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest
from typer.testing import CliRunner

from sbxloop.cli.app import app
from sbxloop.config import Config
from sbxloop.daemon import notify
from sbxloop.daemon.notify import MAX_CHARS, Posted, post_notice
from sbxloop.errors import SbxloopError

runner = CliRunner()


class Recorder:
    """An opener that records the request and answers with a canned body."""

    def __init__(self, body: bytes = b"{}", *, raise_: Exception | None = None) -> None:
        self.body = body
        self.raise_ = raise_
        self.requests: list[Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: Request, timeout_s: float) -> bytes:
        self.requests.append(request)
        self.timeouts.append(timeout_s)
        if self.raise_ is not None:
            raise self.raise_
        return self.body

    @property
    def only(self) -> Request:
        assert len(self.requests) == 1
        return self.requests[0]

    @property
    def payload(self) -> dict[str, Any]:
        data = self.only.data
        assert isinstance(data, bytes)
        result: dict[str, Any] = json.loads(data.decode())
        return result


def _discord(tmp_path: Path, channel_id: int = 123) -> Config:
    return Config.model_validate(
        {"state_dir": str(tmp_path / "state"), "discord": {"channel_id": channel_id}}
    )


def _slack(tmp_path: Path) -> Config:
    return Config.model_validate(
        {"state_dir": str(tmp_path / "state"), "slack": {"channel_id": "C0123ABCDEF"}}
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "nope", {}, io.BytesIO(b""))  # type: ignore[arg-type]


class TestDiscord:
    def test_posts_to_the_channel_with_the_bot_token(self, tmp_path: Path) -> None:
        opener = Recorder()
        posted = post_notice(
            _discord(tmp_path),
            "**deploy** starting — [run](https://example.test/run/1)",
            env={"DISCORD_BOT_TOKEN": "tok"},
            timeout_s=7.0,
            open_url=opener,
        )
        assert posted == Posted("discord", "123")
        request = opener.only
        assert request.full_url == "https://discord.com/api/v10/channels/123/messages"
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bot tok"
        assert opener.timeouts == [7.0]
        payload = opener.payload
        # House dialect goes through untouched; no pings, no link previews.
        assert payload["content"] == "**deploy** starting — [run](https://example.test/run/1)"
        assert payload["allowed_mentions"] == {"parse": []}
        assert payload["flags"] == 4

    @pytest.mark.parametrize(
        ("code", "hint"),
        [
            (401, "the bot token is invalid, or the bot is not in the channel"),
            (403, "the bot token is invalid, or the bot is not in the channel"),
            (404, "no such channel for this bot"),
            (500, "HTTP 500"),
        ],
    )
    def test_http_errors_name_what_to_fix(self, tmp_path: Path, code: int, hint: str) -> None:
        opener = Recorder(raise_=_http_error(code))
        with pytest.raises(SbxloopError, match=f"posting to discord failed: .*{hint}"):
            post_notice(_discord(tmp_path), "hi", env={"DISCORD_BOT_TOKEN": "tok"}, open_url=opener)

    def test_network_errors_are_sbxloop_errors(self, tmp_path: Path) -> None:
        opener = Recorder(raise_=urllib.error.URLError("no route"))
        with pytest.raises(SbxloopError, match=r"posting to discord failed: .*no route"):
            post_notice(_discord(tmp_path), "hi", env={"DISCORD_BOT_TOKEN": "tok"}, open_url=opener)


class TestSlack:
    def test_posts_re_dialected_with_the_bearer_token(self, tmp_path: Path) -> None:
        opener = Recorder(b'{"ok": true}')
        posted = post_notice(
            _slack(tmp_path),
            "**deploy** starting — [run](https://example.test/run/1)",
            env={"SLACK_BOT_TOKEN": "xoxb"},
            open_url=opener,
        )
        assert posted == Posted("slack", "C0123ABCDEF")
        request = opener.only
        assert request.full_url == "https://slack.com/api/chat.postMessage"
        assert request.get_header("Authorization") == "Bearer xoxb"
        payload = opener.payload
        assert payload["channel"] == "C0123ABCDEF"
        assert payload["text"] == "*deploy* starting — <https://example.test/run/1|run>"
        assert payload["unfurl_links"] is False and payload["unfurl_media"] is False

    @pytest.mark.parametrize(
        ("error", "hint"),
        [
            ("not_in_channel", "invite the app to the channel"),
            ("channel_not_found", r"check \[slack\] channel_id"),
            ("invalid_auth", "check SLACK_BOT_TOKEN"),
            ("ratelimited", "ratelimited"),
        ],
    )
    def test_a_refused_post_is_an_error_even_at_http_200(
        self, tmp_path: Path, error: str, hint: str
    ) -> None:
        opener = Recorder(json.dumps({"ok": False, "error": error}).encode())
        with pytest.raises(SbxloopError, match=f"posting to slack failed: .*{hint}"):
            post_notice(_slack(tmp_path), "hi", env={"SLACK_BOT_TOKEN": "xoxb"}, open_url=opener)

    def test_a_non_json_reply_is_an_error(self, tmp_path: Path) -> None:
        opener = Recorder(b"<html>")
        with pytest.raises(SbxloopError, match="the reply is not JSON"):
            post_notice(_slack(tmp_path), "hi", env={"SLACK_BOT_TOKEN": "xoxb"}, open_url=opener)


class TestRefusals:
    def test_headless_daemon_cannot_notify(self, tmp_path: Path) -> None:
        config = Config.model_validate({"state_dir": str(tmp_path / "state")})
        opener = Recorder()
        with pytest.raises(SbxloopError, match=r"no chat backend is configured.*\[chat\] backend"):
            post_notice(config, "hi", env={"DISCORD_BOT_TOKEN": "tok"}, open_url=opener)
        assert opener.requests == []

    @pytest.mark.parametrize(
        ("make", "token_env"),
        [(_discord, "DISCORD_BOT_TOKEN"), (_slack, "SLACK_BOT_TOKEN")],
    )
    def test_missing_token_names_the_variable(
        self, tmp_path: Path, make: Any, token_env: str
    ) -> None:
        opener = Recorder()
        with pytest.raises(SbxloopError, match=f"{token_env} is not set.*never in sbxloop.toml"):
            post_notice(make(tmp_path), "hi", env={}, open_url=opener)
        assert opener.requests == []

    @pytest.mark.parametrize("text", ["", "   \n"])
    def test_empty_text_is_refused(self, tmp_path: Path, text: str) -> None:
        opener = Recorder()
        with pytest.raises(SbxloopError, match="notice text is empty"):
            post_notice(_discord(tmp_path), text, env={"DISCORD_BOT_TOKEN": "t"}, open_url=opener)
        assert opener.requests == []

    def test_oversize_text_is_refused_before_sending(self, tmp_path: Path) -> None:
        opener = Recorder()
        with pytest.raises(SbxloopError, match=f"the limit is {MAX_CHARS}"):
            post_notice(
                _discord(tmp_path),
                "x" * (MAX_CHARS + 1),
                env={"DISCORD_BOT_TOKEN": "t"},
                open_url=opener,
            )
        assert opener.requests == []

    def test_oversize_reply_is_refused(self, tmp_path: Path) -> None:
        opener = Recorder(b"x" * (notify._MAX_REPLY_BYTES + 1))
        with pytest.raises(SbxloopError, match="reply exceeds"):
            post_notice(_discord(tmp_path), "hi", env={"DISCORD_BOT_TOKEN": "t"}, open_url=opener)


class TestCli:
    @pytest.fixture
    def workdir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        return tmp_path

    def test_notify_posts_through_the_configured_backend(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (workdir / "sbxloop.toml").write_text("[discord]\nchannel_id = 123\n")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
        opener = Recorder()
        monkeypatch.setattr(notify, "_open_url", opener)
        result = runner.invoke(
            app, ["daemon", "notify", "deploying **1.2.3** on `db`", "--timeout", "5"]
        )
        assert result.exit_code == 0, result.output
        assert "posted to discord channel 123" in result.output
        assert opener.payload["content"] == "deploying **1.2.3** on `db`"
        assert opener.timeouts == [5.0]

    def test_notify_fails_closed_without_a_backend(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (workdir / "sbxloop.toml").write_text("")
        opener = Recorder()
        monkeypatch.setattr(notify, "_open_url", opener)
        result = runner.invoke(app, ["daemon", "notify", "hello"])
        assert result.exit_code == 2, result.output
        assert "notify failed:" in result.output and "no chat backend" in result.output
        assert opener.requests == []

    def test_notify_fails_closed_without_a_token(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (workdir / "sbxloop.toml").write_text("[discord]\nchannel_id = 123\n")
        opener = Recorder()
        monkeypatch.setattr(notify, "_open_url", opener)
        result = runner.invoke(app, ["daemon", "notify", "hello"])
        assert result.exit_code == 2, result.output
        assert "DISCORD_BOT_TOKEN is not set" in result.output
        assert opener.requests == []
