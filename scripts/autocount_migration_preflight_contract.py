"""Pure contract library for the repository-only AutoCount 2.0 migration preflight.

This module is the Python source of truth for the deterministic migration
preflight and manifest contract locked as ``DL-XB-140-001-A4``. It performs no
network, AutoCount, SQL, n8n, Google, SMB or scheduler action, opens no vendor
workbook, and holds no credentials. Every input is supplied by the caller as
already-parsed public-safe JSON-shaped data.

What this module is
-------------------

It PREPARES and VALIDATES migration evidence. It never performs, triggers,
approves or attests an AutoCount import. Production import remains a
human/vendor-controlled AutoCount action (lock B5/B6), so the strongest outcome
this module can ever emit is ``REPOSITORY_PREFLIGHT_SATISFIED``, which is
explicitly not production-import approval. ``import_performed`` is a hard-coded
``False`` in every result.

Fail-closed model
-----------------

Three finding states exist and they are kept distinct on purpose:

* ``PASS``     - positively evidenced.
* ``UNKNOWN``  - authority is absent, partial, stale or unverifiable. Absence of
  evidence is never converted into a negative fact (for example a missing
  import-time field-routing observation never becomes ``disabled``).
* ``MISMATCH`` - evidence exists and positively contradicts the declaration.

``UNKNOWN`` and ``MISMATCH`` both block. Only an all-``PASS`` gate set yields
``PREPARED_VALIDATION_PASS``.

Qualified field identity (lock L43-L46)
---------------------------------------

Every migration field identity is qualified by its owning template/surface. A
bare name such as ``AccNo``, ``Qty``, ``UOM``, ``Location``, ``ExpiryDate`` or
``Seq`` has no cross-surface identity merely because the spelling matches, so
this module refuses bare identifiers structurally rather than by blacklisting
the known examples. Correspondence between a source column and an AutoCount
destination exists only through an explicit declared mapping between two
qualified identities. Nothing here couples to the gated member-write subsystem
because a field name happens to match (lock L45).

Determinism
-----------

The equality-critical part of a result lives under ``canonical`` and contains no
timestamps, no run identifiers and no host paths. ``canonical_hash`` is a
SHA-256 over the canonical block using the same canonical JSON serialization the
create-UAT contract library already uses in this repository. Volatile run
metadata is attached by the CLI outside the canonical block, so repeating a run
over identical input and identical evidence reproduces byte-identical canonical
content.
"""

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

# --------------------------------------------------------------------------- #
# Contract versions
# --------------------------------------------------------------------------- #

MANIFEST_SCHEMA_VERSION = "autocount_migration_preflight_manifest/v1"
DATASET_SCHEMA_VERSION = "autocount_migration_dataset/v1"
ROUTING_EVIDENCE_SCHEMA_VERSION = "autocount_migration_routing_evidence/v1"
EXECUTION_CONTEXT_SCHEMA_VERSION = "autocount_migration_execution_context_evidence/v1"
RESULT_SCHEMA_VERSION = "autocount_migration_preflight_result/v1"

DESIGN_LOCK = "DL-XB-140-001-A4"

# --------------------------------------------------------------------------- #
# Result semantics (lock B5/B6, and the deliberate avoidance of misleading names)
# --------------------------------------------------------------------------- #

# Repository preflight outcomes. Neither value is an import claim; there is no
# IMPORTED / PRODUCTION_READY / CUTOVER_COMPLETE state in this contract because
# no repository-only evidence can establish one.
PREFLIGHT_PASS = "PREPARED_VALIDATION_PASS"
PREFLIGHT_BLOCKED = "BLOCKED"

# Readiness is reported separately from validation. REPOSITORY_PREFLIGHT_SATISFIED
# means only that every repository-checkable gate holds on the supplied evidence.
NOT_IMPORT_READY = "NOT_IMPORT_READY"
REPOSITORY_PREFLIGHT_SATISFIED = "REPOSITORY_PREFLIGHT_SATISFIED"

STATE_PASS = "PASS"
STATE_UNKNOWN = "UNKNOWN"
STATE_MISMATCH = "MISMATCH"

BLOCKING_STATES = (STATE_UNKNOWN, STATE_MISMATCH)

# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #

GATE_SOURCE_AUTHORITY = "source_authority"
GATE_PASTE_RANGE = "paste_range"
GATE_COLUMNS = "columns"
GATE_FIELD_IDENTITY = "field_identity"
GATE_ROUTING = "routing"
GATE_EXECUTION_CONTEXT = "execution_context"
GATE_DUPLICATE_ITEM_CODE_ACTION = "duplicate_item_code_action"
GATE_LOCATION = "location"
GATE_SKU_ALIAS = "sku_alias"
GATE_QUANTITY = "quantity"
GATE_ITEM_OPENING_KEY = "item_opening_key"
GATE_ARAP_ORDERING = "arap_ordering"
GATE_CONDITIONAL_AUTHORITY = "conditional_authority"

GATE_ORDER = (
    GATE_SOURCE_AUTHORITY,
    GATE_PASTE_RANGE,
    GATE_COLUMNS,
    GATE_FIELD_IDENTITY,
    GATE_ROUTING,
    GATE_EXECUTION_CONTEXT,
    GATE_DUPLICATE_ITEM_CODE_ACTION,
    GATE_LOCATION,
    GATE_SKU_ALIAS,
    GATE_QUANTITY,
    GATE_ITEM_OPENING_KEY,
    GATE_ARAP_ORDERING,
    GATE_CONDITIONAL_AUTHORITY,
)

# --------------------------------------------------------------------------- #
# Qualified target surfaces (lock A1)
# --------------------------------------------------------------------------- #

SURFACE_STOCK_ITEM = "stock_item"
SURFACE_STOCK_ITEM_OPENING = "stock_item_opening"
SURFACE_DEBTOR = "debtor"
SURFACE_CREDITOR = "creditor"
SURFACE_AR_INVOICE = "ar_invoice"
SURFACE_AP_INVOICE = "ap_invoice"

TARGET_SURFACES = (
    SURFACE_STOCK_ITEM,
    SURFACE_STOCK_ITEM_OPENING,
    SURFACE_DEBTOR,
    SURFACE_CREDITOR,
    SURFACE_AR_INVOICE,
    SURFACE_AP_INVOICE,
)

ARAP_SURFACES = (SURFACE_AR_INVOICE, SURFACE_AP_INVOICE)

# Surfaces keyed by AutoCount ItemCode, where Duplicate Item Code Action applies.
ITEM_CODE_SURFACES = (SURFACE_STOCK_ITEM, SURFACE_STOCK_ITEM_OPENING)

# --------------------------------------------------------------------------- #
# Locked structural facts
# --------------------------------------------------------------------------- #

# Lock F20/F21: documented logical Item Opening uniqueness key. SerialNo is
# deliberately absent and must never be silently promoted into the key.
ITEM_OPENING_LOGICAL_KEY = ("ItemCode", "UOM", "Location", "BatchNo", "Seq")
ITEM_OPENING_KEY_EXCLUDED = ("SerialNo",)

# Lock H30: official option names. There is no proven product-wide default, so
# this module never supplies one.
DUPLICATE_ITEM_CODE_ACTIONS = ("OverWrite", "Expand")

# Lock I32: Stock Item Qty semantics are unresolved. Master-only output must
# leave it unused/blank until Ingenious confirms.
STOCK_ITEM_UNRESOLVED_FIELDS = ("Qty",)

# Lock C11: exactly one effective import-time field-routing state is recorded.
ROUTING_ENABLED = "enabled"
ROUTING_DISABLED = "disabled"
ROUTING_UNAVAILABLE = "unavailable_not_applicable"
ROUTING_UNKNOWN = "UNKNOWN"
ROUTING_STATES = (ROUTING_ENABLED, ROUTING_DISABLED, ROUTING_UNAVAILABLE, ROUTING_UNKNOWN)
# States that positively satisfy the routing gate without a mapping.
ROUTING_POSITIVE_NON_MAPPING_STATES = (ROUTING_DISABLED, ROUTING_UNAVAILABLE)

# Lock C12: execution-context attestation states.
ATTESTATION_ATTESTED = "ATTESTED"
ATTESTATION_UNKNOWN = "UNKNOWN"
ATTESTATION_STATES = (ATTESTATION_ATTESTED, ATTESTATION_UNKNOWN)

