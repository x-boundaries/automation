# Member Browse Extract Review Runbook

This is a read-only local review script for extracting the current AutoCount 2 member browse table for migration reconciliation. It uses the proven AutoCount 2 session path, creates `AutoCount.BonusPoint.Member.MemberCommand` with the public `Create` factory, and calls `LoadBrowseTable` after authentication succeeds.

It does not create, update, delete, or directly query members. It does not run SQL. It does not use DBSetting data-write methods.

## Output Contains PII

Output contains PII and operational member data. Keep it local on the AutoCount machine or approved secure storage only.

Generated files must not be committed, attached to PRs, pasted into chat, or captured in screenshots. Share only sanitized status fields unless a separate secure review process is approved.

## Output Location

Recommended local folder:

`C:\XB\autocount_outputs\review\member_browse_extract`

The script creates at least:

- `ac2_member_browse_extract.csv`
- `ac2_member_browse_extract_summary.json`

It also writes a local `PRIVATE_DO_NOT_COMMIT_MEMBER_BROWSE_EXTRACT.txt` warning marker in the same folder.

## Runtime-Only Credentials

Script path: `scripts/ac2_member_browse_extract_review.ps1`

The AutoCount password must be supplied only through the `AC2_PROBE_PASSWORD` environment variable for the current PowerShell process. Do not pass the password as a command argument and do not store it in Git.

## Example Command

Run this locally on the AC2 machine. Replace values only in your private shell session.

```powershell
$env:AC2_PROBE_PASSWORD = "ADMIN"
.\scripts\ac2_member_browse_extract_review.ps1 `
  -DllRoot "C:\Program Files\AutoCount\Accounting 2.2" `
  -ServerName "localhost\A2006" `
  -DatabaseName "AED_XBOUNDARIES" `
  -UserId "ADMIN" `
  -AllowRootLogin `
  -EnableMemberBrowseExtractReview `
  -OutputDirectory "C:\XB\autocount_outputs\review\member_browse_extract"
Remove-Item Env:\AC2_PROBE_PASSWORD
```

The `-EnableMemberBrowseExtractReview` switch is mandatory. Without it, the script refuses to run before loading AutoCount assemblies or attempting API calls.

Use `-MaxRows 100` for a limited local review extract. The default `-MaxRows 0` exports all rows returned by `LoadBrowseTable`.

## Sanitized Console Output

The console JSON is intended to be safe to share after review. It contains only:

- `status`
- `authentication_success`
- `user_session_available`
- `member_command_found`
- `member_command_create_found`
- `load_browse_table_found`
- `load_browse_table_success`
- `row_count`
- `column_names`
- `output_file_paths`
- `warning_count`
- `error`

Do not share the CSV or raw summary JSON until a reviewer confirms the file contains no unexpected sensitive fields.

## Expected Columns

When present, these columns are placed first in the CSV for reconciliation review:

- `MemberNo`
- `MemberType`
- `Name`
- `MobilePhone`
- `EmailAddress`
- `DOB`
- `IsActive`
- `RegisterDate`
- `ExpiryDate`
- `Note`
- `OpeningPoints`
- `Individual`
- `CreatedTime`
- `LastModified`

Additional columns returned by AutoCount are kept after the preferred columns because reconciliation may require them.

## Boundary

Read-only member browse extract only.

Allowed:

- Load `AutoCount.dll` and `AutoCount.Invoicing.dll` from `-DllRoot`.
- Create `DBSetting` with `CreateAutoCountDefaultDBSetting(serverName, dbName)`.
- Authenticate and create a current `UserSession`.
- Set `AllowRootLogin` only when the switch is supplied.
- Create `MemberCommand` with `MemberCommand.Create(session, dbSetting)`.
- Call `LoadBrowseTable`.
- Export local private review files under `-OutputDirectory`.

Blocked:

- No member saves, deletes, new member entities, or member lookup calls.
- No member entity save calls.
- No direct SQL.
- No DBSetting data writes.
- No generated output in Git.
