# X-Boundaries Automation

This repository tracks internal automation work, migration prep tasks, and post-migration automation roadmap progress for X-Boundaries digital transformation and AutoCount migration. It features a lightweight GitHub-powered work tracker and an automated dashboard.

## ⚠️ WARNING: DATA POLICY

**Do NOT commit real company operational data to this repository.**

This repository must NOT store:
- Real stock master or product master files.
- Real customer or supplier lists.
- AP/AR records or invoices.
- Bank or payment exports.
- AutoCount database backups.
- Passwords, API keys, or `.env` files.

Please review the full data policy in [docs/data_policy.md](docs/data_policy.md).

## How to Update the Tracker

1. Edit the CSV file at `tracker/work_tracker.csv`.
2. Update task details, ensuring you use the allowed Status values: `Not Started`, `In Progress`, `Blocked`, `Done`, `Parked`.
3. Allowed Priority values: `High`, `Medium`, `Low`.
4. Commit and push your changes to GitHub.

For more details, see [docs/tracker_usage.md](docs/tracker_usage.md).

## How the Dashboard is Generated

The dashboard is generated automatically using a GitHub Actions workflow whenever changes are pushed to the tracker files or the generation script.

- The Python script `scripts/build_dashboard.py` reads the tracker CSVs and outputs a Markdown dashboard.
- The GitHub Actions workflow (`.github/workflows/build-dashboard.yml`) runs the script on every push to the `tracker/` directory or relevant files.
- The workflow automatically commits the updated `dashboard/README.md` back to the repository.
- You can view the live dashboard at [dashboard/README.md](dashboard/README.md).
