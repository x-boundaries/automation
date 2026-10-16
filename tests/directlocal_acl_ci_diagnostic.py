"""#155 hosted-CI ACL diagnostic (Windows only; never fails the job).

Prints one GitHub ``::notice`` annotation with sanitised classes for a freshly
prepared DirectLocal state root and its disposable parent: whether the process
is elevated, whether the account is the built-in Administrator (RID 500), the
verdict ``prepare_state_root`` reached, and per-path DACL flags, owner class
and ACE type / inheritance flags / principal class. No SID, account name or
path is printed. Read-only apart from creating and removing one temporary
directory.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "directlocal"))

import xb_directlocal as dl  # noqa: E402


def main() -> int:
    if os.name != "nt":
        print("::notice title=xb155-acl-diagnostic::not windows")
        return 0
    evidence: dict = {"python": sys.version.split()[0]}
    holder = Path(tempfile.mkdtemp(prefix="xb155-acl-diag-"))
    try:
        user = dl._win_current_user_sid()
        evidence["elevated"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        evidence["user_is_rid500"] = user.endswith("-500")
        evidence["parent"] = dl.sddl_custody_classes(dl._win_sddl(holder), user_sid=user, resolve=dl._win_resolve_sid)
        root = holder / "state"
        try:
            dl.prepare_state_root(root, environ={})
            evidence["prepare"] = "private"
        except dl.DirectLocalError as exc:
            evidence["prepare"] = exc.code
        evidence["paths"] = dl.describe_private_state(root) if root.exists() else []
    except Exception as exc:  # noqa: BLE001 - diagnostic only; report the class, never fail
        evidence["diagnostic_error"] = type(exc).__name__
    finally:
        shutil.rmtree(holder, ignore_errors=True)
    text = json.dumps(evidence, separators=(",", ":"))
    if "S-1-5-21" in text:
        text = json.dumps({"diagnostic_error": "sanitisation_refused"})
    print(f"::notice title=xb155-acl-diagnostic::{text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
