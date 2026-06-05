import argparse
import csv
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Singapore"
DEFAULT_OVERLAP_DAYS = 3


def build_run_context(
    business_date=None,
    overlap_days=DEFAULT_OVERLAP_DAYS,
    timezone_name=DEFAULT_TIMEZONE,
    now=None,
):
    tz = ZoneInfo(timezone_name)
    current_time = _coerce_datetime(now, tz) if now else datetime.now(tz)
    run_date = date.fromisoformat(business_date) if business_date else current_time.date() - timedelta(days=1)

    movement_start = datetime.combine(run_date - timedelta(days=overlap_days), time.min, tzinfo=tz)
    movement_end = datetime.combine(run_date + timedelta(days=1), time.min, tzinfo=tz)
    snapshot_as_at = datetime.combine(run_date, time(23, 59, 59), tzinfo=tz)

    return {
        "run_id": str(uuid4()),
        "business_date": run_date.isoformat(),
        "storage_batch_id": f"ac2_stock_{run_date.isoformat()}",
        "movement_from": movement_start.isoformat(),
        "movement_to": movement_end.isoformat(),
        "snapshot_as_at": snapshot_as_at.isoformat(),
        "timezone": timezone_name,
        "overlap_days": overlap_days,
        "run_started_at": current_time.isoformat(),
    }


def run_extraction(config, source=None, notifier=None, business_date=None, now=None):
    context = build_run_context(
        business_date=business_date,
        overlap_days=int(config.get("overlap_days", DEFAULT_OVERLAP_DAYS)),
        timezone_name=config.get("timezone", DEFAULT_TIMEZONE),
        now=now,
    )
    archive_root = Path(config["archive_root"]).expanduser()
    batch_dir = archive_root / context["storage_batch_id"]
    batch_dir.mkdir(parents=True, exist_ok=True)

    source = source or create_source(config)
    row_counts = {}
    dataset_files = {}
    storage_files = {}
    exceptions = []

    for dataset_name, dataset_config in config.get("datasets", {}).items():
        try:
            rows = source.fetch_dataset(dataset_name, dataset_config, context)
            dataset_path = batch_dir / f"{dataset_name}.csv"
            write_csv(dataset_path, rows, columns=dataset_config.get("columns"))
            row_counts[dataset_name] = len(rows)
            dataset_files[dataset_name] = str(dataset_path)
            storage_files[dataset_name] = describe_file(dataset_path)
        except Exception as exc:  # noqa: BLE001 - manifest should capture source failures.
            row_counts[dataset_name] = 0
            exceptions.append(
                {
                    "dataset": dataset_name,
                    "message": sanitize_error(str(exc)),
                }
            )

    finished_at = _coerce_datetime(now, ZoneInfo(context["timezone"])) if now else datetime.now(ZoneInfo(context["timezone"]))
    manifest = {
        "source": config.get("source", "autocount_ac2"),
        "job": config.get("job", "daily_stock_extract"),
        "run_id": context["run_id"],
        "business_date": context["business_date"],
        "status": "failed" if exceptions else "success",
        "storage_batch_id": context["storage_batch_id"],
        "row_counts": row_counts,
        "exception_count": len(exceptions),
        "started_at": context["run_started_at"],
        "finished_at": finished_at.isoformat(),
        "extract_window": {
            "movement_from": context["movement_from"],
            "movement_to": context["movement_to"],
            "snapshot_as_at": context["snapshot_as_at"],
            "timezone": context["timezone"],
            "overlap_days": context["overlap_days"],
        },
        "storage": {
            "archive_path": str(batch_dir),
            "dataset_files": dataset_files,
            "files": storage_files,
        },
    }
    if exceptions:
        manifest["exceptions"] = exceptions

    manifest_path = batch_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    active_notifier = notifier or create_notifier(config)
    if active_notifier:
        try:
            active_notifier.send(manifest)
        except Exception as exc:  # noqa: BLE001 - notification must not invalidate archived data.
            manifest["notification_error"] = sanitize_error(str(exc))
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    return manifest


