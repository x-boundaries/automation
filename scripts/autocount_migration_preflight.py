"""Repository-only CLI for the AutoCount 2.0 migration preflight and manifest.

Reads a manifest, a deterministic dataset, and the two external evidence
documents, runs every gate in
``scripts/autocount_migration_preflight_contract.py``, and writes a public-safe
deterministic result plus a short Markdown report.

Safety boundary
---------------

This tool never contacts AutoCount, never opens a vendor ``.xls`` workbook,
never reads live AutoCount settings or a live AutoCount session, never writes to
production SQL, and holds no credentials. Effective import-time field routing and
actual import execution context are EXTERNAL INPUT CONTRACTS: an operator or a
later authorised lane supplies the evidence documents, and this tool only
validates them. Missing, partial, stale or unverifiable evidence yields
``UNKNOWN`` and ``NOT_IMPORT_READY``.

A successful run never means an import happened or was approved. Production
import remains a human/vendor-controlled AutoCount action.

Determinism
-----------

``result["canonical"]`` and ``result["canonical_hash"]`` are equality-critical
and carry no timestamp, run identifier or host path. Volatile run metadata is
written under ``result["volatile"]`` only, so repeating a run over identical
input and identical evidence reproduces byte-identical canonical content.

Usage::

    python scripts/autocount_migration_preflight.py \
        --manifest path/to/manifest.json \
        --dataset path/to/dataset.json \
        --routing-evidence path/to/routing.json \
        --execution-context-evidence path/to/execution_context.json \
        --output-dir path/to/run

    python scripts/autocount_migration_preflight.py --hash-dataset path/to/dataset.json

Exit codes: ``0`` when every gate passes, ``1`` when anything blocks, ``2`` for a
usage error.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autocount_migration_preflight_contract as contract  # noqa: E402

DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\review\migration_preflight"

RESULT_FILENAME = "autocount_migration_preflight_result.json"
REPORT_FILENAME = "autocount_migration_preflight_report.md"


def load_json(path, label):
    """Read one public-safe JSON document."""
    file_path = Path(path)
    if not file_path.exists():
        raise contract.ContractError(f"{label} not found: {file_path.name}")
    try:
        return json.loads(file_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise contract.ContractError(f"{label} is not valid JSON") from exc


def render_report(result):
    """Render a short public-safe Markdown report from a result document."""
    canonical = result["canonical"]
    lines = [
        "# AutoCount migration preflight",
        "",
        f"- Design lock: `{canonical['design_lock']}`",
        f"- Preflight status: `{canonical['preflight_status']}`",
        f"- Import readiness: `{canonical['import_readiness']}`",
        f"- Blocking findings: {canonical['blocking_finding_count']}",
        f"- Canonical hash: `{result['canonical_hash']}`",
        "",
        "No AutoCount contact, no import, no production SQL write was performed by this tool.",
        "A passing preflight is not production import approval.",
        "",
        "## Gates",
        "",
        "| Gate | State |",
        "| --- | --- |",
    ]
    for gate in canonical["gates"]:
        lines.append(f"| `{gate['gate']}` | `{gate['state']}` |")

    lines.extend(["", "## Findings", "", "| Gate | Code | State | Subject | Detail |", "| --- | --- | --- | --- | --- |"])
    for item in canonical["findings"]:
        lines.append(
            f"| `{item['gate']}` | `{item['code']}` | `{item['state']}` | `{item['subject']}` | {item['detail']} |"
        )
    lines.append("")
    return "\n".join(lines)


def run(args, now=None):
    """Load inputs, run the preflight, and return the result document."""
    manifest = load_json(args.manifest, "manifest")
    dataset = load_json(args.dataset, "dataset")
    routing = load_json(args.routing_evidence, "routing evidence") if args.routing_evidence else None
    execution_context = (
        load_json(args.execution_context_evidence, "execution-context evidence")
        if args.execution_context_evidence
        else None
    )

    result = contract.run_preflight_safe(manifest, dataset, routing, execution_context)
    generated_at = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    # Volatile metadata is deliberately outside the canonical block so it can
    # never affect canonical_hash or deterministic repeat-run equality.
    result["volatile"] = {
        "generated_at": generated_at,
        "routing_evidence_supplied": routing is not None,
        "execution_context_evidence_supplied": execution_context is not None,
    }
    return result


def write_outputs(result, output_dir):
    """Write the deterministic result JSON and the Markdown report."""
    run_path = Path(output_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    result_path = run_path / RESULT_FILENAME
    report_path = run_path / REPORT_FILENAME
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(render_report(result), encoding="utf-8")
    return result_path, report_path


def build_parser():
    parser = argparse.ArgumentParser(description="Repository-only AutoCount migration preflight. Never contacts AutoCount.")
    parser.add_argument("--manifest", help="Path to the migration preflight manifest JSON.")
    parser.add_argument("--dataset", help="Path to the deterministic dataset JSON.")
    parser.add_argument("--routing-evidence", help="Path to the external effective import-time routing evidence JSON.")
    parser.add_argument(
        "--execution-context-evidence",
        help="Path to the external actual import execution-context attestation JSON.",
    )
    parser.add_argument("--output-dir", default=None, help=f"Run output directory (default under {DEFAULT_OUTPUT_ROOT}).")
    parser.add_argument("--print-only", action="store_true", help="Print the result JSON without writing files.")
    parser.add_argument(
        "--hash-dataset",
        help="Print the sha256 source content hash for a dataset JSON and exit; nothing else runs.",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.hash_dataset:
        try:
            dataset = load_json(args.hash_dataset, "dataset")
            contract.validate_dataset(dataset)
        except contract.ContractError as exc:
            print(f"dataset refused: {exc}", file=sys.stderr)
            return 2
        print(contract.dataset_content_sha256(dataset))
        return 0

    if not args.manifest or not args.dataset:
        parser.error("--manifest and --dataset are required unless --hash-dataset is used")

    try:
        result = run(args)
    except contract.ContractError as exc:
        print(f"input refused: {exc}", file=sys.stderr)
        return 2

    if args.print_only:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        output_dir = args.output_dir or str(Path(DEFAULT_OUTPUT_ROOT) / result["canonical"].get("import_operation_id", "unbound"))
        result_path, report_path = write_outputs(result, output_dir)
        print(f"result: {result_path}")
        print(f"report: {report_path}")

    canonical = result["canonical"]
    print(f"preflight_status: {canonical['preflight_status']}")
    print(f"import_readiness: {canonical['import_readiness']}")
    print(f"blocking_findings: {canonical['blocking_finding_count']}")
    return 0 if canonical["preflight_status"] == contract.PREFLIGHT_PASS else 1


if __name__ == "__main__":
    sys.exit(main())
