"""The ``openai`` backend: a governed chat-completions loop the worker owns.

The other backends hand the model loop, tool execution and session state
to a vendor SDK. The ``openai`` Python SDK is a client, not an agent
harness: it gives one ``/v1/chat/completions`` call and nothing else, so
the worker owns the loop — call, dispatch every tool call the model made
through the governor, append the results, call again — until the model
answers with content or the tool-call ceiling is reached. It owns no new
tools: the governed layer that already exists (``codex_tools.local_tools``
and the copilot backend's governor, registry and health tracker) is what
every call goes through, so read-only sessions, per-phase ceilings and the
host-tool round trip behave identically on every backend.

Where the calls go is the sandbox's environment, delivered by provisioning
(``sbxloop_worker.protocol``): ``OPENAI_BASE_URL`` names the endpoint, the
variable ``SBXLOOP_OPENAI_API_KEY_ENV`` names holds the credential, and the
client's patience rides beside them. The credential value is replaced in
everything that reaches an event.

Chat completions is stateless — there is no server-side thread to resume —
so a session is a worker-held transcript beside the sandbox's home, keyed
by a session id this backend mints. ``resume_session_id`` continues from
it; a ``require_resume`` job whose transcript is gone fails closed with a
named reason rather than replaying tools in a fresh session.

No native MCP: a job carrying ``mcp_servers`` is refused by name. No cost
accounting: a served endpoint has no price, so ``Usage`` keeps token counts
and cost stays unset. The endpoint's wire behaviour (streaming with usage
in the final chunk, a ``/v1/models`` listing) is exercised against a stub
speaking the wire shape and the SDK against a local server; behaviour
against a real served endpoint is FIELD-UNVERIFIED until it runs on a CI
runner.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sbxloop_worker._json import extract_json
from sbxloop_worker.backends import BackendResult, BackendUnavailableError, EmitFn
from sbxloop_worker.backends.codex_tools import LocalTool, local_tools
from sbxloop_worker.backends.copilot import (
    SessionHealthTracker,
    ToolCallGovernor,
    ToolCallRegistry,
    excerpt_output,
)
from sbxloop_worker.hosttools import HostToolTimeout, request_tool, safe_call_id
from sbxloop_worker.protocol import (
    OPENAI_BASE_URL_ENV,
    OPENAI_KEY_NAME_ENV,
    OPENAI_RETRIES_ENV,
    OPENAI_TIMEOUT_ENV,
    EventTypes,
    HostToolCall,
    HostToolSpec,
    JobRequest,
    ProviderFailure,
    Usage,
)
from sbxloop_worker.rate_limits import RateLimitReport
from sbxloop_worker.secrets import redact_secrets

BACKEND_NAME = "openai"

#: Where transcripts live inside the sandbox; overridable for tests.
SESSION_DIR_ENV = "SBXLOOP_OPENAI_SESSION_DIR"
DEFAULT_SESSION_DIR = Path("~/.sbxloop/openai-sessions")
DEFAULT_KEY_ENV = "OPENAI_API_KEY"  # nosec B105 - env var name, not a secret
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_MAX_RETRIES = 2
SESSION_PREFIX = "openai-v1"

# The coding-agent framing a code job gets ahead of its system message
# (``system_preset``), the way the vendor SDKs frame theirs. Neutral: it
# names the sandbox and the tools, never a language or a project.
CODING_AGENT_PRESET = (
    "You are a coding agent working inside an isolated sandbox on a repository "
    "checkout. Use the tools you are given to inspect files, search, run commands "
    "and make changes; do not describe changes you have not made. Prefer small, "
    "verifiable steps, run the project's own checks where they exist, and report "
    "plainly what you did, what you verified and anything you could not resolve."
)

JSON_REASK = (
    "Your previous reply did not contain a parseable JSON value. Reply again with "
    "only the JSON the task asked for — no prose before or after it."
)


class EndpointError(Exception):
    """A request the endpoint did not answer usefully, as the transport
    reports it: the HTTP status when there was one, a retry hint when the
    server gave one, and ``connection`` when no answer came at all. The
    message never carries the credential — the transport strips it."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        connection: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s
        self.connection = connection