# Lock E17/E18: no hardcoded location master. The historical codes observed in
# earlier extraction work are not authority and are intentionally absent here.
LOCATION_CONFIGURED = "configured"
LOCATION_UNKNOWN = "UNKNOWN"
LOCATION_REFERENCE_STATES = (LOCATION_CONFIGURED, LOCATION_UNKNOWN)

# Conditional authorities that remain vendor/accountant/owner owned. Nothing in
# this module ever converts one of these to RESOLVED on its own.
AUTHORITY_RESOLVED = "RESOLVED"
AUTHORITY_UNKNOWN = "UNKNOWN"
AUTHORITY_RESOLVERS = ("vendor", "accountant", "owner")

CONDITIONAL_AUTHORITY_KEYS = (
    "anomalous_ap_partial_rows",
    "ar_projno",
    "base_uom_rate_one",
    "costing",
    "duplicate_item_code_runtime_behaviour",
    "excel_zero_negative_behaviour",
    "import_api_licensing_applicability",
    "installed_runtime_applicability",
    "item_opening_excel_seq",
    "mandatory_columns",
    "opening_doc_date_mechanism",
    "production_import_order",
    "sandbox_test_company_evidence",
    "stock_item_qty_semantics",
    "udf_availability_licensing",
    "update_reimport_semantics",
)

# Authorities every surface must dispose of before any test/production import.
COMMON_REQUIRED_AUTHORITIES = (
    "import_api_licensing_applicability",
    "installed_runtime_applicability",
    "mandatory_columns",
    "production_import_order",
    "sandbox_test_company_evidence",
    "udf_availability_licensing",
    "update_reimport_semantics",
)

SURFACE_REQUIRED_AUTHORITIES = {
    SURFACE_STOCK_ITEM: (
        "base_uom_rate_one",
        "costing",
        "duplicate_item_code_runtime_behaviour",
        "stock_item_qty_semantics",
    ),
    SURFACE_STOCK_ITEM_OPENING: (
        "costing",
        "duplicate_item_code_runtime_behaviour",
        "excel_zero_negative_behaviour",
        "item_opening_excel_seq",
        "opening_doc_date_mechanism",
    ),
    SURFACE_DEBTOR: (),
    SURFACE_CREDITOR: (),
    SURFACE_AR_INVOICE: ("ar_projno",),
    SURFACE_AP_INVOICE: ("anomalous_ap_partial_rows",),
}

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #

OWNER_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
QUALIFIED_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}\.[A-Za-z][A-Za-z0-9_]{0,63}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/#-]{0,127}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FINGERPRINT_RE = re.compile(r"^acctbk_[0-9a-f]{64}$")
IMPORT_OPERATION_RE = re.compile(r"^imop_[0-9a-f]{32}$")
COLUMN_LETTER_RE = re.compile(r"^[A-Z]{1,3}$")
WORKSHEET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")

# Keys that must never appear anywhere in a manifest, dataset or evidence file.
# Repository outputs and fixtures stay public-safe.
FORBIDDEN_KEY_SUBSTRINGS = (
    "apikey",
    "connectionstring",
    "credential",
    "passphrase",
    "password",
    "privatekey",
    "pwd",
    "secret",
    "token",
)


class ContractError(ValueError):
    """Raised when input is structurally unusable as contract data.

    Structural refusal is distinct from a blocking finding: a malformed manifest
    cannot be evaluated at all, whereas a well-formed manifest with absent
    vendor authority produces a deterministic blocking UNKNOWN finding.
    """


# --------------------------------------------------------------------------- #
# Canonical serialization and hashing
# --------------------------------------------------------------------------- #


def canonical_json(obj):
    """Serialize deterministically.

    Same canonicalization the create-UAT contract library uses: keys sorted by
    Unicode code point, no insignificant whitespace, non-ASCII escaped.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_of(obj):
    """Return ``sha256:<64 lowercase hex>`` over the canonical JSON of ``obj``."""
    digest = hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def account_book_fingerprint(seed):
    """Derive a stable, non-secret account-book/company fingerprint.

    Lock C12(a) requires an evidence-backed stable identifier that stays
    public-safe in structure. The derivation is one-way, so the repository never
    stores the raw company/account-book identity. The precedent is the create-UAT
    ``source_record_id`` derivation already used in this repository.
    """
    if not isinstance(seed, str) or not seed.strip():
        raise ContractError("account-book fingerprint seed must be a non-empty string")
    digest = hashlib.sha256(f"{MANIFEST_SCHEMA_VERSION}\n{seed.strip()}".encode("utf-8")).hexdigest()
    return f"acctbk_{digest}"


# --------------------------------------------------------------------------- #
# Qualified identity helpers (lock L43-L46)
# --------------------------------------------------------------------------- #


def qualify(owner, field):
    """Build a qualified identity ``owner.field``.

    ``owner`` is the owning template/surface. There is no unqualified form: a
    bare field name is not an identity in this contract.
    """
    if not isinstance(owner, str) or not OWNER_RE.match(owner):
        raise ContractError(f"invalid identity owner: {owner!r}")
    if not isinstance(field, str) or not FIELD_RE.match(field):
        raise ContractError(f"invalid identity field: {field!r}")
    return f"{owner}.{field}"


def split_qualified(identity):
    """Split a qualified identity into ``(owner, field)``.

    Refuses a bare name. This is the structural reason a same-spelled field on
    another surface can never silently acquire this identity.
    """
    if not is_qualified(identity):
        raise ContractError(f"identity is not contract-qualified: {identity!r}")
    owner, field = identity.split(".", 1)
    return owner, field


def is_qualified(identity):
    """Return True when ``identity`` is a well-formed qualified identity."""
    return isinstance(identity, str) and bool(QUALIFIED_RE.match(identity))


# --------------------------------------------------------------------------- #
# Public-safety scan
# --------------------------------------------------------------------------- #


def assert_public_safe(obj, where):
    """Refuse obviously private material anywhere in a contract document.

    This is an integrity guard, not a secret scanner: it stops a credential or
    connection string being carried into repository evidence by operator
    mistake. It never inspects or emits the offending value.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            flattened = re.sub(r"[^a-z]", "", str(key).lower())
            for forbidden in FORBIDDEN_KEY_SUBSTRINGS:
                if forbidden in flattened:
                    raise ContractError(f"{where} carries a forbidden key: {key!r}")
            assert_public_safe(value, where)
    elif isinstance(obj, list):
        for item in obj:
            assert_public_safe(item, where)


# --------------------------------------------------------------------------- #
# Small shape helpers
# --------------------------------------------------------------------------- #


def _require(mapping, key, where):
    if not isinstance(mapping, dict) or key not in mapping:
        raise ContractError(f"{where} is missing required key: {key}")
    return mapping[key]


def _require_str(mapping, key, where, pattern=None):
    value = _require(mapping, key, where)
    if not isinstance(value, str) or not value:
        raise ContractError(f"{where}.{key} must be a non-empty string")
    if pattern is not None and not pattern.match(value):
        raise ContractError(f"{where}.{key} has an unacceptable value shape")
    return value


def _require_int(mapping, key, where, minimum=None):
    value = _require(mapping, key, where)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{where}.{key} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{where}.{key} must be >= {minimum}")
    return value


def _reject_unknown_keys(mapping, allowed, where):
    if not isinstance(mapping, dict):
        raise ContractError(f"{where} must be an object")
    extra = sorted(set(mapping) - set(allowed))
    if extra:
        raise ContractError(f"{where} carries unrecognised keys: {extra}")


