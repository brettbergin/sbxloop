"""sbxloop exception hierarchy."""

from __future__ import annotations

from collections.abc import Sequence


class SdxloopError(Exception):
    """Base class for all sbxloop errors."""


class ConfigError(SdxloopError):
    """Invalid or unloadable configuration."""


class SecretStateError(SdxloopError):
    """A secret registration could not be inspected or replaced."""


class SbxError(SdxloopError):
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


class ProvisionError(SdxloopError):
    """Sandbox pair provisioning failed."""


class WorkerError(SdxloopError):
    """The in-sandbox worker failed or returned an invalid result."""


class WorkerTimeoutError(WorkerError):
    """The worker exceeded its job timeout and was killed."""


class ProtocolError(SdxloopError):
    """Host/worker protocol violation (bad event line, missing result, ...)."""


class GithubOpsError(SdxloopError):
    """A GitHub operation failed in the github-ops sandbox."""


class DeliveryError(SdxloopError):
    """Delivering a run's workspace as a GitHub PR failed."""


class BudgetExceededError(SdxloopError):
    """A run or task exhausted one of its budgets."""


class StateError(SdxloopError):
    """Invalid state transition or corrupted persisted state."""
