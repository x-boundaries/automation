# Tracker Usage Guide

This project features a lightweight GitHub-powered work tracker that automatically generates a dashboard.

## How to Update the Tracker

1. Edit the file `tracker/work_tracker.csv`.
2. To add a new task, append a new row following the CSV format.
3. Update the `Status` and `Priority` columns as needed.

### Allowed Status Values
You must use one of the following exact status values:
- `Not Started`
- `In Progress`
- `Blocked`
- `Done`
- `Parked`

### Allowed Priority Values
You must use one of the following exact priority values:
- `High`
- `Medium`
- `Low`

## Triggering the Dashboard Build

Once you commit and push your changes to GitHub, a GitHub Actions workflow is triggered automatically. This workflow runs the `scripts/build_dashboard.py` script and commits the newly generated `dashboard/README.md` file back to the repository.

You can view the updated dashboard at [dashboard/README.md](../dashboard/README.md).
