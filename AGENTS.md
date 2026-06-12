## GitHub PR Auth Rule

When creating, updating, commenting on, or closing GitHub pull requests/issues,
always use the local GitHub CLI (`gh`) from the shell.

Do not use any GitHub connector/app/MCP tool for PR or issue actions unless the
user explicitly asks for the connector.

Before creating or updating a PR, run:

```powershell
gh auth status
gh api user --jq .login
```

Verify the active local `gh` account is the intended GitHub user. If it is not,
stop and report which account is active.

Use local git for commits and pushes, and local `gh` for PR
creation/updates/comments.

If `git push` or `gh` commands fail with an invalid or expired token error,
clear injected token overrides for the current PowerShell command so `gh`/git
can fall back to the local credential manager:

```powershell
$env:GITHUB_TOKEN="";
$env:GH_TOKEN="";
```

Then retry the `gh` command.

## GitHub Token Override Recovery

Environment variables override `gh`'s stored keyring authentication. If
`gh auth status` appears logged in but `gh api user --jq .login`, `gh pr view`,
or `gh pr edit` returns `401 Requires authentication`, suspect a bad
`GH_TOKEN`/`GITHUB_TOKEN` override or a credential-context mismatch.

Do not print token values. It is safe to report only whether token variables are
set and their lengths.

For a one-off Codex/tool-runner fix, prefer setting only `GH_TOKEN` to a valid
token. After the operation, clear process and user-level overrides:

```powershell
[Environment]::SetEnvironmentVariable("GH_TOKEN", $null, "User")
[Environment]::SetEnvironmentVariable("GITHUB_TOKEN", $null, "User")
Remove-Item Env:\GH_TOKEN -ErrorAction SilentlyContinue
Remove-Item Env:\GITHUB_TOKEN -ErrorAction SilentlyContinue
```

If clearing overrides still leaves `gh` unauthenticated, re-run `gh auth login`
from the same terminal/user context that the automation shell uses.