def _decimal(value, where):
    """Parse a control-total style value without float rounding surprises."""
    if isinstance(value, bool):
        raise ContractError(f"{where} must be a decimal string or number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise ContractError(f"{where} is not a decimal value") from exc
    raise ContractError(f"{where} must be a decimal string or number")


def column_letter_to_index(letter):
    """Convert an Excel column letter to a 1-based index."""
    if not isinstance(letter, str) or not COLUMN_LETTER_RE.match(letter):
        raise ContractError(f"invalid spreadsheet column letter: {letter!r}")
    index = 0
    for char in letter:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index


def finding(gate, code, state, subject, detail):
    """Build one deterministic finding record."""
    if state not in (STATE_PASS, STATE_UNKNOWN, STATE_MISMATCH):
        raise ContractError(f"invalid finding state: {state!r}")
    return {
        "gate": gate,
        "code": code,
        "state": state,
        "subject": subject,
        "detail": detail,
    }


# --------------------------------------------------------------------------- #
# Manifest validation
# --------------------------------------------------------------------------- #

MANIFEST_KEYS = (
    "schema_version",
    "design_lock",
    "import_operation_id",
    "target",
    "source",
    "paste_range",
    "field_mappings",
    "duplicate_item_code_action",
    "location_reference",
    "conditional_authorities",
    "item_opening",
    "sku_identity",
    "arap_ordering",
)

PASTE_RANGE_KEYS = (
    "worksheet",
    "header_row",
    "first_row",
    "last_row",
    "first_column",
    "last_column",
)


def validate_manifest(manifest):
    """Validate manifest shape and return it unchanged.

    Shape problems raise ``ContractError``; unresolved authority does not. A
    manifest that is merely missing vendor confirmation is perfectly well formed
    and produces blocking ``UNKNOWN`` findings instead.
    """
    where = "manifest"
    assert_public_safe(manifest, where)
    _reject_unknown_keys(manifest, MANIFEST_KEYS, where)

    if _require(manifest, "schema_version", where) != MANIFEST_SCHEMA_VERSION:
        raise ContractError("manifest.schema_version is not the recognised contract version")
    if _require(manifest, "design_lock", where) != DESIGN_LOCK:
        raise ContractError("manifest.design_lock does not name the final design lock")
    _require_str(manifest, "import_operation_id", where, IMPORT_OPERATION_RE)

    target = _require(manifest, "target", where)
    _reject_unknown_keys(target, ("surface", "account_book_fingerprint"), "manifest.target")
    surface = _require_str(target, "surface", "manifest.target")
    if surface not in TARGET_SURFACES:
        raise ContractError(f"manifest.target.surface is not a known qualified surface: {surface}")
    _require_str(target, "account_book_fingerprint", "manifest.target", FINGERPRINT_RE)

    source = _require(manifest, "source", where)
    _reject_unknown_keys(
        source,
        ("dataset_id", "template_id", "template_identity", "content_sha256", "row_count", "control_totals"),
        "manifest.source",
    )
    _require_str(source, "dataset_id", "manifest.source", REF_RE)
    template_id = _require_str(source, "template_id", "manifest.source", OWNER_RE)
    _require_str(source, "template_identity", "manifest.source", REF_RE)
    _require_str(source, "content_sha256", "manifest.source", SHA256_RE)
    _require_int(source, "row_count", "manifest.source", minimum=0)
    totals = _require(source, "control_totals", "manifest.source")
    if not isinstance(totals, dict):
        raise ContractError("manifest.source.control_totals must be an object")
    for identity, value in totals.items():
        if not is_qualified(identity):
            raise ContractError(f"control total key is not contract-qualified: {identity!r}")
        _decimal(value, f"manifest.source.control_totals[{identity}]")

    _validate_paste_range(_require(manifest, "paste_range", where))
    _validate_field_mappings(_require(manifest, "field_mappings", where), template_id)
    _validate_duplicate_item_code_action(_require(manifest, "duplicate_item_code_action", where))
    _validate_location_reference(_require(manifest, "location_reference", where))
    _validate_conditional_authorities(_require(manifest, "conditional_authorities", where))

    if "item_opening" in manifest:
        _validate_item_opening_block(manifest["item_opening"])
    if surface == SURFACE_STOCK_ITEM_OPENING and "item_opening" not in manifest:
        raise ContractError("manifest.item_opening is required for the stock_item_opening surface")

    if "sku_identity" in manifest:
        _validate_sku_identity_block(manifest["sku_identity"])
    if surface == SURFACE_STOCK_ITEM and "sku_identity" not in manifest:
        raise ContractError("manifest.sku_identity is required for the stock_item surface")

    if "arap_ordering" in manifest:
        _validate_arap_ordering_block(manifest["arap_ordering"])
    if surface in ARAP_SURFACES and "arap_ordering" not in manifest:
        raise ContractError("manifest.arap_ordering is required for an AR/AP surface")

    return manifest


def _validate_paste_range(paste_range):
    where = "manifest.paste_range"
    _reject_unknown_keys(paste_range, PASTE_RANGE_KEYS, where)
    _require_str(paste_range, "worksheet", where, WORKSHEET_RE)
    header_row = _require_int(paste_range, "header_row", where, minimum=1)
    first_row = _require_int(paste_range, "first_row", where, minimum=1)
    last_row = _require_int(paste_range, "last_row", where, minimum=1)
    first_column = _require_str(paste_range, "first_column", where, COLUMN_LETTER_RE)
    last_column = _require_str(paste_range, "last_column", where, COLUMN_LETTER_RE)
    if first_row <= header_row:
        raise ContractError("manifest.paste_range.first_row must be below the header row")
    if last_row < first_row:
        raise ContractError("manifest.paste_range.last_row must not precede first_row")
    if column_letter_to_index(last_column) < column_letter_to_index(first_column):
        raise ContractError("manifest.paste_range.last_column must not precede first_column")
    return paste_range


def _validate_field_mappings(mappings, template_id):
    where = "manifest.field_mappings"
    if not isinstance(mappings, list) or not mappings:
        raise ContractError(f"{where} must be a non-empty array")
    seen_sources = set()
    seen_targets = set()
    for index, mapping in enumerate(mappings):
        item_where = f"{where}[{index}]"
        _reject_unknown_keys(mapping, ("source", "target"), item_where)
        source = _require(mapping, "source", item_where)
        target = _require(mapping, "target", item_where)
        # split_qualified refuses a bare name such as AccNo or Qty: spelling
        # alone never creates an identity (lock L43).
        source_owner, _ = split_qualified(source)
        target_owner, _ = split_qualified(target)
        if source_owner != template_id:
            raise ContractError(f"{item_where}.source is owned by {source_owner!r}, not the declared source template")
        if target_owner not in TARGET_SURFACES:
            raise ContractError(f"{item_where}.target owner is not a known surface: {target_owner!r}")
        if source in seen_sources:
            raise ContractError(f"{item_where}.source is declared more than once")
        if target in seen_targets:
            raise ContractError(f"{item_where}.target is declared more than once")
        seen_sources.add(source)
        seen_targets.add(target)
    return mappings


def _validate_duplicate_item_code_action(block):
    where = "manifest.duplicate_item_code_action"
    _reject_unknown_keys(block, ("state", "action", "evidence_ref"), where)
    state = _require_str(block, "state", where)
    if state not in ("selected", AUTHORITY_UNKNOWN):
        raise ContractError(f"{where}.state must be selected or UNKNOWN")
    if state == "selected":
        action = _require_str(block, "action", where)
        if action not in DUPLICATE_ITEM_CODE_ACTIONS:
            raise ContractError(f"{where}.action is not an official option name")
        _require_str(block, "evidence_ref", where, REF_RE)
    return block


def _validate_location_reference(block):
    where = "manifest.location_reference"
    _reject_unknown_keys(block, ("state", "source_ref", "codes"), where)
    state = _require_str(block, "state", where)
    if state not in LOCATION_REFERENCE_STATES:
        raise ContractError(f"{where}.state must be one of {list(LOCATION_REFERENCE_STATES)}")
    if state == LOCATION_CONFIGURED:
        _require_str(block, "source_ref", where, REF_RE)
        codes = _require(block, "codes", where)
        if not isinstance(codes, list) or not codes:
            raise ContractError(f"{where}.codes must be a non-empty array when the set is configured")
        for code in codes:
            if not isinstance(code, str) or not code.strip():
                raise ContractError(f"{where}.codes entries must be non-empty strings")
        if len(set(codes)) != len(codes):
            raise ContractError(f"{where}.codes must not repeat a location code")
    return block


def _validate_conditional_authorities(block):
    where = "manifest.conditional_authorities"
    if not isinstance(block, dict):
        raise ContractError(f"{where} must be an object")
    unknown_keys = sorted(set(block) - set(CONDITIONAL_AUTHORITY_KEYS))
    if unknown_keys:
        raise ContractError(f"{where} names authorities outside the locked register: {unknown_keys}")
    for key, entry in sorted(block.items()):
        entry_where = f"{where}.{key}"
        _reject_unknown_keys(entry, ("state", "resolved_by", "evidence_ref"), entry_where)
        state = _require_str(entry, "state", entry_where)
        if state not in (AUTHORITY_RESOLVED, AUTHORITY_UNKNOWN):
            raise ContractError(f"{entry_where}.state must be RESOLVED or UNKNOWN")
        if state == AUTHORITY_RESOLVED:
            resolved_by = _require_str(entry, "resolved_by", entry_where)
            if resolved_by not in AUTHORITY_RESOLVERS:
                raise ContractError(f"{entry_where}.resolved_by must be one of {list(AUTHORITY_RESOLVERS)}")
            _require_str(entry, "evidence_ref", entry_where, REF_RE)
    return block


def _validate_item_opening_block(block):
    where = "manifest.item_opening"
    _reject_unknown_keys(block, ("logical_key", "quantity_target"), where)
    key = _require(block, "logical_key", where)
    if not isinstance(key, list) or not all(isinstance(part, str) and FIELD_RE.match(part) for part in key):
        raise ContractError(f"{where}.logical_key must be an array of field names")
    _require_str(block, "quantity_target", where, QUALIFIED_RE)
    return block


def _validate_sku_identity_block(block):
    where = "manifest.sku_identity"
    _reject_unknown_keys(block, ("primary_sku_target", "require_unique_primary_sku", "require_alias_resolution"), where)
    _require_str(block, "primary_sku_target", where, QUALIFIED_RE)
    for flag in ("require_unique_primary_sku", "require_alias_resolution"):
        value = _require(block, flag, where)
        if not isinstance(value, bool):
            raise ContractError(f"{where}.{flag} must be a boolean")
    return block


def _validate_arap_ordering_block(block):
    where = "manifest.arap_ordering"
    _reject_unknown_keys(block, ("document_column", "row_type_column", "header_value", "detail_value"), where)
    _require_str(block, "document_column", where, FIELD_RE)
    _require_str(block, "row_type_column", where, FIELD_RE)
    _require_str(block, "header_value", where)
    _require_str(block, "detail_value", where)
    if block["header_value"] == block["detail_value"]:
        raise ContractError(f"{where}.header_value and detail_value must differ")
    return block


# --------------------------------------------------------------------------- #
# Dataset validation
# --------------------------------------------------------------------------- #

DATASET_KEYS = ("schema_version", "template_id", "template_identity", "columns", "rows", "sku_records", "alias_lookups")

# Lock D16: BarCode is UOM-scoped in the Stock Item template, so a barcode alias
# without a UOM qualification is ambiguous by construction.
ALIAS_UOM_SCOPED_NAMESPACES = ("barcode",)


def validate_dataset(dataset):
    """Validate dataset shape and return it unchanged."""
    where = "dataset"
    assert_public_safe(dataset, where)
    _reject_unknown_keys(dataset, DATASET_KEYS, where)
    if _require(dataset, "schema_version", where) != DATASET_SCHEMA_VERSION:
        raise ContractError("dataset.schema_version is not the recognised contract version")
    _require_str(dataset, "template_id", where, OWNER_RE)
    _require_str(dataset, "template_identity", where, REF_RE)

    columns = _require(dataset, "columns", where)
    if not isinstance(columns, list) or not columns:
        raise ContractError("dataset.columns must be a non-empty array")
    for column in columns:
        if not isinstance(column, str) or not FIELD_RE.match(column):
            raise ContractError(f"dataset column name is unusable: {column!r}")
    if len(set(columns)) != len(columns):
        raise ContractError("dataset.columns must not repeat a column name")

    rows = _require(dataset, "rows", where)
    if not isinstance(rows, list):
        raise ContractError("dataset.rows must be an array")
    for index, row in enumerate(rows):
        row_where = f"dataset.rows[{index}]"
        _reject_unknown_keys(row, ("sheet_row", "values"), row_where)
        _require_int(row, "sheet_row", row_where, minimum=1)
        values = _require(row, "values", row_where)
        if not isinstance(values, dict):
            raise ContractError(f"{row_where}.values must be an object")
        extra = sorted(set(values) - set(columns))
        if extra:
            raise ContractError(f"{row_where}.values carries columns absent from dataset.columns: {extra}")

    for record_index, record in enumerate(dataset.get("sku_records", []) or []):
        record_where = f"dataset.sku_records[{record_index}]"
        _reject_unknown_keys(record, ("primary_sku", "status", "aliases"), record_where)
        if "primary_sku" in record and not isinstance(record["primary_sku"], str):
            raise ContractError(f"{record_where}.primary_sku must be a string when present")
        status = record.get("status", "current")
        if status not in ("current", "retired"):
            raise ContractError(f"{record_where}.status must be current or retired")
        for alias_index, alias in enumerate(record.get("aliases", []) or []):
            _validate_alias(alias, f"{record_where}.aliases[{alias_index}]")

    for lookup_index, lookup in enumerate(dataset.get("alias_lookups", []) or []):
        _validate_alias(lookup, f"dataset.alias_lookups[{lookup_index}]")

    return dataset


def _validate_alias(alias, where):
    _reject_unknown_keys(alias, ("namespace", "value", "uom"), where)
    _require_str(alias, "namespace", where, OWNER_RE)
    _require_str(alias, "value", where)
    if "uom" in alias and not isinstance(alias["uom"], str):
        raise ContractError(f"{where}.uom must be a string when present")
    return alias


def dataset_content(dataset):
    """Return the deterministic, equality-critical content of a dataset.

    Only the parts that define the data being migrated participate, so the
    source hash is stable across incidental wrapper changes.
    """
    return {
        "template_id": dataset["template_id"],
        "template_identity": dataset["template_identity"],
        "columns": list(dataset["columns"]),
        "rows": list(dataset["rows"]),
        "sku_records": list(dataset.get("sku_records", []) or []),
        "alias_lookups": list(dataset.get("alias_lookups", []) or []),
    }


def dataset_content_sha256(dataset):
    """Return the ``sha256:`` source hash a manifest must declare."""
    return sha256_of(dataset_content(dataset))


# --------------------------------------------------------------------------- #
# External evidence validation
# --------------------------------------------------------------------------- #


def validate_routing_evidence(evidence):
    """Validate the external effective import-time field-routing evidence.

    This repository never inspects live AutoCount settings (lock C13). The
    evidence is an external input contract only.
    """
    where = "routing_evidence"
    assert_public_safe(evidence, where)
    _reject_unknown_keys(evidence, ("schema_version", "import_operation_id", "state", "observation_ref", "mappings"), where)
    if _require(evidence, "schema_version", where) != ROUTING_EVIDENCE_SCHEMA_VERSION:
        raise ContractError("routing_evidence.schema_version is not the recognised contract version")
    _require_str(evidence, "import_operation_id", where, IMPORT_OPERATION_RE)
    state = _require_str(evidence, "state", where)
    if state not in ROUTING_STATES:
        raise ContractError(f"{where}.state must be one of {list(ROUTING_STATES)}")
    if state != ROUTING_UNKNOWN:
        _require_str(evidence, "observation_ref", where, REF_RE)
    for index, mapping in enumerate(evidence.get("mappings", []) or []):
        item_where = f"{where}.mappings[{index}]"
        _reject_unknown_keys(mapping, ("source", "destination"), item_where)
        split_qualified(_require(mapping, "source", item_where))
        split_qualified(_require(mapping, "destination", item_where))
    return evidence


def validate_execution_context_evidence(evidence):
    """Validate the external actual import execution-context attestation.

    Two independent dimensions are bound: the actual account-book/company and
    the actual selected import operation/surface/entity (lock C12). Neither can
    substitute for the other, so they are separate objects here rather than one
    conflated block.
    """
    where = "execution_context_evidence"
    assert_public_safe(evidence, where)
    _reject_unknown_keys(evidence, ("schema_version", "import_operation_id", "account_book", "import_surface"), where)
    if _require(evidence, "schema_version", where) != EXECUTION_CONTEXT_SCHEMA_VERSION:
        raise ContractError("execution_context_evidence.schema_version is not the recognised contract version")
    _require_str(evidence, "import_operation_id", where, IMPORT_OPERATION_RE)

    if "account_book" in evidence:
        block = evidence["account_book"]
        block_where = f"{where}.account_book"
        _reject_unknown_keys(block, ("state", "actual_fingerprint", "attestation_ref"), block_where)
        state = _require_str(block, "state", block_where)
        if state not in ATTESTATION_STATES:
            raise ContractError(f"{block_where}.state must be one of {list(ATTESTATION_STATES)}")
        if state == ATTESTATION_ATTESTED:
            _require_str(block, "actual_fingerprint", block_where, FINGERPRINT_RE)
            _require_str(block, "attestation_ref", block_where, REF_RE)

    if "import_surface" in evidence:
        block = evidence["import_surface"]
        block_where = f"{where}.import_surface"
        _reject_unknown_keys(block, ("state", "actual_surface", "attestation_ref"), block_where)
        state = _require_str(block, "state", block_where)
        if state not in ATTESTATION_STATES:
            raise ContractError(f"{block_where}.state must be one of {list(ATTESTATION_STATES)}")
        if state == ATTESTATION_ATTESTED:
            actual = _require_str(block, "actual_surface", block_where)
            if actual not in TARGET_SURFACES:
                raise ContractError(f"{block_where}.actual_surface is not a known qualified surface")
            _require_str(block, "attestation_ref", block_where, REF_RE)

    return evidence


# --------------------------------------------------------------------------- #
# Gate helpers
# --------------------------------------------------------------------------- #

# Severity ordering used to fold per-finding states into one gate state.
_STATE_RANK = {STATE_PASS: 0, STATE_UNKNOWN: 1, STATE_MISMATCH: 2}


def _worst_state(states):
    worst = STATE_PASS
    for state in states:
        if _STATE_RANK[state] > _STATE_RANK[worst]:
            worst = state
    return worst


def _mapping_index(manifest):
    """Return ``{qualified_target: qualified_source}`` for the declared contract."""
    return {mapping["target"]: mapping["source"] for mapping in manifest["field_mappings"]}


def _source_column_for_target(manifest, target_identity):
    """Return the plain source column backing a qualified target, or None.

    The column is only ever obtained through an explicit declared mapping, never
    by matching spelling across surfaces.
    """
    source = _mapping_index(manifest).get(target_identity)
    if source is None:
        return None
    _, column = split_qualified(source)
    return column


def _cell(row, column):
    value = row.get("values", {}).get(column)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return value


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #


def _gate_source_authority(manifest, dataset):
    findings = []
    source = manifest["source"]

    if dataset["template_id"] != source["template_id"]:
        findings.append(
            finding(
                GATE_SOURCE_AUTHORITY,
                "template_id_mismatch",
                STATE_MISMATCH,
                source["template_id"],
                "dataset template_id does not match the manifest source template_id",
            )
        )
    if dataset["template_identity"] != source["template_identity"]:
        findings.append(
            finding(
                GATE_SOURCE_AUTHORITY,
                "template_identity_mismatch",
                STATE_MISMATCH,
                source["template_identity"],
                "dataset template identity does not match the declared template identity",
            )
        )

    actual_hash = dataset_content_sha256(dataset)
    if actual_hash != source["content_sha256"]:
        findings.append(
            finding(
                GATE_SOURCE_AUTHORITY,
                "source_content_hash_mismatch",
                STATE_MISMATCH,
                source["dataset_id"],
                "recomputed source content hash does not match the manifest declaration",
            )
        )

    if len(dataset["rows"]) != source["row_count"]:
        findings.append(
            finding(
                GATE_SOURCE_AUTHORITY,
                "row_count_mismatch",
                STATE_MISMATCH,
                source["dataset_id"],
                "dataset row count does not match the deterministic declared row count",
            )
        )

    for target_identity in sorted(source["control_totals"]):
        declared = _decimal(source["control_totals"][target_identity], "control total")
        column = _source_column_for_target(manifest, target_identity)
        if column is None:
            findings.append(
                finding(
                    GATE_SOURCE_AUTHORITY,
                    "control_total_target_unmapped",
                    STATE_UNKNOWN,
                    target_identity,
                    "control total names a target with no declared source mapping",
                )
            )
            continue
        total = Decimal(0)
        numeric = True
        for row in dataset["rows"]:
            raw = _cell(row, column)
            if raw == "":
                continue
            try:
                total += _decimal(raw, "control total row")
            except ContractError:
                numeric = False
                break
        if not numeric:
            findings.append(
                finding(
                    GATE_SOURCE_AUTHORITY,
                    "control_total_non_numeric_row",
                    STATE_MISMATCH,
                    target_identity,
                    "a row carries a non-numeric value in a control-total column",
                )
            )
        elif total != declared:
            findings.append(
                finding(
                    GATE_SOURCE_AUTHORITY,
                    "control_total_mismatch",
                    STATE_MISMATCH,
                    target_identity,
                    "recomputed control total does not match the manifest declaration",
                )
            )

    if not findings:
        findings.append(
            finding(
                GATE_SOURCE_AUTHORITY,
                "source_authority_bound",
                STATE_PASS,
                source["dataset_id"],
                "source hash, template identity, row count and control totals all match the manifest",
            )
        )
    return findings


def _gate_paste_range(manifest, dataset):
    findings = []
    paste_range = manifest["paste_range"]
    first_row = paste_range["first_row"]
    last_row = paste_range["last_row"]
    span = last_row - first_row + 1
    rows = dataset["rows"]

    if span != len(rows):
        findings.append(
            finding(
                GATE_PASTE_RANGE,
                "paste_range_row_span_mismatch",
                STATE_MISMATCH,
                paste_range["worksheet"],
                "declared bounded paste range does not span exactly the validated rows",
            )
        )

    column_span = column_letter_to_index(paste_range["last_column"]) - column_letter_to_index(paste_range["first_column"]) + 1
    if column_span < len(dataset["columns"]):
        findings.append(
            finding(
                GATE_PASTE_RANGE,
                "paste_range_column_span_too_narrow",
                STATE_MISMATCH,
                paste_range["worksheet"],
                "declared paste range is narrower than the dataset column count",
            )
        )

    seen_rows = set()
    for row in rows:
        sheet_row = row["sheet_row"]
        if sheet_row < first_row or sheet_row > last_row:
            findings.append(
                finding(
                    GATE_PASTE_RANGE,
                    "row_outside_declared_paste_range",
                    STATE_MISMATCH,
                    f"sheet_row={sheet_row}",
                    "a validated row lies outside the declared bounded paste range",
                )
            )
        if sheet_row in seen_rows:
            findings.append(
                finding(
                    GATE_PASTE_RANGE,
                    "duplicate_sheet_row",
                    STATE_MISMATCH,
                    f"sheet_row={sheet_row}",
                    "the same spreadsheet row is claimed twice",
                )
            )
        seen_rows.add(sheet_row)

    if not findings:
        findings.append(
            finding(
                GATE_PASTE_RANGE,
                "bounded_paste_range_declared",
                STATE_PASS,
                paste_range["worksheet"],
                "explicit bounded paste range is declared and consistent with the validated rows",
            )
        )
    return findings


def _structural_control_columns(manifest):
    """Source columns that carry input structure rather than a migration field.

    Only the AR/AP row-type discriminator qualifies today. It is declared in the
    manifest, so it is explicitly classified rather than silently dropped, and it
    is deliberately not mapped to a destination: inventing an AutoCount field for
    it would be exactly the kind of guessed semantics this contract forbids.
    """
    block = manifest.get("arap_ordering")
    if manifest["target"]["surface"] in ARAP_SURFACES and block:
        return {block["row_type_column"]}
    return set()


def _gate_columns(manifest, dataset):
    findings = []
    template_id = manifest["source"]["template_id"]
    mapped_columns = set()
    for mapping in manifest["field_mappings"]:
        _, column = split_qualified(mapping["source"])
        mapped_columns.add(column)

    structural_columns = _structural_control_columns(manifest) & set(dataset["columns"])
    for column in sorted(structural_columns - mapped_columns):
        findings.append(
            finding(
                GATE_COLUMNS,
                "structural_control_column_classified",
                STATE_PASS,
                qualify(template_id, column),
                "input column is explicitly declared as an input-structure control column, not a migration field",
            )
        )

    for column in sorted(set(dataset["columns"]) - mapped_columns - structural_columns):
        # Never silently dropped. A manually added supported column, a UDF column
        # or any other vendor extension lands here as unresolved authority.
        findings.append(
            finding(
                GATE_COLUMNS,
                "unrecognised_input_column",
                STATE_UNKNOWN,
                qualify(template_id, column),
                "input column is outside the confirmed contract and requires explicit classification",
            )
        )

    for column in sorted(mapped_columns - set(dataset["columns"])):
        findings.append(
            finding(
                GATE_COLUMNS,
                "declared_contract_column_absent",
                STATE_MISMATCH,
                qualify(template_id, column),
                "the contract declares a source column the dataset does not provide",
            )
        )

    if not findings:
        findings.append(
            finding(
                GATE_COLUMNS,
                "columns_fully_classified",
                STATE_PASS,
                template_id,
                "every input column is bound to exactly one declared contract mapping",
            )
        )
    return findings


def _gate_field_identity(manifest):
    findings = []
    surface = manifest["target"]["surface"]
    for mapping in manifest["field_mappings"]:
        target_owner, _ = split_qualified(mapping["target"])
        if target_owner != surface:
            findings.append(
                finding(
                    GATE_FIELD_IDENTITY,
                    "target_owned_by_other_surface",
                    STATE_MISMATCH,
                    mapping["target"],
                    "mapping targets a surface other than the declared import surface",
                )
            )
    if not findings:
        findings.append(
            finding(
                GATE_FIELD_IDENTITY,
                "identities_contract_qualified",
                STATE_PASS,
                surface,
                "every source and destination identity is qualified by its owning template or surface",
            )
        )
    return findings


def _gate_routing(manifest, routing_evidence):
    gate = GATE_ROUTING
    if routing_evidence is None:
        # Absence is never disabled (lock C11).
        return [
            finding(
                gate,
                "routing_evidence_absent",
                STATE_UNKNOWN,
                manifest["import_operation_id"],
                "no effective import-time field-routing evidence was supplied",
            )
        ]

    if routing_evidence["import_operation_id"] != manifest["import_operation_id"]:
        return [
            finding(
                gate,
                "routing_evidence_not_current",
                STATE_UNKNOWN,
                manifest["import_operation_id"],
                "routing evidence is bound to a different import operation and is not standing authority",
            )
        ]

    state = routing_evidence["state"]
    if state == ROUTING_UNKNOWN:
        return [
            finding(
                gate,
                "routing_state_unknown",
                STATE_UNKNOWN,
                manifest["import_operation_id"],
                "effective routing state is recorded as UNKNOWN",
            )
        ]

    if state in ROUTING_POSITIVE_NON_MAPPING_STATES:
        return [
            finding(
                gate,
                "routing_positively_not_applicable",
                STATE_PASS,
                state,
                "routing is positively evidenced as not altering source to destination binding",
            )
        ]

    findings = []
    contract = _mapping_index(manifest)
    declared_sources = {source: target for target, source in contract.items()}
    evidence_mappings = {}
    for mapping in routing_evidence.get("mappings", []) or []:
        evidence_mappings[mapping["source"]] = mapping["destination"]

    for source in sorted(declared_sources):
        if source not in evidence_mappings:
            findings.append(
                finding(
                    gate,
                    "routing_mapping_incomplete",
                    STATE_UNKNOWN,
                    source,
                    "enabled routing does not completely bind a participating source field",
                )
            )
        elif evidence_mappings[source] != declared_sources[source]:
            findings.append(
                finding(
                    gate,
                    "routing_destination_mismatch",
                    STATE_MISMATCH,
                    source,
                    "enabled routing sends a source field to a destination the contract does not declare",
                )
            )

    for source in sorted(set(evidence_mappings) - set(declared_sources)):
        findings.append(
            finding(
                gate,
                "routing_maps_undeclared_source",
                STATE_MISMATCH,
                source,
                "enabled routing binds a source field that is not part of the declared contract",
            )
        )

    if not findings:
        findings.append(
            finding(
                gate,
                "routing_mapping_complete_and_matching",
                STATE_PASS,
                manifest["import_operation_id"],
                "enabled routing completely and correctly binds every declared qualified identity",
            )
        )
    return findings


def _gate_execution_context(manifest, evidence):
    gate = GATE_EXECUTION_CONTEXT
    subject = manifest["import_operation_id"]
    if evidence is None:
        return [
            finding(gate, "account_book_attestation_absent", STATE_UNKNOWN, subject,
                    "no current account-book/company attestation was supplied"),
            finding(gate, "import_surface_attestation_absent", STATE_UNKNOWN, subject,
                    "no current import operation/surface/entity attestation was supplied"),
        ]

    if evidence["import_operation_id"] != subject:
        # A prior attestation never becomes standing authority (lock C12).
        return [
            finding(gate, "execution_context_not_current", STATE_UNKNOWN, subject,
                    "execution-context attestation is bound to a different import operation"),
        ]

    findings = []

    account_book = evidence.get("account_book")
    if account_book is None or account_book["state"] != ATTESTATION_ATTESTED:
        findings.append(
            finding(gate, "account_book_attestation_absent", STATE_UNKNOWN, subject,
                    "account-book/company target is not positively attested for this import operation")
        )
    elif account_book["actual_fingerprint"] != manifest["target"]["account_book_fingerprint"]:
        findings.append(
            finding(gate, "account_book_mismatch", STATE_MISMATCH, subject,
                    "attested actual account-book/company differs from the declared target")
        )
    else:
        findings.append(
            finding(gate, "account_book_attested_and_matching", STATE_PASS, subject,
                    "actual account-book/company is positively attested and matches the declaration")
        )

    import_surface = evidence.get("import_surface")
    if import_surface is None or import_surface["state"] != ATTESTATION_ATTESTED:
        findings.append(
            finding(gate, "import_surface_attestation_absent", STATE_UNKNOWN, subject,
                    "actual import operation/surface/entity is not positively attested for this import operation")
        )
    elif import_surface["actual_surface"] != manifest["target"]["surface"]:
        findings.append(
            finding(gate, "import_surface_mismatch", STATE_MISMATCH, subject,
                    "attested actual import surface/entity differs from the declared target")
        )
    else:
        findings.append(
            finding(gate, "import_surface_attested_and_matching", STATE_PASS, subject,
                    "actual import surface/entity is positively attested and matches the declaration")
        )

    return findings


def _gate_duplicate_item_code_action(manifest):
    gate = GATE_DUPLICATE_ITEM_CODE_ACTION
    surface = manifest["target"]["surface"]
    block = manifest["duplicate_item_code_action"]
    if surface not in ITEM_CODE_SURFACES:
        return [
            finding(gate, "duplicate_item_code_action_not_applicable", STATE_PASS, surface,
                    "the declared surface is not keyed by ItemCode, so no duplicate-item action applies")
        ]
    if block["state"] != "selected":
        return [
            finding(gate, "duplicate_item_code_action_unknown", STATE_UNKNOWN, surface,
                    "the selected Duplicate Item Code Action is not evidenced; no default is ever assumed")
        ]
    return [
        finding(gate, "duplicate_item_code_action_selected", STATE_PASS, block["action"],
                "an official Duplicate Item Code Action is explicitly selected and evidenced")
    ]


def _gate_location(manifest, dataset):
    gate = GATE_LOCATION
    surface = manifest["target"]["surface"]
    location_target = qualify(surface, "Location")
    column = _source_column_for_target(manifest, location_target)
    reference = manifest["location_reference"]

    if column is None:
        return [
            finding(gate, "location_validation_not_required", STATE_PASS, surface,
                    "the declared contract binds no Location destination on this surface")
        ]

    if reference["state"] != LOCATION_CONFIGURED:
        return [
            finding(gate, "location_reference_set_absent", STATE_UNKNOWN, location_target,
                    "the dataset requires Location validation but no configured reference set was supplied")
        ]

    configured = set(reference["codes"])
    findings = []
    for row in dataset["rows"]:
        value = _cell(row, column)
        if value == "":
            findings.append(
                finding(gate, "location_value_absent", STATE_UNKNOWN, f"sheet_row={row['sheet_row']}",
                        "row carries no Location value to validate against the configured set")
            )
        elif value not in configured:
            findings.append(
                finding(gate, "location_outside_configured_set", STATE_MISMATCH, f"sheet_row={row['sheet_row']}",
                        "row Location is outside the supplied configured reference set")
            )

    if not findings:
        findings.append(
            finding(gate, "locations_within_configured_set", STATE_PASS, reference["source_ref"],
                    "every row Location is inside the explicitly configured reference set")
        )
    return findings


def _alias_key(alias):
    return (alias["namespace"], alias["value"], alias.get("uom"))


def _gate_sku_alias(manifest, dataset):
    gate = GATE_SKU_ALIAS
    block = manifest.get("sku_identity")
    if block is None:
        return [
            finding(gate, "sku_identity_not_applicable", STATE_PASS, manifest["target"]["surface"],
                    "the declared surface carries no X-Boundaries SKU identity contract")
        ]

    findings = []
    records = dataset.get("sku_records", []) or []
    if not records:
        return [
            finding(gate, "sku_identity_records_absent", STATE_UNKNOWN, block["primary_sku_target"],
                    "the SKU identity contract is declared but no identity records were supplied")
        ]

    current_skus = []
    # Every alias occurrence is kept, not folded into a key-to-SKU map: an alias
    # duplicated across two different SKUs must surface BOTH as a duplicate and
    # as an ambiguous resolution, and a map would hide the second SKU.
    alias_entries = []
    seen_alias_keys = set()
    for index, record in enumerate(records):
        subject = f"sku_records[{index}]"
        primary = (record.get("primary_sku") or "").strip()
        if not primary:
            findings.append(
                finding(gate, "primary_sku_missing", STATE_MISMATCH, subject,
                        "identity record carries no PrimarySKU")
            )
        elif record.get("status", "current") == "current":
            current_skus.append(primary)

        for alias_index, alias in enumerate(record.get("aliases", []) or []):
            alias_subject = f"{subject}.aliases[{alias_index}]"
            if alias["namespace"] in ALIAS_UOM_SCOPED_NAMESPACES and not alias.get("uom"):
                findings.append(
                    finding(gate, "alias_uom_qualification_missing", STATE_UNKNOWN, alias_subject,
                            "alias namespace is UOM-scoped but the alias carries no UOM qualification")
                )
            key = _alias_key(alias)
            if key in seen_alias_keys:
                findings.append(
                    finding(gate, "duplicate_alias_in_namespace", STATE_MISMATCH, alias_subject,
                            "the same alias is declared more than once inside one namespace")
                )
            seen_alias_keys.add(key)
            alias_entries.append((key, primary))

    if block["require_unique_primary_sku"]:
        seen = set()
        for index, primary in enumerate(current_skus):
            if primary in seen:
                findings.append(
                    finding(gate, "duplicate_current_primary_sku", STATE_MISMATCH, f"current_sku[{index}]",
                            "the same current PrimarySKU appears on more than one identity record")
                )
            seen.add(primary)

    current_set = set(current_skus)

    row_column = _source_column_for_target(manifest, block["primary_sku_target"])
    if row_column is None:
        findings.append(
            finding(gate, "primary_sku_target_unmapped", STATE_UNKNOWN, block["primary_sku_target"],
                    "no declared mapping binds a source column to the PrimarySKU destination")
        )
    else:
        for row in dataset["rows"]:
            value = _cell(row, row_column)
            subject = f"sheet_row={row['sheet_row']}"
            if value == "":
                findings.append(
                    finding(gate, "row_primary_sku_missing", STATE_MISMATCH, subject,
                            "row carries no PrimarySKU value")
                )
            elif value not in current_set:
                findings.append(
                    finding(gate, "row_primary_sku_not_current", STATE_MISMATCH, subject,
                            "row PrimarySKU does not resolve to a current identity record")
                )

    if block["require_alias_resolution"]:
        for index, lookup in enumerate(dataset.get("alias_lookups", []) or []):
            subject = f"alias_lookups[{index}]"
            if lookup["namespace"] in ALIAS_UOM_SCOPED_NAMESPACES and not lookup.get("uom"):
                findings.append(
                    finding(gate, "alias_lookup_uom_qualification_missing", STATE_UNKNOWN, subject,
                            "lookup omits the UOM qualification a UOM-scoped alias namespace requires")
                )
                continue
            matches = sorted(
                {
                    primary
                    for key, primary in alias_entries
                    if key == _alias_key(lookup) and primary in current_set
                }
            )
            if not matches:
                findings.append(
                    finding(gate, "alias_resolves_to_no_current_sku", STATE_MISMATCH, subject,
                            "alias does not resolve to any current PrimarySKU")
                )
            elif len(matches) > 1:
                findings.append(
                    finding(gate, "alias_resolves_to_multiple_current_skus", STATE_MISMATCH, subject,
                            "alias resolves to more than one current PrimarySKU")
                )

    if not findings:
        findings.append(
            finding(gate, "sku_identity_deterministic", STATE_PASS, block["primary_sku_target"],
                    "PrimarySKU uniqueness and alias resolution are deterministic across the supplied records")
        )
    return findings


def _gate_quantity(manifest, dataset):
    gate = GATE_QUANTITY
    surface = manifest["target"]["surface"]
    findings = []

    if surface == SURFACE_STOCK_ITEM:
        for field in STOCK_ITEM_UNRESOLVED_FIELDS:
            identity = qualify(SURFACE_STOCK_ITEM, field)
            if identity in _mapping_index(manifest):
                findings.append(
                    finding(gate, "stock_item_unresolved_field_used", STATE_UNKNOWN, identity,
                            "master-only output binds a Stock Item field whose semantics remain unresolved")
                )
        if not findings:
            findings.append(
                finding(gate, "stock_item_unresolved_fields_unused", STATE_PASS, surface,
                        "no unresolved Stock Item field participates in the declared contract")
            )
        return findings

    if surface == SURFACE_STOCK_ITEM_OPENING:
        quantity_target = manifest["item_opening"]["quantity_target"]
        column = _source_column_for_target(manifest, quantity_target)
        if column is None:
            return [
                finding(gate, "opening_quantity_target_unmapped", STATE_UNKNOWN, quantity_target,
                        "no declared mapping binds a source column to the opening quantity destination")
            ]
        for row in dataset["rows"]:
            subject = f"sheet_row={row['sheet_row']}"
            raw = _cell(row, column)
            if raw == "":
                findings.append(
                    finding(gate, "opening_quantity_absent", STATE_UNKNOWN, subject,
                            "opening row carries no quantity value")
                )
                continue
            try:
                quantity = _decimal(raw, "opening quantity")
            except ContractError:
                findings.append(
                    finding(gate, "opening_quantity_non_numeric", STATE_MISMATCH, subject,
                            "opening row quantity is not a decimal value")
                )
                continue
            if quantity == 0:
                # The Excel importer's zero-row behaviour is unproven, so a zero
                # row is surfaced for explicit disposition rather than assumed dropped.
                findings.append(
                    finding(gate, "opening_quantity_zero_requires_disposition", STATE_UNKNOWN, subject,
                            "zero opening quantity requires explicit disposition; Excel-path behaviour is unproven")
                )
            elif quantity < 0:
                findings.append(
                    finding(gate, "opening_quantity_negative_requires_disposition", STATE_UNKNOWN, subject,
                            "negative opening quantity requires explicit disposition; Excel-path behaviour is unproven")
                )
        if not findings:
            findings.append(
                finding(gate, "opening_quantities_positive", STATE_PASS, quantity_target,
                        "every opening row carries a strictly positive quantity")
            )
        return findings

    return [
        finding(gate, "quantity_gate_not_applicable", STATE_PASS, surface,
                "the declared surface carries no unresolved quantity semantics")
    ]


def _gate_item_opening_key(manifest):
    gate = GATE_ITEM_OPENING_KEY
    surface = manifest["target"]["surface"]
    if surface != SURFACE_STOCK_ITEM_OPENING:
        return [
            finding(gate, "item_opening_key_not_applicable", STATE_PASS, surface,
                    "the declared surface has no Item Opening logical key")
        ]

    findings = []
    declared_key = list(manifest["item_opening"]["logical_key"])
    for excluded in ITEM_OPENING_KEY_EXCLUDED:
        if excluded in declared_key:
            findings.append(
                finding(gate, "excluded_field_promoted_into_key", STATE_MISMATCH, excluded,
                        "a field outside the documented uniqueness key was promoted into the key")
            )
    if declared_key != list(ITEM_OPENING_LOGICAL_KEY):
        findings.append(
            finding(gate, "item_opening_key_mismatch", STATE_MISMATCH, surface,
                    "declared Item Opening logical key differs from the documented uniqueness key")
        )

    if not _authority_resolved(manifest, "item_opening_excel_seq"):
        findings.append(
            finding(gate, "item_opening_excel_seq_authority_absent", STATE_UNKNOWN, "Seq",
                    "Seq is structurally part of the key but its Excel-path representation is unevidenced")
        )

    if not findings:
        findings.append(
            finding(gate, "item_opening_key_matches_documented_grain", STATE_PASS, surface,
                    "declared logical key matches the documented grain and Excel-path Seq authority is evidenced")
        )
    return findings


def _gate_arap_ordering(manifest, dataset):
    gate = GATE_ARAP_ORDERING
    surface = manifest["target"]["surface"]
    if surface not in ARAP_SURFACES:
        return [
            finding(gate, "arap_ordering_not_applicable", STATE_PASS, surface,
                    "the declared surface carries no document/detail continuation ordering")
        ]

    block = manifest["arap_ordering"]
    findings = []
    for key in ("document_column", "row_type_column"):
        if block[key] not in dataset["columns"]:
            findings.append(
                finding(gate, "arap_ordering_column_absent", STATE_MISMATCH, block[key],
                        "the dataset does not provide a column the ordering contract depends on")
            )
    if findings:
        return findings

    previous_sheet_row = None
    previous_document = None
    seen_documents = set()
    detail_counts = {}
    document_order = []

    for row in dataset["rows"]:
        subject = f"sheet_row={row['sheet_row']}"
        if previous_sheet_row is not None and row["sheet_row"] <= previous_sheet_row:
            findings.append(
                finding(gate, "arap_input_order_broken", STATE_MISMATCH, subject,
                        "rows are not in strictly increasing input order")
            )
        previous_sheet_row = row["sheet_row"]

        document = _cell(row, block["document_column"])
        row_type = _cell(row, block["row_type_column"])

        if row_type not in (block["header_value"], block["detail_value"]):
            findings.append(
                finding(gate, "arap_row_type_unrecognised", STATE_MISMATCH, subject,
                        "row type is neither the declared header value nor the declared detail value")
            )
            previous_document = document
            continue

        if document != previous_document:
            if document in seen_documents:
                findings.append(
                    finding(gate, "arap_document_rows_not_contiguous", STATE_MISMATCH, subject,
                            "document rows are interleaved, so the continuation ordering has been broken")
                )
            elif row_type != block["header_value"]:
                findings.append(
                    finding(gate, "arap_detail_precedes_header", STATE_MISMATCH, subject,
                            "a detail continuation row precedes its document header row")
                )
            seen_documents.add(document)
            document_order.append(document)
            detail_counts.setdefault(document, 0)
        elif row_type == block["header_value"]:
            findings.append(
                finding(gate, "arap_duplicate_header", STATE_MISMATCH, subject,
                        "a second header row appears inside one document group")
            )

        if row_type == block["detail_value"]:
            detail_counts[document] = detail_counts.get(document, 0) + 1
        previous_document = document

    for index, document in enumerate(document_order):
        if detail_counts.get(document, 0) < 1:
            findings.append(
                finding(gate, "arap_document_without_detail", STATE_MISMATCH, f"document_group[{index}]",
                        "a document group carries no continuation detail row")
            )

    if not findings:
        findings.append(
            finding(gate, "arap_continuation_order_preserved", STATE_PASS, surface,
                    "document and detail continuation ordering matches the declared input ordering contract")
        )
    return findings


def _authority_resolved(manifest, key):
    entry = manifest["conditional_authorities"].get(key)
    return bool(entry) and entry.get("state") == AUTHORITY_RESOLVED


def required_authorities(surface):
    """Return the sorted conditional authorities a surface must dispose of."""
    if surface not in TARGET_SURFACES:
        raise ContractError(f"unknown surface: {surface!r}")
    return tuple(sorted(set(COMMON_REQUIRED_AUTHORITIES) | set(SURFACE_REQUIRED_AUTHORITIES[surface])))


def _gate_conditional_authority(manifest):
    gate = GATE_CONDITIONAL_AUTHORITY
    surface = manifest["target"]["surface"]
    findings = []
    for key in required_authorities(surface):
        if _authority_resolved(manifest, key):
            findings.append(
                finding(gate, "conditional_authority_resolved", STATE_PASS, key,
                        "load-bearing authority is explicitly resolved with an evidence reference")
            )
        else:
            findings.append(
                finding(gate, "conditional_authority_unresolved", STATE_UNKNOWN, key,
                        "load-bearing authority remains unresolved and is never inferred")
            )
    return findings


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #


def _sort_findings(findings):
    order = {gate: index for index, gate in enumerate(GATE_ORDER)}
    return sorted(findings, key=lambda item: (order[item["gate"]], item["code"], item["subject"]))


def build_result(manifest, findings):
    """Fold findings into the deterministic public-safe result document."""
    findings = _sort_findings(findings)
    gates = []
    for gate in GATE_ORDER:
        states = [item["state"] for item in findings if item["gate"] == gate]
        gates.append({"gate": gate, "state": _worst_state(states) if states else STATE_PASS})

    blocking = [item for item in findings if item["state"] in BLOCKING_STATES]
    passed = not blocking

    canonical = {
        "design_lock": DESIGN_LOCK,
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "import_operation_id": manifest["import_operation_id"],
        "target": {
            "surface": manifest["target"]["surface"],
            "account_book_fingerprint": manifest["target"]["account_book_fingerprint"],
        },
        "source": {
            "dataset_id": manifest["source"]["dataset_id"],
            "template_id": manifest["source"]["template_id"],
            "template_identity": manifest["source"]["template_identity"],
            "content_sha256": manifest["source"]["content_sha256"],
            "row_count": manifest["source"]["row_count"],
        },
        "gates": gates,
        "findings": findings,
        "blocking_finding_count": len(blocking),
        "preflight_status": PREFLIGHT_PASS if passed else PREFLIGHT_BLOCKED,
        "import_readiness": REPOSITORY_PREFLIGHT_SATISFIED if passed else NOT_IMPORT_READY,
        # Structural, non-negotiable facts about what this repository tool did.
        "import_performed": False,
        "live_autocount_contacted": False,
        "production_sql_writes": 0,
    }
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "canonical": canonical,
        "canonical_hash": sha256_of(canonical),
    }


