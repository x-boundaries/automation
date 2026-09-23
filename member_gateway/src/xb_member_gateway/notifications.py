"""Immutable ``welcome_v1`` message and the durable welcome-email state machine.

The gateway, never form input or the n8n export, constructs the sender,
template, subject and body. A welcome email becomes eligible only when an AC2
member result is durably ``CREATED_VERIFIED``; email failure never touches the
member job, allocation or AutoCount.

Delivery semantics:

* Positive Send Email node completion means the SMTP server accepted the
  message. It is not proof of inbox delivery.
* After a send intent is recorded, any timeout, crash, lost acknowledgement or
  unclassified failure is ``DELIVERY_OUTCOME_UNCERTAIN``. That state is never
  claimable, so no blind resend can happen.
* Only failures before a send intent (the SMTP node never ran) may retry.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Mapping

from .canonical import canonical_json
from .crypto import payload_hash
from .models import WelcomeEmailState


WELCOME_TEMPLATE_ID = "welcome_v1"
WELCOME_JOB_SCHEMA_VERSION = "xb.member.welcome_email.job.v1"
WELCOME_RESULT_SCHEMA_VERSION = "xb.member.welcome_email.result.v1"
WELCOME_OPERATION = "welcome_email.send"
WELCOME_MAX_ATTEMPTS = 3
WELCOME_LEASE_SECONDS = 120
# Eligibility delays before attempts 1, 2 and 3: a new row becomes claimable
# one minute after CREATED_VERIFIED, then +5 and +30 minutes after the 1st and
# 2nd definitively safe (pre-intent) failures. The 3rd safe failure dead-letters.
WELCOME_RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))
WELCOME_INITIAL_DELAY = WELCOME_RETRY_DELAYS[0]

# Owner-approved welcome_v1 binding (#155 owner decision). Changing any value
# is a later owner/content change and must use a new template identity.
WELCOME_V1 = {
    "template_id": WELCOME_TEMPLATE_ID,
    "from_name": "X-Boundaries",
    "from_address": "noreply@x-boundaries.com",
    "reply_to": None,
    "subject": "Welcome to X-Boundaries!",
    "text": "Welcome to X-Boundaries!",
    "content_type": "text/plain; charset=UTF-8",
}

RESULT_OUTCOMES = frozenset({"smtp_accepted", "delivery_outcome_uncertain", "failed_before_send_intent"})
LEASE_ID_RE = re.compile(r"^lease-[0-9a-f]{32}$")
OUTBOX_ID_RE = re.compile(r"^welcome-[0-9a-f]{32}$")
ERROR_CODE_RE = re.compile(r"^[a-z0-9_.:-]{1,80}$")


class WelcomeEmailError(RuntimeError):
    """Bounded, PII-free welcome-email contract violation."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# Allowed durable transitions. Anything else fails closed.
TRANSITIONS: Mapping[WelcomeEmailState, frozenset[WelcomeEmailState]] = {
    WelcomeEmailState.PENDING: frozenset({WelcomeEmailState.LEASED}),
    WelcomeEmailState.LEASED: frozenset({
        WelcomeEmailState.RETRY_WAIT,
        WelcomeEmailState.DEAD_LETTER,
        WelcomeEmailState.SEND_INTENT_RECORDED,
    }),
    WelcomeEmailState.RETRY_WAIT: frozenset({WelcomeEmailState.LEASED, WelcomeEmailState.DEAD_LETTER}),
    WelcomeEmailState.SEND_INTENT_RECORDED: frozenset({
        WelcomeEmailState.SENT,
        WelcomeEmailState.DELIVERY_OUTCOME_UNCERTAIN,
    }),
    WelcomeEmailState.SENT: frozenset(),
    WelcomeEmailState.DELIVERY_OUTCOME_UNCERTAIN: frozenset(),
    WelcomeEmailState.DEAD_LETTER: frozenset(),
}


def assert_transition(current: WelcomeEmailState, target: WelcomeEmailState) -> None:
    if target not in TRANSITIONS[current]:
        raise WelcomeEmailError("welcome_email_state_conflict")


