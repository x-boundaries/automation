"""Fake provider protocol facade for the EnergyGrid v4 offline core (#226 G3).

This module is the only provider boundary the offline core reaches, and it
reaches only in-memory fakes. It imports no Google, Gmail, SMTP, n8n, HTTP or
portal code, and it opens no socket. `require_fake_connectors` refuses any
connector object that is not one of the two fake classes below, and
`config.assert_offline_fake_profile` refuses every live profile.

- `ActionRequest` carries the presentation name, the archive SHA-256 identity
  and the archive bytes that a simulated upload or attachment would carry.
- `ActionOutcome` is what a provider reported. The state layer turns it into
  one immutable receipt. Replayed and conflicting outcomes are judged there.
- `FakeProviderLedger` records every simulated effect. It is the evidence that
  a run produced zero, or exactly one, effect per intent.
- `SimulatedCrash` is a `BaseException`, so no `except Exception` in the core
  swallows it. Tests use it to model process death at a named point.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .errors import OfflineV4Error

ACTION_DRIVE_UPLOAD = "DRIVE_UPLOAD"
ACTION_GMAIL_SEND = "GMAIL_SEND"
# Drive first. The Gmail intent is dispatched only after its Drive intent is VERIFIED.
ACTIONS = (ACTION_DRIVE_UPLOAD, ACTION_GMAIL_SEND)

OUTCOME_VERIFIED = "VERIFIED"
OUTCOME_REJECTED = "REJECTED"
OUTCOME_UNKNOWN = "UNKNOWN"


class SimulatedCrash(BaseException):
    """Test seam: the process dies at a named point. The core never catches it."""


@dataclass(frozen=True)
class ActionRequest:
    intent_id: str
    action: str
    stream: str
    display_filename: str
    sha256: str
    byte_size: int
    payload: bytes = field(repr=False)
    # Gmail only: the provider reference of the VERIFIED Drive object to attach.
    drive_provider_ref: str | None = None


@dataclass(frozen=True)
class ActionOutcome:
    outcome: str
    provider_ref: str
    provider_sha256: str


def _provider_ref(action: str, intent_id: str) -> str:
    material = f"energygrid.v4.fake_provider\n{action}\n{intent_id}"
    return "fake-" + hashlib.sha256(material.encode("ascii")).hexdigest()[:32]


class FakeProviderLedger:
    """In-memory record of simulated effects. Nothing here leaves the process."""

    def __init__(self) -> None:
        self.effects: list[dict[str, object]] = []
        self.objects: dict[str, dict[str, object]] = {}

    def record(self, *, action: str, intent_id: str, provider_ref: str, sha256: str, display_filename: str) -> bool:
        """Record one simulated object. Returns False when the same object already exists."""
        if provider_ref in self.objects:
            return False
        entry = {
            "action": action,
            "intent_id": intent_id,
            "provider_ref": provider_ref,
            "sha256": sha256,
            "display_filename": display_filename,
        }
        self.objects[provider_ref] = entry
        self.effects.append(entry)
        return True

    def count(self, action: str) -> int:
        return sum(1 for entry in self.effects if entry["action"] == action)


class FakeDriveConnector:
    """Simulated Drive upload. Verifies the exact archive bytes before it reports VERIFIED."""

    action = ACTION_DRIVE_UPLOAD

    def __init__(
        self,
        ledger: FakeProviderLedger,
        *,
        crash_after_effect: bool = False,
        fail_after_effect: bool = False,
    ) -> None:
        self._ledger = ledger
        self._crash_after_effect = crash_after_effect
        self._fail_after_effect = fail_after_effect
        self.calls = 0

    def perform(self, request: ActionRequest) -> ActionOutcome:
        if request.action != self.action:
            raise OfflineV4Error("EG_V4_CONNECTOR_ACTION_MISMATCH")
        self.calls += 1
        observed = hashlib.sha256(request.payload).hexdigest()
        if observed != request.sha256 or len(request.payload) != request.byte_size:
            return ActionOutcome(OUTCOME_REJECTED, "", observed)
        ref = _provider_ref(self.action, request.intent_id)
        self._ledger.record(
            action=self.action,
            intent_id=request.intent_id,
            provider_ref=ref,
            sha256=observed,
            display_filename=request.display_filename,
        )
        if self._crash_after_effect:
            self._crash_after_effect = False
            raise SimulatedCrash("after_effect")
        if self._fail_after_effect:
            self._fail_after_effect = False
            raise TimeoutError("simulated response lost after effect")
        return ActionOutcome(OUTCOME_VERIFIED, ref, observed)


class FakeGmailConnector:
    """Simulated Gmail send. It refuses to attach anything that is not a VERIFIED Drive object."""

    action = ACTION_GMAIL_SEND

    def __init__(self, ledger: FakeProviderLedger, *, fail_after_effect: bool = False) -> None:
        self._ledger = ledger
        self._fail_after_effect = fail_after_effect
        self.calls = 0

    def perform(self, request: ActionRequest) -> ActionOutcome:
        if request.action != self.action:
            raise OfflineV4Error("EG_V4_CONNECTOR_ACTION_MISMATCH")
        self.calls += 1
        observed = hashlib.sha256(request.payload).hexdigest()
        drive_object = self._ledger.objects.get(request.drive_provider_ref or "")
        if (
            drive_object is None
            or drive_object["action"] != ACTION_DRIVE_UPLOAD
            or drive_object["sha256"] != request.sha256
            or observed != request.sha256
            or len(request.payload) != request.byte_size
        ):
            return ActionOutcome(OUTCOME_REJECTED, "", observed)
        ref = _provider_ref(self.action, request.intent_id)
        self._ledger.record(
            action=self.action,
            intent_id=request.intent_id,
            provider_ref=ref,
            sha256=observed,
            display_filename=request.display_filename,
        )
        if self._fail_after_effect:
            self._fail_after_effect = False
            raise TimeoutError("simulated response lost after effect")
        return ActionOutcome(OUTCOME_VERIFIED, ref, observed)


def require_fake_connectors(connectors: dict[str, object]) -> None:
    """Refuse anything that is not exactly the two fake connectors, one per action."""
    if set(connectors) != set(ACTIONS):
        raise OfflineV4Error("EG_V4_CONNECTOR_SET_INVALID")
    expected = {ACTION_DRIVE_UPLOAD: FakeDriveConnector, ACTION_GMAIL_SEND: FakeGmailConnector}
    for action, connector in connectors.items():
        if type(connector) is not expected[action]:
            raise OfflineV4Error("EG_V4_CONNECTOR_NOT_FAKE")


def require_fake_adapters(adapters: dict[str, object]) -> None:
    """Refuse any portal adapter that does not declare itself an offline fake.

    An adapter is accepted only when its class sets OFFLINE_FAKE = True. Real portal and
    HTTP source classes do not, so the offline core cannot reach them by accident.
    """
    if not set(adapters) <= {"EB_BILL", "TENANT_BILL"}:
        raise OfflineV4Error("EG_V4_ADAPTER_SET_INVALID")
    for adapter in adapters.values():
        if getattr(type(adapter), "OFFLINE_FAKE", False) is not True:
            raise OfflineV4Error("EG_V4_ADAPTER_NOT_FAKE")
