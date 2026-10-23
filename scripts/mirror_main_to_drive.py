#!/usr/bin/env python3
r"""Read-only weekly mirror of this repository's ``main`` branch into a Drive folder (#273).

What it does, in order:

1. Loads the local settings file (``config/mirror_main_to_drive.local.json``, gitignored).
2. Checks the target folder guard: the folder must already exist and hold the operator-made
   marker file ``.xb-mirror-target``; it must not be a drive root, a Drive root folder ("My Drive",
   "Shared drives", a shared drive, "Other computers"), a user profile root, a link, inside a git
   checkout, or contain a ``.git`` entry. Paths are compared both as written and fully resolved
   (junctions, symlinks, case); network and ``\\?\`` device paths are refused. A folder that was
   never mirrored into must hold nothing but the marker. Nothing is written to the target when
   the guard refuses.
3. Fetches ``main`` read-only into a local bare cache clone (no credentials, no push, never a
   working clone). The source must be this repository's https address; git runs with system and
   global config ignored, so a PC-wide ``insteadOf`` or header cannot redirect it.
4. If the fetched commit equals the commit in the target's ``_MIRROR_INFO.txt``, it exits 0 and
   changes nothing in the target.
5. Otherwise it exports the commit with ``git archive`` (no ``.git``, honours export-ignore) into a
   staging folder next to the cache, re-runs the whole target guard (marker, identity), marks the
   run ``running`` in the target, then syncs staging into the target: adds and replaces changed
   files, deletes files that are no longer in ``main`` (re-checking the marker and folder
   identity before every delete), removes links without following them, never deletes the
   marker, and never touches anything outside the target. Internal temp files are always created
   new, never written through an existing file or link.
6. Writes ``last_run_status.json`` (log folder, and the target when the target was updated or a
   run failed after the guard passed), then ``_MIRROR_INFO.txt`` last.

A run marks the log status ``running`` as soon as it holds the lock, so a run that was killed
(time limit, restart) stays visible: the next run reports the left-over lock, and ``--status``
prints a read-only summary of the last run, the lock and the Drive info file.

Status, log lines and console output carry fixed generic messages, a failure class and a reason
code only: no exception text, no local paths and no secrets.

Exit codes: 0 updated or no change; 1 unexpected error; 2 settings invalid; 3 target guard
refused; 4 fetch failed; 5 export failed; 6 sync failed; 7 another run holds the lock.
``--status``: 0 when the last run finished well (or a run is going now), 1 otherwise, 2 settings
invalid.

Python standard library only. Requires ``git`` on PATH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_REPO_URL = "https://github.com/x-boundaries/automation.git"
# The only accepted sources (compared lower-case): this public repository over https.
ALLOWED_REPO_URLS = (DEFAULT_REPO_URL, "https://github.com/x-boundaries/automation")
# Offline tests only: lets a local throwaway repository stand in for GitHub. Never read from the
# settings file, the command line or the environment.
_ALLOW_LOCAL_SOURCE_FOR_TESTS = False
ALLOWED_BRANCH = "main"
SETTINGS_KEYS = {"repo_url", "branch", "cache_dir", "target_dir", "log_dir"}
REQUIRED_PATH_KEYS = ("cache_dir", "target_dir", "log_dir")

MARKER_NAME = ".xb-mirror-target"
INFO_NAME = "_MIRROR_INFO.txt"
STATUS_NAME = "last_run_status.json"
# Names at the target root that the sync never treats as mirrored content.
RESERVED_ROOT_NAMES = (MARKER_NAME, INFO_NAME, STATUS_NAME)
LOG_NAME = "mirror_main_to_drive.log"
STATUS_SCHEMA = "xb_mirror_main_to_drive_status/v1"
INFO_NOTICE = "Read-only copy of GitHub main. Do not edit here: changes are overwritten."

CACHE_REPO_NAME = "repo.git"
MANIFEST_NAME = "last_sync_manifest.json"
ADOPTED_NAME = "adopted_target.json"  # in the cache: the target this cache has started mirroring into
LOCK_NAME = "mirror.lock"
LOCK_STALE_SECONDS = 2 * 60 * 60  # the scheduled task is capped at 30 minutes
RUN_CAP_SECONDS = 35 * 60  # a lock older than this outlived the task's 30-minute limit
# Google Drive for desktop root folders (compared case-folded). A target with one of these names,
# or directly inside "Shared drives" / "Other computers", is refused. English names only.
DRIVE_ROOT_FOLDER_NAMES = {"my drive", "shared drives", "other computers"}
DRIVE_CONTAINER_NAMES = {"shared drives", "other computers"}
STAGING_SUFFIX = ".mirror-staging"
STAGING_MARKER_NAME = ".xb-mirror-staging"
TEMP_SUFFIX = ".xbmirror-tmp"

SGT = timezone(timedelta(hours=8))
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # Windows directory junction
SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
GIT_TIMEOUT_SECONDS = 600

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_CONFIG = 2
EXIT_TARGET_GUARD = 3
EXIT_FETCH = 4
EXIT_EXPORT = 5
EXIT_SYNC = 6
EXIT_LOCKED = 7

# Fixed, generic messages per failure class. Nothing run-specific is ever appended.
MESSAGES = {
    None: "Mirror run finished.",
    "CONFIG_INVALID": "Settings file is missing or invalid. See the runbook, section Settings.",
    "TARGET_GUARD": "Target folder refused by the safety guard. Nothing was changed.",
    "LOCKED": ("Another mirror run is in progress, or an earlier run was stopped before it "
               "finished. This run changed nothing. See the runbook, section Reading the status."),
    "FETCH_FAILED": "Could not fetch main from GitHub. Nothing was changed in the target.",
    "EXPORT_FAILED": "Could not export main. Nothing was changed in the target.",
    "SYNC_FAILED": "Copy into the target folder failed part way. The next run retries.",
    "UNEXPECTED": "Unexpected error. See the runbook, section Reading the status.",
}
EXIT_CODES = {
    None: EXIT_OK,
    "CONFIG_INVALID": EXIT_CONFIG,
    "TARGET_GUARD": EXIT_TARGET_GUARD,
    "LOCKED": EXIT_LOCKED,
    "FETCH_FAILED": EXIT_FETCH,
    "EXPORT_FAILED": EXIT_EXPORT,
    "SYNC_FAILED": EXIT_SYNC,
    "UNEXPECTED": EXIT_UNEXPECTED,
}
RUNNING_MESSAGE = ("Mirror run in progress. If this is still shown after the run should have "
                   "ended, the run was stopped part way. See the runbook, section Reading the status.")


class MirrorError(Exception):
    """A classified failure. ``reason`` is a short fixed code, never run data."""

    def __init__(self, failure_class: str, reason: str, details=None):
        super().__init__(reason)
        self.failure_class = failure_class
        self.reason = reason
        self.details = details or {}  # fixed keys with numbers only, never paths or text


# --------------------------------------------------------------------------- helpers


def now_sgt() -> str:
    return datetime.now(SGT).replace(microsecond=0).isoformat()


def norm(path) -> str:
    """Comparable absolute form (case-folded on Windows)."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def strip_device_prefix(path: str) -> str:
    r"""Drop a ``\\?\`` (or ``\\?\UNC\``) prefix that ``realpath`` may return on Windows."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[len("\\\\?\\UNC\\"):]
    if path.startswith("\\\\?\\"):
        return path[len("\\\\?\\"):]
    return path


def real(path) -> str:
    """Comparable resolved form: junctions and symlinks resolved, case-folded on Windows."""
    absolute = os.path.abspath(os.fspath(path))
    try:
        resolved = os.path.realpath(absolute)
    except (OSError, ValueError):
        resolved = absolute
    return os.path.normcase(strip_device_prefix(resolved))


def forms(path) -> set:
    """The written and the resolved form. Guards refuse when either form trips them."""
    return {norm(path), real(path)}


def is_unc_or_device(value: str) -> bool:
    r"""``\\server\share``, ``\\?\...`` and ``\\.\...`` (either slash) are never accepted."""
    return len(value) >= 2 and value[0] in "\\/" and value[1] in "\\/"


def is_within(child, parent) -> bool:
    """True when ``child`` is ``parent`` or lies under it (lexical, both normalised)."""
    c, p = norm(child), norm(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives
        return False


def overlaps(first, second) -> bool:
    """True when either folder is, or is inside, the other, written or resolved."""
    return any(is_within(a, b) or is_within(b, a) for a in forms(first) for b in forms(second))


def is_link_stat(st: os.stat_result) -> bool:
    """Symlink or Windows junction. Other reparse points (cloud files) are not links."""
    if stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) == IO_REPARSE_TAG_MOUNT_POINT


def lstat_or_none(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def open_exclusive_temp(path: str):
    """Open a brand-new file at ``path`` for binary writing.

    Whatever already sits at that name (a stale temp file, or a hard link, symlink or junction
    planted there) is unlinked first and never written through; the file is then created with
    O_CREAT|O_EXCL, so a name re-created in between makes the open fail instead of following it.
    """
    st = lstat_or_none(path)
    if st is not None:
        if is_link_stat(st):
            remove_link(path)
        elif stat.S_ISDIR(st.st_mode):
            raise IsADirectoryError("temp name is a folder")
        else:
            os.unlink(path)  # a hard link loses only this name; the other file is untouched
    flags = (os.O_CREAT | os.O_EXCL | os.O_WRONLY
             | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    return os.fdopen(os.open(path, flags, 0o666), "wb")


def write_text_atomic(path, text: str) -> None:
    tmp = os.fspath(path) + TEMP_SUFFIX
    with open_exclusive_temp(tmp) as handle:
        handle.write(text.encode("utf-8"))
    os.replace(tmp, path)


def copy_file_into(source: str, destination: str) -> None:
    """Copy via a fresh temp file next to ``destination``, then rename over it."""
    temporary = destination + TEMP_SUFFIX
    with open(source, "rb") as src, open_exclusive_temp(temporary) as out:
        shutil.copyfileobj(src, out, 1024 * 1024)
    st = lstat_or_none(destination)
    if (st is not None and stat.S_ISREG(st.st_mode) and not is_link_stat(st)
            and not st.st_mode & stat.S_IWRITE and st.st_nlink <= 1):
        os.chmod(destination, stat.S_IWRITE)  # a read-only mirrored file must not block updates
    os.replace(temporary, destination)


def remove_link(path: str) -> None:
    """Remove a symlink or junction itself, never its target."""
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)


def remove_tree(path: str, root: str) -> int:
    """Delete ``path`` (file, folder or link) without following links. Returns entries removed.

    Refuses anything that is not strictly inside ``root``.
    """
    if not is_within(path, root) or norm(path) == norm(root):
        raise MirrorError("SYNC_FAILED", "delete_outside_target")
    st = lstat_or_none(path)
    if st is None:
        return 0
    if is_link_stat(st):
        remove_link(path)
        return 1
    if stat.S_ISDIR(st.st_mode):
        removed = 0
        with os.scandir(path) as entries:
            children = [entry.path for entry in entries]
        for child in children:
            removed += remove_tree(child, root)
        os.rmdir(path)
        return removed + 1
    try:
        os.unlink(path)
    except PermissionError:
        if st.st_nlink > 1:
            raise  # never change the attributes of a file that also lives elsewhere
        os.chmod(path, stat.S_IWRITE)  # clear a read-only attribute, then retry once
        os.unlink(path)
    return 1


# --------------------------------------------------------------------------- settings


def load_settings(config_path: str) -> dict:
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        raise MirrorError("CONFIG_INVALID", "settings_missing") from None
    except (OSError, ValueError):
        raise MirrorError("CONFIG_INVALID", "settings_unreadable") from None
    if not isinstance(raw, dict):
        raise MirrorError("CONFIG_INVALID", "settings_not_object")
    unknown = [key for key in raw if key not in SETTINGS_KEYS and not str(key).startswith("_")]
    if unknown:
        raise MirrorError("CONFIG_INVALID", "unknown_setting")

    settings = {}
    for key in REQUIRED_PATH_KEYS:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise MirrorError("CONFIG_INVALID", "missing_" + key)
        if is_unc_or_device(value):
            raise MirrorError("CONFIG_INVALID", "unc_or_device_" + key)
        if not os.path.isabs(value):
            raise MirrorError("CONFIG_INVALID", "relative_" + key)
        settings[key] = os.path.abspath(value)

    branch = raw.get("branch", ALLOWED_BRANCH)
    if branch != ALLOWED_BRANCH:
        raise MirrorError("CONFIG_INVALID", "branch_not_main")
    settings["branch"] = branch

    repo_url = raw.get("repo_url", DEFAULT_REPO_URL)
    if not isinstance(repo_url, str) or not repo_url or repo_url.startswith("-"):
        raise MirrorError("CONFIG_INVALID", "repo_url_invalid")
    parts = urlsplit(repo_url)
    if parts.scheme == "https":
        if "@" in parts.netloc or parts.username or parts.password:
            raise MirrorError("CONFIG_INVALID", "repo_url_has_credentials")
        if parts.query or parts.fragment:
            raise MirrorError("CONFIG_INVALID", "repo_url_invalid")
        if repo_url.lower() not in ALLOWED_REPO_URLS:
            raise MirrorError("CONFIG_INVALID", "repo_url_not_this_repo")
    elif _ALLOW_LOCAL_SOURCE_FOR_TESTS and os.path.isabs(repo_url) and os.path.isdir(repo_url):
        pass  # offline tests only
    elif os.path.isabs(repo_url) or is_unc_or_device(repo_url):
        raise MirrorError("CONFIG_INVALID", "repo_url_local_path")
    else:
        raise MirrorError("CONFIG_INVALID", "repo_url_not_https")
    settings["repo_url"] = repo_url

    staging = os.path.join(
        os.path.dirname(settings["cache_dir"]),
        os.path.basename(settings["cache_dir"]) + STAGING_SUFFIX,
    )
    settings["staging_dir"] = staging
    return settings


def check_work_dirs(settings: dict) -> None:
    """The local cache, staging and log folders must not overlap the target in either direction."""
    target = settings["target_dir"]
    for work in (settings["cache_dir"], settings["log_dir"], settings["staging_dir"]):
        if overlaps(work, target):
            raise MirrorError("TARGET_GUARD", "work_dir_overlaps_target")


def reporting_log_dir(config_path: str, settings):
    """Log folder usable for reporting a failure before the checks passed, or None.

    Never a folder inside (or containing) the configured target, so a refused run cannot
    create anything in the Drive folder.
    """
    if settings is not None:
        log_dir, target = settings["log_dir"], settings["target_dir"]
    else:
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        log_dir, target = raw.get("log_dir"), raw.get("target_dir")
        if not (isinstance(log_dir, str) and os.path.isabs(log_dir)) or is_unc_or_device(log_dir):
            return None
        log_dir = os.path.abspath(log_dir)
    if isinstance(target, str) and target and overlaps(log_dir, target):
        return None
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        return None
    return log_dir


# --------------------------------------------------------------------------- target guard


def profile_roots() -> tuple[set, set]:
    """(exact profile-root paths, parents whose direct children are profile roots)."""
    exact, parents = set(), set()
    candidates = [os.path.expanduser("~"), os.environ.get("USERPROFILE"), os.environ.get("HOME")]
    for value in candidates:
        if value and os.path.isabs(value):
            for form in forms(value):
                exact.add(form)
                parents.add(os.path.dirname(form))
    return exact, parents


def check_target(target: str) -> None:
    """Raise MirrorError(TARGET_GUARD, reason) unless ``target`` is a marked mirror folder.

    Every path rule is checked on the written and on the fully resolved form.
    """
    absolute = os.path.abspath(target)
    if is_unc_or_device(absolute):
        raise MirrorError("TARGET_GUARD", "unc_or_device_path")
    candidates = forms(absolute)
    for form in candidates:
        if os.path.dirname(form) == form or norm(Path(form).anchor) == form:
            raise MirrorError("TARGET_GUARD", "drive_root")
    for form in candidates:
        name = os.path.basename(form).casefold()
        parent_name = os.path.basename(os.path.dirname(form)).casefold()
        if name in DRIVE_ROOT_FOLDER_NAMES or parent_name in DRIVE_CONTAINER_NAMES:
            raise MirrorError("TARGET_GUARD", "drive_root_folder")
    exact, parents = profile_roots()
    for form in candidates:
        if form in exact or form in parents or os.path.dirname(form) in parents:
            raise MirrorError("TARGET_GUARD", "profile_root")
    st = lstat_or_none(absolute)
    if st is None:
        raise MirrorError("TARGET_GUARD", "target_missing")
    if is_link_stat(st):
        raise MirrorError("TARGET_GUARD", "target_is_link")
    if not stat.S_ISDIR(st.st_mode):
        raise MirrorError("TARGET_GUARD", "target_not_folder")
    with os.scandir(absolute) as entries:
        names = {entry.name: entry for entry in entries}
    if any(name.casefold() == ".git" for name in names):
        raise MirrorError("TARGET_GUARD", "target_contains_git")
    marker = names.get(MARKER_NAME)
    if marker is None:
        raise MirrorError("TARGET_GUARD", "marker_missing")
    marker_st = os.lstat(marker.path)
    if is_link_stat(marker_st) or not stat.S_ISREG(marker_st.st_mode):
        raise MirrorError("TARGET_GUARD", "marker_not_file")
    for form in candidates:
        ancestor = os.path.dirname(form)
        while True:
            if lstat_or_none(os.path.join(ancestor, ".git")) is not None:
                raise MirrorError("TARGET_GUARD", "target_inside_git_checkout")
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                break
            ancestor = parent


def read_adopted(settings: dict):
    """The resolved target this cache has started mirroring into, or None."""
    try:
        with open(os.path.join(settings["cache_dir"], ADOPTED_NAME), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data.get("target") if isinstance(data, dict) else None


def save_adopted(settings: dict) -> None:
    write_text_atomic(Path(settings["cache_dir"], ADOPTED_NAME),
                      json.dumps({"target": real(settings["target_dir"])}))


def check_first_run(settings: dict) -> None:
    """A folder this mirror has never written into must hold only the marker (and a status file).

    A folder counts as already mirrored when it holds ``_MIRROR_INFO.txt``, or when this cache
    recorded it before its first copy started (so an interrupted copy can be finished).
    """
    target = settings["target_dir"]
    info_st = lstat_or_none(os.path.join(target, INFO_NAME))
    if info_st is not None and stat.S_ISREG(info_st.st_mode) and not is_link_stat(info_st):
        return
    if read_adopted(settings) == real(target):
        return
    allowed = {MARKER_NAME.casefold(), STATUS_NAME.casefold()}
    with os.scandir(target) as entries:
        if any(entry.name.casefold() not in allowed for entry in entries):
            raise MirrorError("TARGET_GUARD", "first_run_target_not_empty")


def target_identity(target: str) -> tuple:
    st = os.lstat(target)
    return (real(target), st.st_dev, st.st_ino)


def guard_target(settings: dict) -> tuple:
    """All target checks. Returns the folder identity for the later re-checks."""
    check_target(settings["target_dir"])
    check_work_dirs(settings)
    check_first_run(settings)
    return target_identity(settings["target_dir"])


def make_confirm(target: str, identity: tuple):
    """A cheap check run before every delete: same folder, marker still a plain file."""
    def confirm() -> None:
        try:
            st = os.lstat(target)
            marker_st = os.lstat(os.path.join(target, MARKER_NAME))
        except OSError:
            raise MirrorError("TARGET_GUARD", "marker_missing") from None
        if is_link_stat(st) or (st.st_dev, st.st_ino) != identity[1:]:
            raise MirrorError("TARGET_GUARD", "target_changed")
        if is_link_stat(marker_st) or not stat.S_ISREG(marker_st.st_mode):
            raise MirrorError("TARGET_GUARD", "marker_missing")
    return confirm


# --------------------------------------------------------------------------- git


def git_env() -> dict:
    """The PC's environment without any GIT_* variable, plus a config-free git setup.

    System and global (user) git config are ignored, so a PC-wide ``url.*.insteadOf``,
    ``http.extraHeader`` or credential setting cannot redirect or authenticate the fetch.
    """
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    return env


def git_base_command() -> list:
    return [
        "git",
        "-c", "credential.helper=",
        "-c", "core.askPass=",
        "-c", "http.extraHeader=",
        "-c", "protocol.allow=never",
        "-c", "protocol.https.allow=always",
        "-c", "protocol.file.allow=" + ("always" if _ALLOW_LOCAL_SOURCE_FOR_TESTS else "never"),
    ]


def git(args, *, failure_class: str, reason: str, git_dir=None, stdout=subprocess.PIPE):
    command = git_base_command()
    if git_dir is not None:
        command += ["--git-dir", git_dir]
    command += list(args)
    try:
        result = subprocess.run(
            command,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=git_env(),
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise MirrorError(failure_class, reason) from None
    if result.returncode != 0:
        raise MirrorError(failure_class, reason)
    return result.stdout.decode("utf-8", "replace").strip() if result.stdout is not None else ""


def fetch_main(settings: dict) -> tuple[str, str]:
    """Fetch ``main`` into the bare cache. Returns (commit sha, commit date ISO)."""
    cache_repo = os.path.join(settings["cache_dir"], CACHE_REPO_NAME)
    if lstat_or_none(cache_repo) is None:
        git(["init", "--bare", "--quiet", cache_repo], failure_class="FETCH_FAILED", reason="cache_init_failed")
    bare = git(["rev-parse", "--is-bare-repository"], git_dir=cache_repo,
               failure_class="FETCH_FAILED", reason="cache_not_bare")
    if bare != "true":
        raise MirrorError("FETCH_FAILED", "cache_not_bare")
    refspec = "+refs/heads/%s:refs/remotes/origin/%s" % (settings["branch"], settings["branch"])
    git(["fetch", "--no-tags", "--quiet", settings["repo_url"], refspec], git_dir=cache_repo,
        failure_class="FETCH_FAILED", reason="fetch_failed")
    sha = git(["rev-parse", "--verify", "refs/remotes/origin/%s^{commit}" % settings["branch"]],
              git_dir=cache_repo, failure_class="FETCH_FAILED", reason="commit_unresolved")
    if not SHA_RE.match(sha):
        raise MirrorError("FETCH_FAILED", "commit_unresolved")
    date = git(["log", "-1", "--format=%cI", sha], git_dir=cache_repo,
               failure_class="FETCH_FAILED", reason="commit_unresolved")
    return sha, date


# --------------------------------------------------------------------------- export


def safe_parts(name: str) -> list:
    """Validated path parts of an archive member, or MirrorError."""
    if not name or name.startswith(("/", "\\")):
        raise MirrorError("EXPORT_FAILED", "unsafe_archive_path")
    parts = name.rstrip("/").split("/")
    for part in parts:
        if part in ("", ".", "..") or "\\" in part or ":" in part or "\x00" in part:
            raise MirrorError("EXPORT_FAILED", "unsafe_archive_path")
        if part.casefold() == ".git":
            raise MirrorError("EXPORT_FAILED", "git_folder_in_archive")
    if len(parts) == 1 and parts[0].casefold() in {n.casefold() for n in RESERVED_ROOT_NAMES}:
        raise MirrorError("EXPORT_FAILED", "reserved_name_in_archive")
    return parts


def prepare_staging(staging: str) -> None:
    """Empty the staging folder. A pre-existing folder must carry our staging marker."""
    st = lstat_or_none(staging)
    if st is not None:
        if is_link_stat(st) or not stat.S_ISDIR(st.st_mode):
            raise MirrorError("EXPORT_FAILED", "staging_not_folder")
        if lstat_or_none(os.path.join(staging, STAGING_MARKER_NAME)) is None:
            raise MirrorError("EXPORT_FAILED", "staging_not_ours")
        remove_tree(staging, os.path.dirname(staging))
    os.makedirs(staging)
    Path(staging, STAGING_MARKER_NAME).write_text("mirror staging, safe to delete\n", encoding="utf-8")


def export_commit(settings: dict, sha: str) -> tuple[dict, set, int]:
    """``git archive`` the commit into staging.

    Returns ({relative file path: (sha256, size)}, {relative folder paths}, links skipped).
    """
    staging = settings["staging_dir"]
    try:
        prepare_staging(staging)
    except OSError:
        raise MirrorError("EXPORT_FAILED", "staging_failed") from None
    cache_repo = os.path.join(settings["cache_dir"], CACHE_REPO_NAME)
    # core.autocrlf=false: the copy holds the committed bytes, whatever this PC's git setting.
    command = git_base_command() + ["-c", "core.autocrlf=false", "--git-dir", cache_repo,
                                    "archive", "--format=tar", sha]
    files, folders, links_skipped = {}, set(), 0
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, env=git_env())
    except OSError:
        raise MirrorError("EXPORT_FAILED", "archive_failed") from None
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            for member in archive:
                parts = safe_parts(member.name)
                rel = "/".join(parts)
                if member.isdir():
                    folders.add(rel)
                    os.makedirs(os.path.join(staging, *parts), exist_ok=True)
                    continue
                if not member.isreg():
                    links_skipped += 1  # symlinks and other special entries are never mirrored
                    continue
                for depth in range(1, len(parts)):
                    folders.add("/".join(parts[:depth]))
                destination = os.path.join(staging, *parts)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                source = archive.extractfile(member)
                digest = hashlib.sha256()
                size = 0
                with open(destination, "xb") as out:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                        out.write(chunk)
                files[rel] = (digest.hexdigest(), size)
    except MirrorError:
        proc.kill()
        raise
    except (OSError, tarfile.TarError):
        proc.kill()
        raise MirrorError("EXPORT_FAILED", "archive_failed") from None
    finally:
        proc.stdout.close()
        returncode = proc.wait()
    if returncode != 0:
        raise MirrorError("EXPORT_FAILED", "archive_failed")
    if not files:
        raise MirrorError("EXPORT_FAILED", "archive_empty")
    collision = {}
    for rel in list(files) + sorted(folders):
        key = rel.casefold()
        if collision.setdefault(key, rel) != rel:
            raise MirrorError("EXPORT_FAILED", "case_collision_in_archive")
    return files, folders, links_skipped


# --------------------------------------------------------------------------- sync


def load_manifest(settings: dict) -> dict:
    path = os.path.join(settings["cache_dir"], MANIFEST_NAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("target") != real(settings["target_dir"]):
        return {}
    entries = data.get("files")
    return entries if isinstance(entries, dict) else {}


def save_manifest(settings: dict, entries: dict) -> None:
    path = Path(settings["cache_dir"], MANIFEST_NAME)
    write_text_atomic(path, json.dumps({"target": real(settings["target_dir"]), "files": entries}))


def prune_target(target: str, rel_dir: str, files: dict, folders: set, confirm) -> int:
    """Delete everything under the target that is not in the export. Returns entries removed.

    ``confirm()`` runs before every delete and raises when the marker or folder changed.
    """
    removed = 0
    here = os.path.join(target, *rel_dir.split("/")) if rel_dir else target
    with os.scandir(here) as entries:
        children = [(entry.name, entry.path) for entry in entries]
    reserved = {name.casefold() for name in RESERVED_ROOT_NAMES}
    for name, path in children:
        rel = name if not rel_dir else rel_dir + "/" + name
        st = os.lstat(path)
        if is_link_stat(st):
            if not is_within(path, target):
                raise MirrorError("SYNC_FAILED", "delete_outside_target")
            confirm()
            remove_link(path)
            removed += 1
            continue
        if not rel_dir and name.casefold() in reserved:
            if name.casefold() == MARKER_NAME.casefold():
                continue  # the marker is never deleted
            if stat.S_ISREG(st.st_mode):
                continue  # info and status files are rewritten by this run
            confirm()
            removed += remove_tree(path, target)
            continue
        if stat.S_ISDIR(st.st_mode):
            if rel in folders:
                removed += prune_target(target, rel, files, folders, confirm)
            else:
                confirm()
                removed += remove_tree(path, target)
        elif stat.S_ISREG(st.st_mode) and rel in files:
            continue
        else:
            confirm()
            removed += remove_tree(path, target)
    return removed


def ensure_folder(path: str, target: str, confirm) -> None:
    st = lstat_or_none(path)
    if st is not None and (is_link_stat(st) or not stat.S_ISDIR(st.st_mode)):
        confirm()
        remove_tree(path, target)
        st = None
    if st is None:
        os.mkdir(path)


def sync_into_target(settings: dict, files: dict, folders: set, confirm) -> dict:
    target = settings["target_dir"]
    staging = settings["staging_dir"]
    counts = {"files_written": 0, "files_unchanged": 0, "entries_deleted": 0}
    manifest = load_manifest(settings)
    info = os.path.join(target, INFO_NAME)
    confirm()
    if lstat_or_none(info) is not None:
        remove_tree(info, target)  # the info file only ever names a fully copied commit
    counts["entries_deleted"] = prune_target(target, "", files, folders, confirm)
    for rel in sorted(folders):
        ensure_folder(os.path.join(target, *rel.split("/")), target, confirm)
    confirm()
    new_manifest = {}
    for rel in sorted(files):
        digest, size = files[rel]
        destination = os.path.join(target, *rel.split("/"))
        if not is_within(destination, target):
            raise MirrorError("SYNC_FAILED", "write_outside_target")
        st = lstat_or_none(destination)
        previous = manifest.get(rel)
        if (st is not None and stat.S_ISREG(st.st_mode) and not is_link_stat(st)
                and isinstance(previous, list) and len(previous) == 3
                and previous[0] == digest and previous[1] == size == st.st_size
                and previous[2] == st.st_mtime_ns):
            counts["files_unchanged"] += 1
            new_manifest[rel] = previous
            continue
        copy_file_into(os.path.join(staging, *rel.split("/")), destination)
        written = os.lstat(destination)
        new_manifest[rel] = [digest, size, written.st_mtime_ns]
        counts["files_written"] += 1
    save_manifest(settings, new_manifest)
    return counts


# --------------------------------------------------------------------------- status


def read_info_commit(target: str):
    try:
        with open(os.path.join(target, INFO_NAME), "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("commit: "):
                    value = line[len("commit: "):].strip()
                    return value if SHA_RE.match(value) else None
    except (OSError, UnicodeDecodeError):
        return None
    return None


def read_status_file(folder) -> dict:
    """A status record read back, or {} when missing or unreadable."""
    if not folder:
        return {}
    try:
        with open(os.path.join(folder, STATUS_NAME), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def target_status_is_ok(target: str) -> bool:
    return read_status_file(target).get("status") in ("ok", "no_change")


def build_status(status, failure_class, reason, sha=None, commit_date=None, counts=None,
                 error_type=None) -> dict:
    record = {
        "schema": STATUS_SCHEMA,
        "status": status,
        "failure_class": failure_class,
        "reason": reason,
        "message": MESSAGES[failure_class],
        "commit": sha,
        "commit_date": commit_date,
        "time": now_sgt(),
        "exit_code": EXIT_CODES[failure_class],
    }
    if counts:
        record.update(counts)
    if error_type:
        record["error_type"] = error_type
    return record


def build_running(sha=None, commit_date=None) -> dict:
    record = build_status("running", None, "run_started", sha, commit_date)
    record["message"] = RUNNING_MESSAGE
    record["exit_code"] = None
    return record


def write_status(folder, record: dict) -> bool:
    if not folder:
        return False
    try:
        write_text_atomic(Path(folder, STATUS_NAME), json.dumps(record, indent=2) + "\n")
        return True
    except OSError:
        return False


def append_log(log_dir, record: dict) -> None:
    if not log_dir:
        return
    line = "%s status=%s class=%s reason=%s commit=%s exit=%s\n" % (
        record["time"], record["status"], record["failure_class"] or "-",
        record["reason"] or "-", record["commit"] or "-", record["exit_code"])
    try:
        with open(os.path.join(log_dir, LOG_NAME), "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def write_info(target: str, settings: dict, sha: str, commit_date: str) -> None:
    text = "\n".join([
        INFO_NOTICE,
        "",
        "commit: " + sha,
        "commit_date: " + commit_date,
        "copied_at: " + now_sgt(),
        "source: " + settings["repo_url"],
        "branch: " + settings["branch"],
        "",
    ])
    write_text_atomic(Path(target, INFO_NAME), text)


# --------------------------------------------------------------------------- lock


def lock_age_seconds(cache_dir: str):
    st = lstat_or_none(os.path.join(cache_dir, LOCK_NAME))
    return None if st is None else max(0.0, time.time() - st.st_mtime)


def locked_error(age) -> MirrorError:
    """``lock_held``: a run may still be going. ``previous_run_unfinished``: the lock outlived
    the task's time limit, so the run that made it was stopped (time limit, restart, kill)."""
    age = age or 0.0
    reason = "previous_run_unfinished" if age > RUN_CAP_SECONDS else "lock_held"
    return MirrorError("LOCKED", reason, {"lock_age_minutes": int(age // 60)})


def acquire_lock(cache_dir: str) -> tuple:
    """Returns (lock path, True when a stale lock of a stopped run was taken over)."""
    path = os.path.join(cache_dir, LOCK_NAME)
    took_over = False
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = lock_age_seconds(cache_dir)
            if age is not None and age > LOCK_STALE_SECONDS:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                took_over = True
                continue
            raise locked_error(age) from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(now_sgt() + "\n")
        return path, took_over
    raise locked_error(lock_age_seconds(cache_dir))


def release_lock(path) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- main


def run(config_path: str) -> dict:
    """One mirror run. Returns the status record that was written."""
    settings = None
    log_dir = None
    target_ours = False
    lock = None
    sha = commit_date = None
    extra = {}
    try:
        settings = load_settings(config_path)
        # Guard first: nothing (not even a local folder) is created before it passes.
        identity = guard_target(settings)
        try:
            os.makedirs(settings["log_dir"], exist_ok=True)
            os.makedirs(settings["cache_dir"], exist_ok=True)
        except OSError:
            raise MirrorError("CONFIG_INVALID", "work_dir_not_writable") from None
        log_dir = settings["log_dir"]
        target_ours = True
        lock, took_over = acquire_lock(settings["cache_dir"])
        if took_over or read_status_file(log_dir).get("status") == "running":
            extra["previous_run_unfinished"] = True
        write_status(log_dir, build_running())
        sha, commit_date = fetch_main(settings)
        target = settings["target_dir"]
        if read_info_commit(target) == sha:
            record = build_status("no_change", None, "main_unchanged", sha, commit_date, extra)
            if not target_status_is_ok(target):
                write_status(target, record)  # clear a stale failure, otherwise untouched
            write_status(log_dir, record)
            append_log(log_dir, record)
            return record
        files, folders, links_skipped = export_commit(settings, sha)
        # Re-check everything right before the first change to the target: the fetch and
        # export can take minutes, and the marker may have been removed (or Drive switched).
        if guard_target(settings) != identity:
            raise MirrorError("TARGET_GUARD", "target_changed")
        try:
            save_adopted(settings)
            write_status(target, build_running(sha, commit_date))
            counts = sync_into_target(settings, files, folders, make_confirm(target, identity))
        except MirrorError:
            raise
        except OSError:
            raise MirrorError("SYNC_FAILED", "copy_failed") from None
        counts["files_total"] = len(files)
        counts["links_skipped"] = links_skipped
        counts.update(extra)
        record = build_status("ok", None, "mirror_updated", sha, commit_date, counts)
        if not write_status(target, record):
            raise MirrorError("SYNC_FAILED", "status_write_failed")
        try:
            write_info(target, settings, sha, commit_date)
        except OSError:
            raise MirrorError("SYNC_FAILED", "info_write_failed") from None
        try:
            remove_tree(settings["staging_dir"], os.path.dirname(settings["staging_dir"]))
        except (OSError, MirrorError):
            pass  # staging is local scratch; the next run empties it again
        write_status(log_dir, record)
        append_log(log_dir, record)
        return record
    except MirrorError as error:
        record = build_status("failed", error.failure_class, error.reason, sha, commit_date,
                              dict(error.details, **extra))
    except Exception as error:  # noqa: BLE001 - classified, never echoed
        record = build_status("failed", "UNEXPECTED", "unexpected", sha, commit_date, extra,
                              error_type=type(error).__name__)
    finally:
        release_lock(lock)
    # A refused guard (including a marker removed mid-run) writes nothing more to the target.
    if target_ours and record["failure_class"] not in ("LOCKED", "TARGET_GUARD"):
        write_status(settings["target_dir"], record)
    if log_dir is None:
        log_dir = reporting_log_dir(config_path, settings)
    write_status(log_dir, record)
    append_log(log_dir, record)
    return record


def report_status(config_path: str) -> int:
    """Read-only summary of the last run, the lock and the Drive info file. Writes nothing."""
    try:
        settings = load_settings(config_path)
    except MirrorError as error:
        print("mirror_main_to_drive --status: settings invalid, reason=%s" % error.reason)
        return EXIT_CONFIG
    last = read_status_file(settings["log_dir"])
    age = lock_age_seconds(settings["cache_dir"])
    info_commit = read_info_commit(settings["target_dir"])
    if age is not None:
        verdict = "RUNNING" if age <= RUN_CAP_SECONDS else "UNFINISHED"
    elif last.get("status") == "running":
        verdict = "UNFINISHED"
    elif last.get("status") in ("ok", "no_change"):
        verdict = "OK"
    elif not last:
        verdict = "NEVER_RUN"
    else:
        verdict = "FAILED"
    explain = {
        "OK": "The last run finished.",
        "RUNNING": "A run is in progress now (lock younger than the 30-minute limit).",
        "UNFINISHED": ("The last run was stopped before it finished (time limit, restart or "
                       "kill). The Drive copy may be incomplete; the next run redoes it."),
        "NEVER_RUN": "No run has been recorded yet.",
        "FAILED": "The last run failed. See failure_class and the runbook.",
    }[verdict]
    print("verdict: %s - %s" % (verdict, explain))
    print("last_run: status=%s class=%s reason=%s commit=%s time=%s" % (
        last.get("status") or "-", last.get("failure_class") or "-", last.get("reason") or "-",
        last.get("commit") or "-", last.get("time") or "-"))
    print("lock: %s" % ("none" if age is None else "present, age %d min" % int(age // 60)))
    print("drive_info_file: %s" % ("commit=" + info_commit if info_commit else "missing"))
    return 0 if verdict in ("OK", "RUNNING") else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only mirror of the repository main branch into a marked Drive folder.")
    parser.add_argument("--config", required=True, help="path to the local settings JSON file")
    parser.add_argument("--status", action="store_true",
                        help="only print a read-only summary of the last run; change nothing")
    args = parser.parse_args(argv)
    if args.status:
        return report_status(args.config)
    record = run(args.config)
    print("mirror_main_to_drive: status=%s class=%s reason=%s commit=%s - %s" % (
        record["status"], record["failure_class"] or "-", record["reason"] or "-",
        record["commit"] or "-", record["message"]))
    return record["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