def structural_refusal_result(message):
    """Return a deterministic blocked result for input that cannot be evaluated."""
    canonical = {
        "design_lock": DESIGN_LOCK,
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "structural_refusal": True,
        "findings": [
            finding(
                GATE_SOURCE_AUTHORITY,
                "input_contract_structural_refusal",
                STATE_UNKNOWN,
                "input",
                str(message),
            )
        ],
        "gates": [{"gate": gate, "state": STATE_UNKNOWN if gate == GATE_SOURCE_AUTHORITY else STATE_PASS}
                  for gate in GATE_ORDER],
        "blocking_finding_count": 1,
        "preflight_status": PREFLIGHT_BLOCKED,
        "import_readiness": NOT_IMPORT_READY,
        "import_performed": False,
        "live_autocount_contacted": False,
        "production_sql_writes": 0,
    }
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "canonical": canonical,
        "canonical_hash": sha256_of(canonical),
    }


def run_preflight(manifest, dataset, routing_evidence=None, execution_context_evidence=None):
    """Run every gate and return the deterministic public-safe result.

    Raises ``ContractError`` when the inputs are structurally unusable. Callers
    that must always produce a result document should use ``run_preflight_safe``.
    """
    validate_manifest(manifest)
    validate_dataset(dataset)
    if routing_evidence is not None:
        validate_routing_evidence(routing_evidence)
    if execution_context_evidence is not None:
        validate_execution_context_evidence(execution_context_evidence)

    findings = []
    findings.extend(_gate_source_authority(manifest, dataset))
    findings.extend(_gate_paste_range(manifest, dataset))
    findings.extend(_gate_columns(manifest, dataset))
    findings.extend(_gate_field_identity(manifest))
    findings.extend(_gate_routing(manifest, routing_evidence))
    findings.extend(_gate_execution_context(manifest, execution_context_evidence))
    findings.extend(_gate_duplicate_item_code_action(manifest))
    findings.extend(_gate_location(manifest, dataset))
    findings.extend(_gate_sku_alias(manifest, dataset))
    findings.extend(_gate_quantity(manifest, dataset))
    findings.extend(_gate_item_opening_key(manifest))
    findings.extend(_gate_arap_ordering(manifest, dataset))
    findings.extend(_gate_conditional_authority(manifest))
    return build_result(manifest, findings)


def run_preflight_safe(manifest, dataset, routing_evidence=None, execution_context_evidence=None):
    """Always return a result document; structural refusal becomes a blocked result."""
    try:
        return run_preflight(manifest, dataset, routing_evidence, execution_context_evidence)
    except ContractError as exc:
        return structural_refusal_result(exc)
