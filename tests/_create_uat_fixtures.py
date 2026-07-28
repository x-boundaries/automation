"""Shared synthetic fixtures for the single-member creation UAT tests.

Underscore-prefixed so unittest discovery (pattern ``test*.py``) never collects it
as a test module. All member values here are synthetic: ``.invalid`` emails and
``9000000x`` / ``6590000x`` numbers built at runtime. No real PII is ever written.
"""

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import member_create_uat_contract as contract  # noqa: E402

SYNTHETIC_PAYLOAD = {
    "MemberNo": "6590000001",
    "Name": "Synthetic Alpha",
    "EmailAddress": "synthetic.alpha@example.invalid",
    "MobilePhone": "",
    "DOB": "2000-03-01",
    "MemberType": "Default",
    "RegisterDate": "2026-07-01",
    "ExpiryDate": "2028-06-30",
    "OpeningPoints": 0,
}


def build_valid_package(payload_overrides=None, *, expires_in_hours=72, reviewer_id="digital"):
    """Build a structurally valid, integrity-consistent package dict."""
    payload = dict(SYNTHETIC_PAYLOAD)
    if payload_overrides:
        payload.update(payload_overrides)
    desired = {
        "MemberType": payload["MemberType"],
        "RegisterDate": payload["RegisterDate"],
        "ExpiryDate": contract.INTENDED_BUSINESS_VALUES["ExpiryDate"],
        "OpeningPoints": payload["OpeningPoints"],
    }
    srid = contract.source_record_id(payload["MemberNo"])
    fingerprint = contract.source_fingerprint(contract.build_fingerprint_fields(payload, desired))
    now = datetime.now(timezone.utc)
    package = {
        "schema_version": contract.SCHEMA_VERSION,
        "operation_id": "mcuat_" + uuid.uuid4().hex,
        "source_record_id": srid,
        "source_fingerprint": fingerprint,
        "row_number_hint": 2,
        "created_at": now.isoformat(timespec="seconds"),
        "approval": {
            "approval_id": "appr_" + uuid.uuid4().hex,
            "reviewer_id": reviewer_id,
            "decision": "approved",
            "approved_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(hours=expires_in_hours)).isoformat(timespec="seconds"),
            "source_record_id": srid,
            "source_fingerprint": fingerprint,
            "bound_package_payload_hash": "sha256:" + ("0" * 64),
        },
        "assignable_fields": list(contract.ASSIGNABLE_FIELDS),
        "member_payload": payload,
        "desired_business_fields": desired,
        "business_confirmation_required": list(contract.BUSINESS_CONFIRMATION_REQUIRED),
        "payload_hash": "sha256:" + ("0" * 64),
    }
    payload_hash = contract.compute_payload_hash(package)
    package["payload_hash"] = payload_hash
    package["approval"]["bound_package_payload_hash"] = payload_hash
    return package


def write_package(path, package):
    Path(path).write_text(json.dumps(package, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_form_csv(path, *, member_no="90000001", name="Synthetic Alpha", email="synthetic.alpha@example.invalid",
                   month="March", marketing="Yes", pdpa="Yes"):
    """Write a one-row synthetic Google Form response CSV."""
    header = "Timestamp,Full Name,AutoCount MemberNo,Email Address,Birthday Month,Marketing Consent,PDPA Acknowledged"
    row = f"2026/07/01 10:00:00,{name},{member_no},{email},{month},{marketing},{pdpa}"
    Path(path).write_text(header + "\n" + row + "\n", encoding="utf-8")


def write_decision_rows(path, *, row_number=2, decision="READY_FOR_CREATE_REVIEW"):
    header = "RowNumber,ValidationStatus,LookupStatus,DecisionCode,IssueCodes"
    row = f"{row_number},valid,not_found,{decision},"
    Path(path).write_text(header + "\n" + row + "\n", encoding="utf-8")