class ChatTransport(Protocol):
    """The one seam to the wire: a streamed chat completion, and the
    endpoint's model listing. The SDK-backed transport implements it; a
    stub speaking the same chunk shape stands in for tests."""

    def stream(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Yield each ``chat.completion.chunk`` as a plain dict."""
        ...

    def list_models(self) -> list[str]:
        """The ids ``GET /models`` lists; raises EndpointError when it
        cannot (a 404 is one — not every server serves a listing)."""
        ...


@dataclass(frozen=True)
class EndpointSettings:
    """What provisioning delivered about the endpoint."""

    base_url: str
    key_env: str
    api_key: str
    timeout_s: float
    max_retries: int

    @property
    def authority(self) -> str:
        """``host[:port]`` for messages: the endpoint, never the path or key."""
        rest = self.base_url.split("://", 1)[-1]
        return rest.split("/", 1)[0]


def endpoint_settings(env: dict[str, str] | None = None) -> EndpointSettings:
    """Read the endpoint from the sandbox environment, failing by name when
    provisioning did not deliver it."""
    env = dict(os.environ) if env is None else env
    base_url = env.get(OPENAI_BASE_URL_ENV, "").strip()
    if not base_url:
        raise BackendUnavailableError(
            f"{OPENAI_BASE_URL_ENV} is not set in the agent sandbox; the openai backend "
            "needs the endpoint [agent.openai] base_url names, which provisioning "
            'delivers under [agent] backend = "openai"'
        )
    key_env = env.get(OPENAI_KEY_NAME_ENV, "").strip() or DEFAULT_KEY_ENV
    api_key = env.get(key_env, "")
    if not api_key:
        raise BackendUnavailableError(
            f"{key_env} is not set in the agent sandbox; the openai backend sends it to "
            f"the endpoint at {base_url.split('://', 1)[-1].split('/', 1)[0]} (a "
            "placeholder value if the endpoint wants no credential)"
        )
    try:
        timeout_s = float(env.get(OPENAI_TIMEOUT_ENV) or DEFAULT_TIMEOUT_S)
        max_retries = int(env.get(OPENAI_RETRIES_ENV) or DEFAULT_MAX_RETRIES)
    except ValueError as exc:
        raise BackendUnavailableError(
            f"malformed openai client settings in the env: {exc}"
        ) from exc
    return EndpointSettings(
        base_url=base_url,
        key_env=key_env,
        api_key=api_key,
        timeout_s=timeout_s,
        max_retries=max_retries,
    )


def _function_spec(spec: HostToolSpec) -> dict[str, Any]:
    """The chat-completions tool shape: ``function`` with a JSON Schema
    under ``parameters`` (Codex spells the same tool differently)."""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def parse_tool_arguments(raw: Any) -> dict[str, Any] | str:
    """The model's ``function.arguments`` is a JSON *string*, not an object.
    Returns the object, or the reason it is not one — a malformed call is a
    tool failure the model can read and fix, never an exception."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return f"tool arguments must be a JSON object string, got {type(raw).__name__}"
    try:
        value = json.loads(raw)
    except ValueError as exc:
        return f"tool arguments are not valid JSON: {exc}"
    if not isinstance(value, dict):
        return f"tool arguments must be a JSON object, got {type(value).__name__}"
    return value


def session_dir(env: dict[str, str] | None = None) -> Path:
    env = dict(os.environ) if env is None else env
    raw = env.get(SESSION_DIR_ENV)
    return Path(raw) if raw else DEFAULT_SESSION_DIR.expanduser()


def _session_fingerprint(job: JobRequest, specs: list[HostToolSpec], base_url: str) -> str:
    """Bind a transcript to the capabilities and framing it was made under:
    a changed tool roster or schema, permission mode, system prompt,
    workspace, model or endpoint starts fresh instead of replaying a
    transcript whose tool results came from other tools."""
    manifest = {
        "tools": [spec.model_dump(mode="json") for spec in specs],
        "permission_mode": job.permission_mode,
        "system_preset": job.system_preset,
        "system_message": job.system_message,
        "cwd": job.cwd,
        "model": job.model,
        "base_url": base_url,
    }
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:32]


def _resume_id(handle: str | None) -> str | None:
    if handle is None:
        return None
    parts = handle.split(":")
    if len(parts) != 2 or parts[0] != SESSION_PREFIX or not parts[1].isalnum():
        return None
    return parts[1]


