"""sbxloop exception hierarchy."""

from __future__ import annotations

from collections.abc import Sequence


class SbxloopError(Exception):
    """Base class for all sbxloop errors."""


class ConfigError(SbxloopError):
    """Invalid or unloadable configuration."""


class SecretStateError(SbxloopError):
    """A secret registration could not be inspected or replaced."""


class SbxError(SbxloopError):
    """An sbx CLI invocation failed."""

    def __init__(
        self,
        message: str,
        *,
        argv: Sequence[str] = (),
        returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr

    def __str__(self) -> str:
        base = super().__str__()
        parts = [base]
        if self.argv:
            parts.append(f"argv={' '.join(self.argv)}")
        if self.returncode is not None:
            parts.append(f"rc={self.returncode}")
        if self.stderr.strip():
            parts.append(f"stderr={self.stderr.strip()}")
        return " | ".join(parts)


class SbxNotFoundError(SbxError):
    """The sbx binary is missing, or a referenced sandbox does not exist."""


class ProvisionError(SbxloopError):
    """Sandbox pair provisioning failed."""


class BakeError(SbxloopError):
    """Building or saving a prebaked sandbox template failed."""


class WorkerError(SbxloopError):
    """The in-sandbox worker failed or returned an invalid result."""


class InvalidOutputTwice(WorkerError):
    """A JSON-expecting agent job produced no valid output on either of its
    two attempts. Its own class so a phase that must fail closed on a
    model that cannot answer — the workload judge — can tell it from a
    broken worker."""


class WorkerTimeoutError(WorkerError):
    """The worker exceeded its job timeout and was killed."""


class ProtocolError(SbxloopError):
    """Host/worker protocol violation (bad event line, missing result, ...)."""


class ServiceOpsError(SbxloopError):
    """A service op the run's service sandbox could not carry out (#765):
    a credential the run was not granted, a request the op refuses, or a
    transport failure. Never carries a credential value."""


class GithubOpsError(SbxloopError):
    """A GitHub operation failed in the github-ops sandbox.

    ``http_status`` mirrors the worker's structured status so callers that
    treat some responses as expected (404 on a probe, 409 on an empty repo)
    compare integers instead of grepping gh's prose (#221).
    """

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class RoleNotImplemented(GithubOpsError):
    """A backend was asked for an operation of a role it does not implement
    yet (#1009): the forge is configured and the read paths answer, but
    this write path has not landed. Raised, never guessed around, so a run
    that reaches it fails closed naming the backend, the role and the
    operation; ``sbxloop doctor`` lists the same roles as not implemented
    before any run starts."""

    def __init__(self, kind: str, role: str, operation: str) -> None:
        super().__init__(
            f"the {kind} backend does not implement {role}.{operation} yet; "
            f"a run on a {kind} repository cannot go past this operation"
        )
        self.kind = kind
        self.role = role
        self.operation = operation


class DeliveryError(SbxloopError):
    """Delivering a run's workspace as a GitHub PR failed."""


class EmptyDeliveryError(DeliveryError):
    """No deliverable files or changes remain; another attempt needs triage.

    An empty diff can mean the request is already satisfied, omitted work,
    or changes excluded from delivery. It does not establish completion.
    """


class DeliveryPermissionError(DeliveryError):
    """A delivery the credential is known not to be allowed to make (#752):
    the plan touches ``.github/workflows/`` and the token has no
    ``workflows: write``. Operator-actionable and never transient — no
    retry changes a permission grant — so the run ends ``blocked`` with
    the remedy named, not ``failed`` with attempts to burn.
    """

    def __init__(self, message: str, *, paths: tuple[str, ...], permission: str) -> None:
        super().__init__(message)
        self.paths = paths
        self.permission = permission


class BudgetExceededError(SbxloopError):
    """A run or task exhausted one of its budgets."""


class StateError(SbxloopError):
    """Invalid state transition or corrupted persisted state."""


class RunCancelledError(StateError):
    """The engine stopped at a phase boundary because the run was cancelled
    (in-process ``request_cancel`` or a persisted ``cancelled`` state). A
    StateError subclass so existing handlers keep working; distinct so the
    daemon can tell an operator's cancel from an infra failure that raced
    with it — the persisted state alone cannot (both leave the run
    resumable)."""


class DaemonError(SbxloopError):
    """The daemon could not start or continue (misconfiguration, no work
    sources, an unrecoverable ops sandbox)."""