def build_welcome_message(recipient: str) -> dict[str, Any]:
    """Return the complete private message the mailer must send verbatim."""

    if not isinstance(recipient, str) or not recipient or len(recipient) > 254 or "@" not in recipient:
        raise WelcomeEmailError("welcome_email_recipient_invalid")
    message = dict(WELCOME_V1)
    message["to"] = recipient
    return message


def welcome_message_hash(message: Mapping[str, Any]) -> str:
    return payload_hash(canonical_json(dict(message)))


def retry_delay(attempt: int) -> timedelta:
    """Delay before the next claim after safe pre-intent failure ``attempt``."""

    if attempt < 1 or attempt >= len(WELCOME_RETRY_DELAYS):
        raise WelcomeEmailError("welcome_email_attempt_invalid")
    return WELCOME_RETRY_DELAYS[attempt]


def safe_failure_target(attempt: int, max_attempts: int = WELCOME_MAX_ATTEMPTS) -> WelcomeEmailState:
    return WelcomeEmailState.DEAD_LETTER if attempt >= max_attempts else WelcomeEmailState.RETRY_WAIT


def validate_lease_request(body: Mapping[str, Any]) -> tuple[str, int]:
    if set(body) != {"lease_id", "state_version"}:
        raise WelcomeEmailError("welcome_email_request_fields_invalid")
    lease_id, version = body["lease_id"], body["state_version"]
    if not isinstance(lease_id, str) or not LEASE_ID_RE.fullmatch(lease_id):
        raise WelcomeEmailError("welcome_email_lease_invalid")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise WelcomeEmailError("welcome_email_state_version_invalid")
    return lease_id, version


def validate_result_request(body: Mapping[str, Any]) -> tuple[str, int, str, str | None]:
    if set(body) != {"schema_version", "lease_id", "state_version", "outcome", "error_code"}:
        raise WelcomeEmailError("welcome_email_request_fields_invalid")
    if body["schema_version"] != WELCOME_RESULT_SCHEMA_VERSION:
        raise WelcomeEmailError("welcome_email_result_schema_invalid")
    lease_id, version = validate_lease_request({"lease_id": body["lease_id"], "state_version": body["state_version"]})
    outcome = body["outcome"]
    if outcome not in RESULT_OUTCOMES:
        raise WelcomeEmailError("welcome_email_outcome_invalid")
    error_code = body["error_code"]
    if outcome == "smtp_accepted":
        if error_code is not None:
            raise WelcomeEmailError("welcome_email_error_code_invalid")
    elif not isinstance(error_code, str) or not ERROR_CODE_RE.fullmatch(error_code):
        raise WelcomeEmailError("welcome_email_error_code_invalid")
    return lease_id, version, outcome, error_code


def validate_outbox_id(value: str) -> str:
    if not isinstance(value, str) or not OUTBOX_ID_RE.fullmatch(value):
        raise WelcomeEmailError("welcome_email_outbox_id_invalid")
    return value


def claim_envelope(outbox: Any, message: Mapping[str, Any]) -> dict[str, Any]:
    """Private mailer claim envelope ``xb.member.welcome_email.job.v1``."""

    return {
        "schema_version": WELCOME_JOB_SCHEMA_VERSION,
        "outbox_id": outbox.outbox_id,
        "job_id": outbox.job_id,
        "source_response_ref": outbox.source_response_ref,
        "operation": WELCOME_OPERATION,
        "template_id": outbox.template_id,
        "message_hash": outbox.message_hash,
        "state": outbox.state.value,
        "state_version": outbox.state_version,
        "attempt": outbox.attempt,
        "max_attempts": outbox.max_attempts,
        "lease_id": outbox.lease_id,
        "lease_expires_at": outbox.lease_expires_at,
        "message": dict(message),
    }


def verify_message(outbox: Any) -> dict[str, Any]:
    """Rebuild the immutable message and prove it matches the stored hash."""

    message = build_welcome_message(outbox.recipient)
    if outbox.template_id != WELCOME_TEMPLATE_ID or welcome_message_hash(message) != outbox.message_hash:
        raise WelcomeEmailError("welcome_email_message_identity_mismatch")
    return message


def next_attempt_after(attempt: int, now: datetime) -> datetime:
    return now + retry_delay(attempt)