class _SdkTransport:
    """The official ``openai`` client, wired to the configured endpoint.

    Streams with ``stream_options.include_usage`` so token counts arrive in
    the final chunk; a server that rejects ``stream_options`` (a 400 naming
    it) is asked once more without it, and then reports no usage. Every SDK
    exception is reduced to :class:`EndpointError` here, with the
    credential stripped, so the loop never sees an SDK type.
    """

    def __init__(self, settings: EndpointSettings) -> None:
        import openai

        self._openai = openai
        self._settings = settings
        self._client = openai.OpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=settings.timeout_s,
            max_retries=settings.max_retries,
        )
        self._usage_in_stream = True

    def _error(self, exc: Exception) -> EndpointError:
        openai = self._openai
        message = str(exc).replace(self._settings.api_key, "[REDACTED]")
        if isinstance(exc, openai.APITimeoutError):
            return EndpointError(f"request timed out: {message}", connection=True)
        if isinstance(exc, openai.APIConnectionError):
            return EndpointError(f"connection failed: {message}", connection=True)
        if isinstance(exc, openai.APIStatusError):
            retry_after = None
            headers = getattr(getattr(exc, "response", None), "headers", None)
            raw = headers.get("retry-after") if headers is not None else None
            if isinstance(raw, str) and raw.strip().replace(".", "", 1).isdigit():
                retry_after = float(raw)
            return EndpointError(message, status=exc.status_code, retry_after_s=retry_after)
        return EndpointError(message)

    def stream(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        params = {**request, "stream": True}
        if self._usage_in_stream:
            params["stream_options"] = {"include_usage": True}
        try:
            try:
                response = self._client.chat.completions.create(**params)
            except self._openai.BadRequestError as exc:
                if not self._usage_in_stream or "stream_options" not in str(exc):
                    raise
                self._usage_in_stream = False
                params.pop("stream_options", None)
                response = self._client.chat.completions.create(**params)
            for chunk in response:
                yield chunk.model_dump(mode="json")
        except Exception as exc:
            if isinstance(exc, EndpointError):
                raise
            raise self._error(exc) from exc

    def list_models(self) -> list[str]:
        try:
            return [model.id for model in self._client.models.list()]
        except Exception as exc:
            raise self._error(exc) from exc


class OpenAIBackend:
    name = BACKEND_NAME

    def __init__(self, transport_factory: Any = None) -> None:
        # Tests hand in a factory returning a stub transport; the default
        # builds the SDK client for the delivered endpoint.
        self._transport_factory = transport_factory

    def rate_limits(self, *, timeout_s: float) -> RateLimitReport:
        return RateLimitReport(
            backend=self.name,
            status="unsupported",
            reason="A served endpoint exposes no read-only provider capacity query.",
        )

    def ensure_available(self) -> None:
        """The SDK, which the worker's ``[openai]`` extra installs — and
        nothing else: the endpoint is checked when a session starts."""
        try:
            import openai  # noqa: F401
        except ImportError as exc:
            raise BackendUnavailableError(
                "openai is not installed; install sbxloop-worker[openai]"
            ) from exc

    def _transport(self, settings: EndpointSettings) -> ChatTransport:
        if self._transport_factory is not None:
            transport: ChatTransport = self._transport_factory(settings)
            return transport
        return _SdkTransport(settings)

    def run_session(self, job: JobRequest, emit: EmitFn) -> BackendResult:
        if job.mcp_servers:
            raise BackendUnavailableError(
                "the openai backend does not support native MCP servers; remove native "
                "[[mcp]] entries or select a backend that supports them. Credentialed "
                "HTTP MCP must be mediated into host tools before dispatch."
            )
        self.ensure_available()
        settings = endpoint_settings()
        deadline = time.monotonic() + job.timeout_s
        session = _Session(job, emit, settings, self._transport(settings), deadline)
        return session.run()


class _Session:
    def __init__(
        self,
        job: JobRequest,
        emit: EmitFn,
        settings: EndpointSettings,
        transport: ChatTransport,
        deadline: float,
    ) -> None:
        self.job, self.emit, self.settings = job, emit, settings
        self.transport, self.deadline = transport, deadline
        self.tracker = SessionHealthTracker()
        self.governor = ToolCallGovernor(job.max_tool_calls)
        self.registry = ToolCallRegistry()
        local = local_tools(job, deadline=deadline)
        self.local: dict[str, LocalTool] = {tool.spec.name: tool for tool in local}
        self.host = {spec.name: spec for spec in job.host_tools}
        if len(self.host) != len(job.host_tools) or self.local.keys() & self.host.keys():
            raise ValueError("tool names must be unique across local and host tools")
        if self.host and not job.host_tools_dir:
            raise ValueError("host_tools need host_tools_dir")
        self.specs = [*(tool.spec for tool in local), *job.host_tools]
        self.tools = [_function_spec(spec) for spec in self.specs]
        self.fingerprint = _session_fingerprint(job, self.specs, settings.base_url)
        self.model = job.model if job.model and job.model != "auto" else None
        self.usage = Usage(backend=BACKEND_NAME)
        self.samples = 0
        self.turns = 0
        self.final_text = ""
        self.messages: list[dict[str, Any]] = []
        self.session_id: str | None = None

    # -- the loop ------------------------------------------------------------

    def run(self) -> BackendResult:
        resumed = self._open()
        if resumed is not None:
            return resumed
        assert self.job.prompt is not None
        self.messages.append({"role": "user", "content": self.job.prompt})
        try:
            self.model = self.model or self._pick_model()
            self._converse()
            output_json = None
            if self.job.expect == "json":
                output_json = extract_json(self.final_text)
                if output_json is None:
                    # One reask, then the runner's ExpectedJsonMissing:
                    # never an unbounded retry.
                    self.messages.append({"role": "user", "content": JSON_REASK})
                    self._converse()
                    output_json = extract_json(self.final_text)
        except EndpointError as exc:
            self._persist()
            return BackendResult(
                output_text=self.final_text,
                session_id=self._handle(),
                usage=self.usage if self.samples else None,
                turns=self.turns or None,
                health=self.tracker.health(self.governor),
                failure=self._failure(exc),
            )
        self._persist()
        return BackendResult(
            output_text=self.final_text,
            output_json=output_json,
            session_id=self._handle(),
            usage=self.usage if self.samples else None,
            turns=self.turns,
            health=self.tracker.health(self.governor),
        )

    def _converse(self) -> None:
        """Call until the model answers without tool calls."""
        while True:
            self._check_deadline()
            reply = self._complete()
            self.turns += 1
            content, calls = reply["content"], reply["tool_calls"]
            if content:
                self.emit(
                    EventTypes.AGENT_MESSAGE,
                    content=content,
                    model=self.model,
                    backend=BACKEND_NAME,
                )
            assistant: dict[str, Any] = {"role": "assistant", "content": content or None}
            if calls:
                assistant["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"]},
                    }
                    for call in calls
                ]
            self.messages.append(assistant)
            if not calls:
                self.final_text = content
                return
            for call in calls:
                text = self._dispatch(call["id"], call["name"], call["arguments"])
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})

    def _complete(self) -> dict[str, Any]:
        """One streamed completion: the assembled content, the tool calls
        (arguments as the JSON string the model sent) and the usage."""
        request: dict[str, Any] = {"model": self.model, "messages": self.messages}
        if self.tools:
            request["tools"] = self.tools
        content: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] | None = None
        for chunk in self.transport.stream(request):
            self._check_deadline()
            served = chunk.get("model")
            if isinstance(served, str) and served:
                self.model = served
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    content.append(piece)
                    self.emit(
                        EventTypes.AGENT_MESSAGE_DELTA,
                        delta=self._clean(piece),
                        backend=BACKEND_NAME,
                    )
                for entry in delta.get("tool_calls") or []:
                    index = entry.get("index", 0)
                    call = calls.setdefault(index, {"id": None, "name": "", "arguments": ""})
                    if entry.get("id"):
                        call["id"] = entry["id"]
                    function = entry.get("function") or {}
                    if function.get("name"):
                        call["name"] += function["name"]
                    if function.get("arguments"):
                        call["arguments"] += function["arguments"]
        if usage is not None:
            self._usage(usage)
        ordered = [calls[index] for index in sorted(calls)]
        for call in ordered:
            call["id"] = safe_call_id(call["id"])
        return {"content": self._clean("".join(content)), "tool_calls": ordered}

    def _dispatch(self, call_id: str, name: str, raw_arguments: str) -> str:
        """One tool call through the governor: the ceiling first, then the
        roster, then the call itself. Returns the text the model reads."""
        parsed = parse_tool_arguments(raw_arguments)
        arguments = parsed if isinstance(parsed, dict) else {}
        args = (
            self._args(arguments) if isinstance(parsed, dict) else self._clean(raw_arguments)[:400]
        )
        self.registry.start(call_id, name, args)
        self.emit(EventTypes.AGENT_TOOL_START, tool=name, tool_call_id=call_id, args=args)
        nudge = self.governor.decide()
        exit_code: int | None = None
        count_failure = True
        if nudge is not None:
            text, success, count_failure = nudge, False, False
            if self.governor.denied == 1:
                self.emit(
                    EventTypes.AGENT_TOOL_CAP,
                    cap=self.governor.cap,
                    calls=self.governor.calls,
                    tool=name,
                )
        elif name not in self.local and name not in self.host:
            text, success = f"The {name!r} tool is not available in this session.", False
            self.tracker.record_denial(name, call_id)
            self.emit(EventTypes.AGENT_PERMISSION_DENIED, kind=name, feedback=text)
        elif not isinstance(parsed, dict):
            text, success = parsed, False
        else:
            try:
                if name in self.host:
                    assert self.job.host_tools_dir is not None
                    response = request_tool(
                        self.emit,
                        self.job.host_tools_dir,
                        HostToolCall(call_id=call_id, name=name, arguments=arguments),
                        min(
                            self.job.host_tool_timeout_s, max(0.0, self.deadline - time.monotonic())
                        ),
                    )
                    text, success = response.text or response.error or "", response.ok
                else:
                    text, success = self.local[name].invoke(arguments), True
                    if name == "shell":
                        exit_code = json.loads(text)["exit_code"]
                        success = exit_code == 0
            except (HostToolTimeout, TimeoutError) as exc:
                if time.monotonic() >= self.deadline:
                    raise subprocess.TimeoutExpired(BACKEND_NAME, self.job.timeout_s) from None
                text, success = str(exc), False
            except Exception as exc:
                text, success = str(exc), False
        text = self._clean(text)
        _, _, duration_ms = self.registry.end(call_id)
        if count_failure:
            self.tracker.record_tool_end(name, success, call_id)
        output = excerpt_output(text)
        self.emit(
            EventTypes.AGENT_TOOL_END,
            tool=name,
            tool_call_id=call_id,
            args=args,
            success=success,
            exit_code=exit_code,
            output=output,
            error=output if not success else None,
            output_lines=len(text.splitlines()),
            duration_ms=duration_ms,
        )
        return text

    # -- model, usage, failures --------------------------------------------

    def _pick_model(self) -> str:
        """``model = "auto"``: the first model the endpoint lists — the one
        a single-model box serves. An endpoint that lists nothing cannot
        pick, and says so rather than guessing a name."""
        try:
            listed = self.transport.list_models()
        except EndpointError as exc:
            raise EndpointError(
                f'model = "auto" needs the endpoint at {self.settings.authority} to list '
                f"its models, and it did not ({exc}); set `model` to a model it serves",
                status=exc.status,
                connection=exc.connection,
            ) from exc
        if not listed:
            raise EndpointError(
                f'model = "auto" but the endpoint at {self.settings.authority} lists no '
                "models; set `model` to a model it serves"
            )
        return listed[0]

    def _usage(self, payload: dict[str, Any]) -> None:
        def count(key: str) -> int | None:
            value = payload.get(key)
            return value if type(value) is int and value >= 0 else None

        details = payload.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        sample = Usage(
            backend=BACKEND_NAME,
            model=self.model,
            input_tokens=count("prompt_tokens"),
            output_tokens=count("completion_tokens"),
            cache_read_tokens=cached if type(cached) is int and cached >= 0 else None,
        )
        self.samples += 1
        self.usage = self.usage.merged(sample)
        self.emit(EventTypes.AGENT_USAGE, **sample.model_dump(exclude_none=True))

    def _failure(self, exc: EndpointError) -> ProviderFailure:
        """Name the endpoint and the model, never the key."""
        where = f"the endpoint at {self.settings.authority}"
        model = self.model or self.job.model or "(unset)"
        message = self._clean(str(exc))
        if exc.connection:
            return ProviderFailure(
                backend=BACKEND_NAME,
                category="unavailable",
                reason=f"{where} did not answer: {message}",
                partial_progress=self.turns > 0,
            )
        if exc.status == 404:
            return ProviderFailure(
                backend=BACKEND_NAME,
                category="unavailable",
                http_status=404,
                reason=(
                    f"{where} answered 404 for model {model!r}: either the base URL "
                    f"({self.settings.base_url}) is not an OpenAI-compatible root or the "
                    f"endpoint does not serve that model — {message}"
                ),
                partial_progress=self.turns > 0,
            )
        if exc.status in (401, 403):
            return ProviderFailure(
                backend=BACKEND_NAME,
                category="unknown",
                http_status=exc.status,
                reason=(
                    f"{where} refused the credential in {self.settings.key_env} "
                    f"(HTTP {exc.status}): {message}"
                ),
                partial_progress=self.turns > 0,
            )
        if exc.status == 429:
            retry_at = time.time() + exc.retry_after_s if exc.retry_after_s else None
            return ProviderFailure(
                backend=BACKEND_NAME,
                category="throttle",
                http_status=429,
                retry_at=retry_at,
                reason=f"{where} throttled model {model!r}: {message}",
                partial_progress=self.turns > 0,
            )
        if exc.status is not None and exc.status >= 500:
            return ProviderFailure(
                backend=BACKEND_NAME,
                category="unavailable",
                http_status=exc.status,
                reason=f"{where} failed (HTTP {exc.status}) for model {model!r}: {message}",
                partial_progress=self.turns > 0,
            )
        return ProviderFailure(
            backend=BACKEND_NAME,
            category="unknown",
            http_status=exc.status,
            reason=f"{where} rejected the request for model {model!r}: {message}",
            partial_progress=self.turns > 0,
        )

    # -- sessions ---------------------------------------------------------

    def _system_prompt(self) -> str:
        message = self.job.system_message or ""
        if not self.job.system_preset:
            return message
        return f"{CODING_AGENT_PRESET}\n\n{message}".rstrip() if message else CODING_AGENT_PRESET

    def _open(self) -> BackendResult | None:
        """Start the transcript: from the persisted session when the job
        resumes one and it still binds, fresh otherwise — unless the job
        requires the resume, in which case a missing or unmatched
        transcript is the named failure it should be."""
        resume_id = _resume_id(self.job.resume_session_id)
        transcript = self._load(resume_id) if resume_id else None
        if transcript is not None:
            self.session_id = resume_id
            self.messages = transcript
            return None
        if self.job.require_resume:
            reason = (
                "the session's transcript is missing"
                if resume_id and not self._path(resume_id).is_file()
                else "no session to resume was recorded"
                if not resume_id
                else "the session's transcript was made with other tools, instructions, "
                "workspace, model or endpoint"
            )
            return BackendResult(
                session_id=self.job.resume_session_id,
                failure=ProviderFailure(
                    backend=BACKEND_NAME,
                    category="recovery",
                    partial_progress=True,
                    reason=(
                        f"The interrupted session could not resume: {reason}; inspect "
                        "preserved work before recovery"
                    ),
                ),
            )
        self.session_id = uuid.uuid4().hex
        self.messages = [{"role": "system", "content": self._system_prompt()}]
        return None

    def _handle(self) -> str:
        assert self.session_id is not None
        return f"{SESSION_PREFIX}:{self.session_id}"

    def _path(self, session_id: str) -> Path:
        return session_dir() / f"{session_id}.json"

    def _load(self, session_id: str) -> list[dict[str, Any]] | None:
        try:
            data = json.loads(self._path(session_id).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("fingerprint") != self.fingerprint:
            return None
        messages = data.get("messages")
        return messages if isinstance(messages, list) else None

    def _persist(self) -> None:
        if self.session_id is None:
            return
        path = self._path(self.session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"v": 1, "fingerprint": self.fingerprint, "messages": self.messages}
            scratch = path.with_suffix(".tmp")
            scratch.write_text(json.dumps(payload))
            scratch.replace(path)
        except OSError:
            # A transcript that could not be written is a resume that will
            # start fresh (or fail closed under require_resume) — never a
            # lost answer.
            pass

    # -- helpers --------------------------------------------------------------

    def _check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            raise subprocess.TimeoutExpired(BACKEND_NAME, self.job.timeout_s)

    def _clean(self, text: str) -> str:
        if self.settings.api_key:
            text = text.replace(self.settings.api_key, "[REDACTED]")
        return redact_secrets(text)

    def _args(self, arguments: dict[str, Any]) -> str | None:
        if not arguments:
            return None
        for key in ("command", "path", "pattern", "query"):
            if isinstance(arguments.get(key), str):
                return self._clean(arguments[key])[:400]
        return self._clean(json.dumps(arguments, separators=(",", ":")))[:400]