def write_csv(path, rows, columns=None):
    rows = list(rows)
    fieldnames = list(columns) if columns else infer_columns(rows)
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not fieldnames:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def describe_file(path):
    return {
        "path": str(path),
        "byte_size": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def infer_columns(rows):
    columns = []
    for row in rows:
        for key in row.keys():
            if key not in columns:
                columns.append(key)
    return columns


class SqlServerSource:
    def __init__(self, connection_config):
        self.connection_config = connection_config

    def fetch_dataset(self, dataset_name, dataset_config, context):
        query = dataset_config.get("query")
        if not query:
            raise ValueError(f"Dataset {dataset_name} has no query configured")

        connection_string = resolve_connection_string(self.connection_config)
        parameters = resolve_query_parameters(dataset_config.get("parameters", []), context)

        try:
            import pyodbc  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install pyodbc on the AutoCount VM before using sqlserver source mode") from exc

        with pyodbc.connect(connection_string, autocommit=True) as connection:
            cursor = connection.cursor()
            cursor.execute(query, parameters)
            columns = [column[0] for column in cursor.description or []]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]


class SampleSource:
    def __init__(self, rows_by_dataset):
        self.rows_by_dataset = rows_by_dataset

    def fetch_dataset(self, dataset_name, dataset_config, context):
        return list(self.rows_by_dataset.get(dataset_name, []))


class JsonWebhookNotifier:
    def __init__(self, webhook_url, timeout_seconds=15):
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds

    def send(self, payload):
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            response.read()


def create_source(config):
    connection_config = config.get("source_connection", {})
    kind = connection_config.get("kind", "sqlserver")
    if kind == "sqlserver":
        return SqlServerSource(connection_config)
    if kind == "sample":
        return SampleSource(config.get("sample_rows", {}))
    raise ValueError(f"Unsupported source_connection.kind: {kind}")


def create_notifier(config):
    notification = config.get("notification", {})
    webhook_url = notification.get("webhook_url")
    if not webhook_url:
        return None
    return JsonWebhookNotifier(
        webhook_url=webhook_url,
        timeout_seconds=int(notification.get("timeout_seconds", 15)),
    )


def resolve_connection_string(connection_config):
    env_name = connection_config.get("connection_string_env")
    if env_name:
        value = os.environ.get(env_name)
        if not value:
            raise RuntimeError(f"Environment variable {env_name} is not set")
        return value

    value = connection_config.get("connection_string")
    if value:
        return value

    raise RuntimeError("Configure source_connection.connection_string_env for the read-only SQL login")


def resolve_query_parameters(parameter_names, context):
    return [_coerce_query_parameter(context[name]) for name in parameter_names]


def sanitize_error(message):
    redacted = re.sub(r"(?i)(password|pwd)\s*=\s*[^;]+", r"\1=<redacted>", message)
    redacted = re.sub(r"(?i)(token|apikey|api_key)=([^;&\s]+)", r"\1=<redacted>", redacted)
    return redacted


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _coerce_datetime(value, tz):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def _coerce_query_parameter(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        if parsed.tzinfo:
            return parsed.replace(tzinfo=None)
        return parsed
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract read-only AutoCount stock data into an archive batch.")
    parser.add_argument("--config", required=True, help="Path to local JSON config. Do not commit production config.")
    parser.add_argument("--business-date", help="Business date to extract, YYYY-MM-DD. Defaults to yesterday.")
    parser.add_argument("--dry-run", action="store_true", help="Validate config and print the planned extraction window.")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.dry_run:
        context = build_run_context(
            business_date=args.business_date,
            overlap_days=int(config.get("overlap_days", DEFAULT_OVERLAP_DAYS)),
            timezone_name=config.get("timezone", DEFAULT_TIMEZONE),
        )
        print(json.dumps(context, indent=2, sort_keys=True))
        return 0

    manifest = run_extraction(config, business_date=args.business_date)
    print(json.dumps({"status": manifest["status"], "storage_batch_id": manifest["storage_batch_id"]}, indent=2))
    return 1 if manifest["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
