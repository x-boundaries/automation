from __future__ import annotations

from dataclasses import dataclass


NO_NEW_BILLS = "NO_NEW_BILLS"
DOWNLOADED = "DOWNLOADED"
ALREADY_PRESENT = "ALREADY_PRESENT"
RETRYABLE_NETWORK_FAILURE = "RETRYABLE_NETWORK_FAILURE"
LOGIN_FAILED = "LOGIN_FAILED"
PORTAL_LAYOUT_CHANGED = "PORTAL_LAYOUT_CHANGED"
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
INVALID_PDF = "INVALID_PDF"
ARCHIVE_CONFLICT = "ARCHIVE_CONFLICT"
STATE_INCONSISTENT = "STATE_INCONSISTENT"
ACTION_REQUIRED = "ACTION_REQUIRED"
# DL-XB-199 G3-101: another run holds the single-run lock. Nothing was read,
# written or fetched; the next scheduled run retries naturally.
RUN_IN_PROGRESS = "RUN_IN_PROGRESS"

SUCCESS_STATUSES = {NO_NEW_BILLS, DOWNLOADED, ALREADY_PRESENT}


def exit_code_for(status: str) -> int:
    if status in SUCCESS_STATUSES:
        return 0
    if status in {RETRYABLE_NETWORK_FAILURE, DOWNLOAD_FAILED, RUN_IN_PROGRESS}:
        return 10
    if status in {
        LOGIN_FAILED,
        PORTAL_LAYOUT_CHANGED,
        INVALID_PDF,
        ARCHIVE_CONFLICT,
        STATE_INCONSISTENT,
        ACTION_REQUIRED,
    }:
        return 20
    return 64


@dataclass
class AppError(Exception):
    message: str
    status: str = ACTION_REQUIRED
    exit_code: int | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if self.exit_code is None:
            self.exit_code = exit_code_for(self.status)


class ConfigError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=ACTION_REQUIRED, exit_code=64)


class DependencyError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=ACTION_REQUIRED, exit_code=64)


class LayoutChangedError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=PORTAL_LAYOUT_CHANGED, exit_code=20)


class LoginError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=LOGIN_FAILED, exit_code=20)


class DownloadError(AppError):
    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(
            message,
            status=DOWNLOAD_FAILED if retryable else ACTION_REQUIRED,
            exit_code=10 if retryable else 20,
            retryable=retryable,
        )


class InvalidPdfError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=INVALID_PDF, exit_code=20)


class ArchiveConflictError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=ARCHIVE_CONFLICT, exit_code=20)


class StateError(AppError):
    def __init__(self, message: str, status: str = STATE_INCONSISTENT) -> None:
        super().__init__(message, status=status, exit_code=20)


# DL-XB-199 G3-101: the direct-HTTP source and its inventory contract. Each
# failure carries one closed, public-safe support reference chosen by the code
# that raised it. The message is fixed text and never carries a response body,
# endpoint, tenant identifier, filename or header value.
SUPPORT_REF_PATTERN = r"\A[A-Z][A-Z0-9_]{0,63}\Z"


class SourceContractError(AppError):
    """The source answered, but not inside the frozen contract. Never retried."""

    def __init__(self, support_ref: str, stage: str = "list") -> None:
        super().__init__("direct-HTTP source contract violated", status=PORTAL_LAYOUT_CHANGED, exit_code=20)
        self.support_ref = support_ref
        self.stage = stage


class SourceTransportError(AppError):
    """Bounded transient transport failure that exhausted its attempts."""

    def __init__(self, support_ref: str, stage: str = "list") -> None:
        super().__init__(
            "direct-HTTP transport failed",
            status=RETRYABLE_NETWORK_FAILURE,
            exit_code=10,
            retryable=True,
        )
        self.support_ref = support_ref
        self.stage = stage


class RunLockedError(AppError):
    def __init__(self) -> None:
        super().__init__("another run holds the single-run lock", status=RUN_IN_PROGRESS, exit_code=10)
        self.support_ref = "EG_RUN_ALREADY_ACTIVE"
        self.stage = "lock"
