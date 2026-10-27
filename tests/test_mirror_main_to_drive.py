"""Offline tests for the weekly read-only Drive mirror of main (#273).

Everything runs in temporary folders. The "remote" is a throwaway local git repository, so no
network, Google Drive or Task Scheduler is touched. Git runs with an empty global config so a
developer's own settings (autocrlf, signing, hooks) cannot change the result. All data is fake.
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mirror_main_to_drive.py"
INSTALLER = ROOT / "scripts" / "install_mirror_main_to_drive_task.ps1"
EXAMPLE_CONFIG = ROOT / "config" / "mirror_main_to_drive.example.json"
RUNBOOK = ROOT / "docs" / "drive_mirror_runbook.md"

_spec = importlib.util.spec_from_file_location("mirror_main_to_drive", SCRIPT)
mirror = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mirror)

GIT = shutil.which("git")
FAKE_TOKEN = "FAKE-TOKEN-not-a-secret-000"


def snapshot(folder: Path) -> dict:
    """{relative path: (bytes, mtime_ns)} for every regular file, links not followed."""
    result = {}
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        for name in filenames:
            path = Path(dirpath, name)
            if path.is_symlink():
                continue
            rel = path.relative_to(folder).as_posix()
            result[rel] = (path.read_bytes(), os.lstat(path).st_mtime_ns)
    return result


def make_dir_link(link: Path, target: Path) -> bool:
    """Create a directory symlink, or a junction on Windows. False when neither is allowed."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return done.returncode == 0 and link.exists()
    return False


@unittest.skipIf(GIT is None, "git is not installed")
class MirrorTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        empty_config = self.root / "empty.gitconfig"
        empty_config.write_text("", encoding="utf-8")
        self.env = {
            "GIT_CONFIG_GLOBAL": str(empty_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Mirror Test",
            "GIT_AUTHOR_EMAIL": "mirror-test@example.com",
            "GIT_COMMITTER_NAME": "Mirror Test",
            "GIT_COMMITTER_EMAIL": "mirror-test@example.com",
        }
        env_patch = mock.patch.dict(os.environ, self.env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        # The throwaway local repository stands in for GitHub (test-only switch).
        local_patch = mock.patch.object(mirror, "_ALLOW_LOCAL_SOURCE_FOR_TESTS", True)
        local_patch.start()
        self.addCleanup(local_patch.stop)
        self.addCleanup(self._cleanup)

        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.git("init", "-q", "-b", "main")
        self.write_remote({
            "README.md": b"# Fake repo\n",
            "scripts/tool.py": b"print('fake tool')\n",
            "docs/old/notes.md": b"old notes\n",
            "keep.txt": b"unchanged content\n",
            "private-notes.txt": b"export-ignored file\n",
            ".gitattributes": b"private-notes.txt export-ignore\n",
        })
        self.commit("first")

        self.drive = self.root / "drive"
        self.target = self.drive / "mirror"
        self.target.mkdir(parents=True)
        (self.target / mirror.MARKER_NAME).write_bytes(b"")
        self.outside_file = self.drive / "outside.txt"
        self.outside_file.write_bytes(b"not part of the mirror\n")
        self.outside_dir = self.drive / "other-folder"
        self.outside_dir.mkdir()
        (self.outside_dir / "file.txt").write_bytes(b"other folder content\n")
        self.cache = self.root / "local" / "cache"
        self.logs = self.root / "local" / "logs"
        self.config = self.root / "settings.json"
        self.write_config()

    def _cleanup(self):
        # Remove any links first so the temporary-folder cleanup never follows them.
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            for name in list(dirnames) + filenames:
                path = os.path.join(dirpath, name)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if mirror.is_link_stat(st):
                    mirror.remove_link(path)
        self._tmp.cleanup()

    # -- fixtures

    def git(self, *args, cwd=None):
        done = subprocess.run(["git", *args], cwd=cwd or self.remote, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def write_remote(self, files: dict):
        for rel, data in files.items():
            path = self.remote / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def write_config(self, **overrides):
        settings = {
            "repo_url": str(self.remote),
            "branch": "main",
            "cache_dir": str(self.cache),
            "target_dir": str(self.target),
            "log_dir": str(self.logs),
        }
        settings.update(overrides)
        settings = {key: value for key, value in settings.items() if value is not None}
        self.config.write_text(json.dumps(settings), encoding="utf-8")

    def run_mirror(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mirror.main(["--config", str(self.config)])
        self.last_stdout = out.getvalue()
        return code

    def log_status(self):
        return json.loads((self.logs / mirror.STATUS_NAME).read_text(encoding="utf-8"))

    def target_status(self):
        return json.loads((self.target / mirror.STATUS_NAME).read_text(encoding="utf-8"))

    def info_text(self):
        return (self.target / mirror.INFO_NAME).read_text(encoding="utf-8")

    def mirrored(self) -> dict:
        reserved = set(mirror.RESERVED_ROOT_NAMES)
        return {rel: data for rel, (data, _) in snapshot(self.target).items() if rel not in reserved}

    def outside_snapshot(self):
        return (self.outside_file.read_bytes(), os.lstat(self.outside_file).st_mtime_ns,
                snapshot(self.outside_dir))

    def adopt_then_move_main(self):
        """First run into the empty folder, then a new upstream commit so the next run syncs."""
        self.assertEqual(self.run_mirror(), 0)
        self.write_remote({"README.md": b"# Fake repo, next commit\n"})
        return self.commit("next")

    def run_status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mirror.main(["--config", str(self.config), "--status"])
        self.last_stdout = out.getvalue()
        return code


class FirstRunTests(MirrorTestBase):
    def test_first_run_copies_files_and_writes_info(self):
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.run_mirror(), 0)
        self.assertEqual(self.mirrored(), {
            "README.md": b"# Fake repo\n",
            "scripts/tool.py": b"print('fake tool')\n",
            "docs/old/notes.md": b"old notes\n",
            "keep.txt": b"unchanged content\n",
            ".gitattributes": b"private-notes.txt export-ignore\n",
        })
        info = self.info_text()
        self.assertIn(mirror.INFO_NOTICE, info)
        self.assertIn("commit: " + head, info)
        self.assertRegex(info, r"copied_at: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+08:00")
        self.assertRegex(info, r"commit_date: \d{4}-\d\d-\d\dT")
        self.assertIn("source: " + str(self.remote), info)
        for status in (self.log_status(), self.target_status()):
            self.assertEqual(status["status"], "ok")
            self.assertIsNone(status["failure_class"])
            self.assertEqual(status["commit"], head)
            self.assertTrue(status["time"].endswith("+08:00"))
        self.assertTrue((self.target / mirror.MARKER_NAME).is_file())
        self.assertTrue((self.logs / mirror.LOG_NAME).is_file())

    def test_git_folder_and_export_ignored_files_never_copied(self):
        self.assertEqual(self.run_mirror(), 0)
        for dirpath, dirnames, filenames in os.walk(self.target):
            self.assertNotIn(".git", dirnames)
            self.assertNotIn(".git", filenames)
        self.assertFalse((self.target / "private-notes.txt").exists())
        self.assertFalse(any(self.target.rglob("*" + mirror.TEMP_SUFFIX)))

    def test_upstream_symlink_is_not_mirrored(self):
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.remote,
                              input=b"../../outside.txt", capture_output=True).stdout.decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "120000,%s,link-out" % blob)
        self.git("commit", "-q", "-m", "add link")
        self.assertEqual(self.run_mirror(), 0)
        self.assertFalse(os.path.lexists(self.target / "link-out"))
        self.assertEqual(self.log_status()["links_skipped"], 1)


class NoChangeAndUpdateTests(MirrorTestBase):
    def test_no_op_when_main_unchanged_leaves_target_untouched(self):
        self.assertEqual(self.run_mirror(), 0)
        before = snapshot(self.target)
        time.sleep(0.05)
        self.assertEqual(self.run_mirror(), 0)
        self.assertEqual(snapshot(self.target), before)
        status = self.log_status()
        self.assertEqual(status["status"], "no_change")
        self.assertEqual(status["reason"], "main_unchanged")
        self.assertEqual(self.target_status()["status"], "ok")

    def test_upstream_add_change_delete_is_reflected(self):
        self.assertEqual(self.run_mirror(), 0)
        keep_mtime = os.lstat(self.target / "keep.txt").st_mtime_ns
        (self.remote / "docs" / "old" / "notes.md").unlink()
        (self.remote / "scripts" / "tool.py").write_bytes(b"print('fake tool v2')\n")
        self.write_remote({"docs/new/guide.md": b"new guide\n"})
        head = self.commit("second")
        time.sleep(0.05)
        self.assertEqual(self.run_mirror(), 0)
        files = self.mirrored()
        self.assertNotIn("docs/old/notes.md", files)
        self.assertFalse((self.target / "docs" / "old").exists())
        self.assertEqual(files["scripts/tool.py"], b"print('fake tool v2')\n")
        self.assertEqual(files["docs/new/guide.md"], b"new guide\n")
        self.assertEqual(os.lstat(self.target / "keep.txt").st_mtime_ns, keep_mtime,
                         "an unchanged file must not be rewritten")
        self.assertIn("commit: " + head, self.info_text())
        status = self.log_status()
        self.assertEqual(status["files_written"], 2)
        self.assertEqual(status["files_unchanged"], 3)

    def test_drive_edits_and_stray_files_are_overwritten_when_main_moves(self):
        self.assertEqual(self.run_mirror(), 0)
        (self.target / "keep.txt").write_bytes(b"edited in Drive, longer than before\n")
        (self.target / "stray.txt").write_bytes(b"added in Drive\n")
        (self.target / "stray-folder").mkdir()
        (self.target / "stray-folder" / "x.txt").write_bytes(b"x\n")
        self.write_remote({"README.md": b"# Fake repo v2\n"})
        self.commit("third")
        self.assertEqual(self.run_mirror(), 0)
        files = self.mirrored()
        self.assertEqual(files["keep.txt"], b"unchanged content\n")
        self.assertNotIn("stray.txt", files)
        self.assertFalse((self.target / "stray-folder").exists())
        self.assertTrue((self.target / mirror.MARKER_NAME).is_file())

    def test_stale_failure_status_in_target_is_cleared_on_no_change(self):
        self.assertEqual(self.run_mirror(), 0)
        (self.target / mirror.STATUS_NAME).write_text('{"status": "failed"}', encoding="utf-8")
        self.assertEqual(self.run_mirror(), 0)
        self.assertEqual(self.target_status()["status"], "no_change")


class TargetGuardTests(MirrorTestBase):
    def assert_refused(self, reason):
        code = self.run_mirror()
        self.assertEqual(code, mirror.EXIT_TARGET_GUARD, self.last_stdout)
        self.assertIn("reason=" + reason, self.last_stdout)
        self.assertFalse(self.cache.exists(), "nothing is fetched or cached when refused")

    def test_refuses_without_marker_and_changes_nothing(self):
        (self.target / mirror.MARKER_NAME).unlink()
        (self.target / "existing.txt").write_bytes(b"keep me\n")
        before = snapshot(self.target)
        self.assert_refused("marker_missing")
        self.assertEqual(snapshot(self.target), before)
        self.assertEqual(self.log_status()["failure_class"], "TARGET_GUARD")

    def test_refuses_drive_root(self):
        self.write_config(target_dir=Path(self.root).anchor)
        self.assert_refused("drive_root")

    def test_refuses_target_containing_git(self):
        (self.target / ".git").mkdir()
        (self.target / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        before = snapshot(self.target)
        self.assert_refused("target_contains_git")
        self.assertEqual(snapshot(self.target), before)

    def test_refuses_target_inside_git_checkout(self):
        inside = self.remote / "mirror-here"
        inside.mkdir()
        (inside / mirror.MARKER_NAME).write_bytes(b"")
        self.write_config(target_dir=str(inside))
        self.assert_refused("target_inside_git_checkout")

    def test_refuses_user_profile_root_and_other_profiles(self):
        users = self.root / "users"
        alice, bob = users / "alice", users / "bob"
        for folder in (alice, bob):
            folder.mkdir(parents=True)
            (folder / mirror.MARKER_NAME).write_bytes(b"")
        with mock.patch.dict(os.environ, {"USERPROFILE": str(alice), "HOME": str(alice)}):
            self.write_config(target_dir=str(alice))
            self.assert_refused("profile_root")
            self.write_config(target_dir=str(bob))
            self.assert_refused("profile_root")
            self.write_config(target_dir=str(users))
            self.assert_refused("profile_root")

    def test_refuses_work_folders_inside_target(self):
        self.write_config(log_dir=str(self.target / "logs"))
        before = snapshot(self.target)
        self.assert_refused("work_dir_overlaps_target")
        self.assertEqual(snapshot(self.target), before)
        self.assertFalse((self.target / "logs").exists())

    def test_marker_never_deleted_and_reserved_upstream_name_refused(self):
        self.assertEqual(self.run_mirror(), 0)
        self.write_remote({mirror.MARKER_NAME: b"upstream must not ship this\n"})
        self.commit("reserved")
        self.assertEqual(self.run_mirror(), mirror.EXIT_EXPORT)
        self.assertEqual(self.log_status()["reason"], "reserved_name_in_archive")
        self.assertEqual((self.target / mirror.MARKER_NAME).read_bytes(), b"")


class OutsideTargetTests(MirrorTestBase):
    def test_files_outside_target_untouched(self):
        before = self.outside_snapshot()
        self.assertEqual(self.run_mirror(), 0)
        (self.remote / "keep.txt").unlink()
        self.commit("delete")
        self.assertEqual(self.run_mirror(), 0)
        self.assertEqual(self.outside_snapshot(), before)

    def test_links_inside_target_are_removed_not_followed(self):
        self.adopt_then_move_main()
        link = self.target / "link-to-outside"
        if not make_dir_link(link, self.outside_dir):
            self.skipTest("this machine cannot create a directory link or junction")
        before = self.outside_snapshot()
        self.assertEqual(self.run_mirror(), 0)
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(self.outside_snapshot(), before)

    def test_link_where_a_mirrored_folder_belongs_is_replaced(self):
        self.adopt_then_move_main()
        shutil.rmtree(self.target / "scripts")
        link = self.target / "scripts"
        if not make_dir_link(link, self.outside_dir):
            self.skipTest("this machine cannot create a directory link or junction")
        before = self.outside_snapshot()
        self.assertEqual(self.run_mirror(), 0)
        st = os.lstat(self.target / "scripts")
        self.assertFalse(mirror.is_link_stat(st))
        self.assertEqual((self.target / "scripts" / "tool.py").read_bytes(), b"print('fake tool')\n")
        self.assertEqual(self.outside_snapshot(), before)
        self.assertFalse((self.outside_dir / "tool.py").exists())


class StatusAndFailureTests(MirrorTestBase):
    def all_reported_text(self):
        parts = [self.last_stdout]
        if self.logs.exists():
            parts += [p.read_text(encoding="utf-8") for p in self.logs.iterdir() if p.is_file()]
        if (self.target / mirror.STATUS_NAME).exists():
            parts.append((self.target / mirror.STATUS_NAME).read_text(encoding="utf-8"))
        return "\n".join(parts)

    def assert_no_paths_or_secrets(self, text):
        forbidden = {str(self.root), os.path.realpath(str(self.root)), str(self.remote),
                     os.path.expanduser("~"), tempfile.gettempdir(), FAKE_TOKEN}
        for value in forbidden:
            self.assertNotIn(value, text)
            self.assertNotIn(value.replace("\\", "\\\\"), text)  # JSON-escaped form

    def test_fetch_failure_reports_status_in_log_and_target(self):
        not_a_repo = self.root / "not-a-repo"
        not_a_repo.mkdir()
        self.write_config(repo_url=str(not_a_repo))
        self.assertEqual(self.run_mirror(), mirror.EXIT_FETCH)
        for status in (self.log_status(), self.target_status()):
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["failure_class"], "FETCH_FAILED")
            self.assertEqual(status["exit_code"], mirror.EXIT_FETCH)
        self.assertFalse((self.target / mirror.INFO_NAME).exists())
        self.assert_no_paths_or_secrets(self.all_reported_text())

    def test_settings_failure_reports_status_in_log(self):
        self.write_config(cache_dir=None)
        self.assertEqual(self.run_mirror(), mirror.EXIT_CONFIG)
        status = self.log_status()
        self.assertEqual(status["failure_class"], "CONFIG_INVALID")
        self.assertEqual(status["reason"], "missing_cache_dir")
        self.assertFalse((self.target / mirror.STATUS_NAME).exists())

    def test_credentials_in_repo_url_refused_and_never_echoed(self):
        self.write_config(repo_url="https://example-user:%s@github.example.com/x/y.git" % FAKE_TOKEN)
        self.assertEqual(self.run_mirror(), mirror.EXIT_CONFIG)
        self.assertEqual(self.log_status()["reason"], "repo_url_has_credentials")
        self.assert_no_paths_or_secrets(self.all_reported_text())

    def test_non_https_url_and_other_branch_refused(self):
        self.write_config(repo_url="ssh://github.example.com/x/y.git")
        self.assertEqual(self.run_mirror(), mirror.EXIT_CONFIG)
        self.assertEqual(self.log_status()["reason"], "repo_url_not_https")
        self.write_config(branch="feature")
        self.assertEqual(self.run_mirror(), mirror.EXIT_CONFIG)
        self.assertEqual(self.log_status()["reason"], "branch_not_main")

    def test_success_output_has_no_paths(self):
        self.assertEqual(self.run_mirror(), 0)
        self.assertEqual(self.run_mirror(), 0)
        self.assert_no_paths_or_secrets(self.all_reported_text())
        log_lines = (self.logs / mirror.LOG_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(log_lines), 2)
        self.assertIn("status=ok", log_lines[0])
        self.assertIn("status=no_change", log_lines[1])

    def test_lock_held_refuses_and_stale_lock_is_taken_over(self):
        self.cache.mkdir(parents=True)
        lock = self.cache / mirror.LOCK_NAME
        lock.write_text("held\n", encoding="utf-8")
        before = snapshot(self.target)
        self.assertEqual(self.run_mirror(), mirror.EXIT_LOCKED)
        self.assertEqual(snapshot(self.target), before)
        self.assertEqual(self.log_status()["failure_class"], "LOCKED")
        old = time.time() - mirror.LOCK_STALE_SECONDS - 60
        os.utime(lock, (old, old))
        self.assertEqual(self.run_mirror(), 0)
        self.assertFalse(lock.exists())

    def test_interrupted_copy_is_redone_next_run(self):
        self.assertEqual(self.run_mirror(), 0)
        self.write_remote({"README.md": b"# Fake repo v3\n"})
        head = self.commit("fourth")
        with mock.patch.object(mirror.shutil, "copyfileobj", side_effect=OSError("disk full")):
            self.assertEqual(self.run_mirror(), mirror.EXIT_SYNC)
        self.assertFalse((self.target / mirror.INFO_NAME).exists(),
                         "the info file must never name a commit that was not fully copied")
        self.assertEqual(self.target_status()["failure_class"], "SYNC_FAILED")
        self.assertNotIn("disk full", self.all_reported_text())
        self.assertEqual(self.run_mirror(), 0)
        self.assertIn("commit: " + head, self.info_text())
        self.assertEqual(self.mirrored()["README.md"], b"# Fake repo v3\n")


class H1TempFileTests(MirrorTestBase):
    """Internal temp files are created new; an existing name (link) is never written through."""

    def plant(self, name, kind="hard"):
        victim = self.root / "outside-victim.txt"
        victim.write_bytes(b"victim content\n")
        try:
            if kind == "hard":
                os.link(victim, self.target / name)
            else:
                os.symlink(victim, self.target / name)
        except (OSError, NotImplementedError):
            self.skipTest("this machine cannot create a %s link here" % kind)
        return victim

    def break_fetch(self):
        not_a_repo = self.root / "not-a-repo"
        not_a_repo.mkdir()
        self.write_config(repo_url=str(not_a_repo))

    def test_failure_status_not_written_through_planted_hard_link(self):
        self.assertEqual(self.run_mirror(), 0)
        victim = self.plant(mirror.STATUS_NAME + mirror.TEMP_SUFFIX)
        self.break_fetch()
        self.assertEqual(self.run_mirror(), mirror.EXIT_FETCH)
        self.assertEqual(victim.read_bytes(), b"victim content\n")
        self.assertEqual(self.target_status()["failure_class"], "FETCH_FAILED")
        self.assertFalse(os.path.lexists(self.target / (mirror.STATUS_NAME + mirror.TEMP_SUFFIX)))

    def test_no_change_status_not_written_through_planted_hard_link(self):
        self.assertEqual(self.run_mirror(), 0)
        (self.target / mirror.STATUS_NAME).write_text('{"status": "failed"}', encoding="utf-8")
        victim = self.plant(mirror.STATUS_NAME + mirror.TEMP_SUFFIX)
        self.assertEqual(self.run_mirror(), 0)
        self.assertEqual(victim.read_bytes(), b"victim content\n")
        self.assertEqual(self.target_status()["status"], "no_change")

    def test_failure_status_not_written_through_planted_symlink(self):
        self.assertEqual(self.run_mirror(), 0)
        victim = self.plant(mirror.STATUS_NAME + mirror.TEMP_SUFFIX, kind="symlink")
        self.break_fetch()
        self.assertEqual(self.run_mirror(), mirror.EXIT_FETCH)
        self.assertEqual(victim.read_bytes(), b"victim content\n")
        self.assertEqual(self.target_status()["failure_class"], "FETCH_FAILED")

    def test_copy_temp_not_written_through_planted_hard_link(self):
        victim = self.root / "outside-victim.txt"
        victim.write_bytes(b"victim content\n")
        destination = self.root / "dest.txt"
        source = self.root / "source.txt"
        source.write_bytes(b"new content\n")
        os.link(victim, str(destination) + mirror.TEMP_SUFFIX)
        mirror.copy_file_into(str(source), str(destination))
        self.assertEqual(destination.read_bytes(), b"new content\n")
        self.assertEqual(victim.read_bytes(), b"victim content\n")


class H2FirstRunTests(MirrorTestBase):
    """A folder never mirrored into must hold only the marker; Drive root folders are refused."""

    def assert_refused(self, reason):
        code = self.run_mirror()
        self.assertEqual(code, mirror.EXIT_TARGET_GUARD, self.last_stdout)
        self.assertIn("reason=" + reason, self.last_stdout)

    def test_first_run_refuses_folder_with_other_content(self):
        plan = self.target / "team-files" / "plan.txt"
        plan.parent.mkdir()
        plan.write_bytes(b"fake team plan\n")
        before = snapshot(self.target)
        self.assert_refused("first_run_target_not_empty")
        self.assertEqual(snapshot(self.target), before)
        self.assertFalse(self.cache.exists())

    def test_first_run_allows_marker_and_old_status_file(self):
        (self.target / mirror.STATUS_NAME).write_text('{"status": "failed"}', encoding="utf-8")
        self.assertEqual(self.run_mirror(), 0)

    def test_interrupted_first_copy_is_finished_but_not_carried_to_another_folder(self):
        with mock.patch.object(mirror.shutil, "copyfileobj", side_effect=OSError("disk full")):
            self.assertEqual(self.run_mirror(), mirror.EXIT_SYNC)
        self.assertFalse((self.target / mirror.INFO_NAME).exists())
        self.assertEqual(self.run_mirror(), 0, self.last_stdout)
        other = self.drive / "other-target"
        other.mkdir()
        (other / mirror.MARKER_NAME).write_bytes(b"")
        (other / "keep.txt").write_bytes(b"someone else's file\n")
        self.write_config(target_dir=str(other))
        self.assert_refused("first_run_target_not_empty")
        self.assertEqual((other / "keep.txt").read_bytes(), b"someone else's file\n")

    def test_refuses_drive_root_folders_by_name(self):
        for rel in ("G/My Drive", "G/my drive", "G/Shared drives", "G/Shared drives/Fake Team",
                    "G/Other computers", "G/Other computers/FAKE-PC"):
            folder = self.root.joinpath(*rel.split("/"))
            folder.mkdir(parents=True, exist_ok=True)
            (folder / mirror.MARKER_NAME).write_bytes(b"")
            (folder / "team-files.txt").write_bytes(b"fake\n")
            self.write_config(target_dir=str(folder))
            self.assert_refused("drive_root_folder")
            self.assertTrue((folder / "team-files.txt").exists())

    def test_refuses_drive_root_folder_by_resolved_path(self):
        my_drive = self.root / "G" / "My Drive"
        my_drive.mkdir(parents=True)
        (my_drive / mirror.MARKER_NAME).write_bytes(b"")
        alias = self.root / "alias"
        if not make_dir_link(alias, my_drive):
            self.skipTest("this machine cannot create a directory link or junction")
        self.write_config(target_dir=str(alias))
        self.assert_refused("drive_root_folder")


class H3ResolvedPathTests(MirrorTestBase):
    """Guards compare resolved paths; network and device paths are refused."""

    def test_device_and_network_paths_refused(self):
        plain = str(self.target).lstrip("\\/")
        for value in ("\\\\?\\" + plain, "\\\\.\\" + plain, "\\\\localhost\\C$\\fake",
                      "//fake-server/share/mirror"):
            self.write_config(target_dir=value)
            self.assertEqual(self.run_mirror(), mirror.EXIT_CONFIG, value)
            self.assertIn("reason=unc_or_device_target_dir", self.last_stdout)
        self.write_config(cache_dir="\\\\fake-server\\share\\cache")
        self.assertEqual(self.run_mirror(), mirror.EXIT_CONFIG)
        self.assertIn("reason=unc_or_device_cache_dir", self.last_stdout)

    def test_profile_reached_through_a_junction_is_refused(self):
        users = self.root / "users"
        alice = users / "alice"
        (alice / "Documents").mkdir(parents=True)
        (alice / mirror.MARKER_NAME).write_bytes(b"")
        keep = alice / "Documents" / "keep.txt"
        keep.write_bytes(b"fake private file\n")
        junction = self.root / "jn"
        if not make_dir_link(junction, users):
            self.skipTest("this machine cannot create a directory link or junction")
        with mock.patch.dict(os.environ, {"USERPROFILE": str(alice), "HOME": str(alice)}):
            self.write_config(target_dir=str(junction / "alice"))
            self.assertEqual(self.run_mirror(), mirror.EXIT_TARGET_GUARD, self.last_stdout)
            self.assertIn("reason=profile_root", self.last_stdout)
        self.assertEqual(keep.read_bytes(), b"fake private file\n")

    def test_cache_reached_through_a_junction_into_the_target_is_refused(self):
        junction = self.root / "localjn"
        if not make_dir_link(junction, self.target):
            self.skipTest("this machine cannot create a directory link or junction")
        before = snapshot(self.target)
        self.write_config(cache_dir=str(junction / "cache"))
        self.assertEqual(self.run_mirror(), mirror.EXIT_TARGET_GUARD, self.last_stdout)
        self.assertIn("reason=work_dir_overlaps_target", self.last_stdout)
        self.assertEqual(snapshot(self.target), before)


KILL_AFTER_FIRST_COPY = r"""
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("mirror_kill", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m._ALLOW_LOCAL_SOURCE_FOR_TESTS = True
real_copy = m.copy_file_into
done = []
def copy_then_die(source, destination):
    if done:
        os._exit(9)  # hard kill: no finally, no lock release, no status write
    done.append(1)
    real_copy(source, destination)
m.copy_file_into = copy_then_die
m.main(["--config", sys.argv[2]])
"""


class H4UnfinishedRunTests(MirrorTestBase):
    """A hard-killed run stays visible in the status, the next run and --status."""

    def age_lock(self, seconds):
        old = time.time() - seconds
        os.utime(self.cache / mirror.LOCK_NAME, (old, old))

    def test_hard_killed_run_is_reported_truthfully(self):
        self.assertEqual(self.run_mirror(), 0)
        self.assertEqual(self.run_status(), 0)
        self.assertIn("verdict: OK", self.last_stdout)
        self.write_remote({"README.md": b"v2\n", "keep.txt": b"v2\n", "scripts/tool.py": b"v2\n"})
        head = self.commit("three changes")
        done = subprocess.run([sys.executable, "-c", KILL_AFTER_FIRST_COPY, str(SCRIPT),
                               str(self.config)], capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 9, done.stderr)
        self.assertEqual(self.log_status()["status"], "running")
        self.assertEqual(self.target_status()["status"], "running")
        self.assertFalse((self.target / mirror.INFO_NAME).exists())
        self.assertTrue((self.cache / mirror.LOCK_NAME).exists())

        # Straight after: the lock is younger than the task limit, so a run may still be going.
        self.assertEqual(self.run_status(), 0)
        self.assertIn("verdict: RUNNING", self.last_stdout)
        self.assertEqual(self.run_mirror(), mirror.EXIT_LOCKED)
        self.assertEqual(self.log_status()["reason"], "lock_held")

        # Past the 30-minute limit: reported as a stopped run, not as "another run".
        self.age_lock(40 * 60)
        self.assertEqual(self.run_mirror(), mirror.EXIT_LOCKED)
        status = self.log_status()
        self.assertEqual(status["reason"], "previous_run_unfinished")
        self.assertGreaterEqual(status["lock_age_minutes"], 40)
        self.assertEqual(self.run_status(), 1)
        self.assertIn("verdict: UNFINISHED", self.last_stdout)
        self.assertIn("drive_info_file: missing", self.last_stdout)

        # Past the stale limit: taken over, redone, and the takeover is recorded.
        self.age_lock(mirror.LOCK_STALE_SECONDS + 60)
        self.assertEqual(self.run_mirror(), 0, self.last_stdout)
        status = self.log_status()
        self.assertEqual(status["status"], "ok")
        self.assertTrue(status["previous_run_unfinished"])
        self.assertIn("commit: " + head, self.info_text())
        self.assertEqual(self.mirrored()["scripts/tool.py"], b"v2\n")
        self.assertEqual(self.run_status(), 0)
        self.assertIn("verdict: OK", self.last_stdout)

    def test_log_says_running_while_the_run_is_going(self):
        seen = {}
        real_fetch = mirror.fetch_main

        def fetch_and_look(settings):
            seen["status"] = self.log_status()["status"]
            return real_fetch(settings)

        with mock.patch.object(mirror, "fetch_main", side_effect=fetch_and_look):
            self.assertEqual(self.run_mirror(), 0)
        self.assertEqual(seen["status"], "running")
        self.assertEqual(self.log_status()["status"], "ok")

    def test_status_writes_nothing(self):
        self.assertEqual(self.run_mirror(), 0)
        before = (snapshot(self.target), snapshot(self.logs), snapshot(self.cache))
        self.run_status()
        self.assertEqual((snapshot(self.target), snapshot(self.logs), snapshot(self.cache)), before)
        self.assertNotIn(str(self.root), self.last_stdout)


class H5RecheckTests(MirrorTestBase):
    """The marker and folder identity are re-checked before the sync and before every delete."""

    def setUp(self):
        super().setUp()
        self.adopt_then_move_main()
        self.stray = self.target / "stray.txt"
        self.stray.write_bytes(b"stray file\n")
        self.info_before = self.info_text()
        self.status_before = self.target_status()

    def assert_stopped(self, reason):
        self.assertEqual(self.last_code, mirror.EXIT_TARGET_GUARD, self.last_stdout)
        self.assertIn("reason=" + reason, self.last_stdout)
        self.assertTrue(self.stray.exists(), "nothing may be deleted after the marker went")
        self.assertEqual(self.info_text(), self.info_before)

    def test_marker_removed_during_fetch_stops_before_the_sync(self):
        real_fetch = mirror.fetch_main

        def fetch_then_remove_marker(settings):
            result = real_fetch(settings)
            (self.target / mirror.MARKER_NAME).unlink()
            return result

        with mock.patch.object(mirror, "fetch_main", side_effect=fetch_then_remove_marker):
            self.last_code = self.run_mirror()
        self.assert_stopped("marker_missing")
        self.assertEqual(self.target_status(), self.status_before, "refused: target not written")

    def test_marker_removed_after_the_last_guard_stops_before_the_first_delete(self):
        real_load = mirror.load_manifest

        def remove_marker_then_load(settings):
            (self.target / mirror.MARKER_NAME).unlink()
            return real_load(settings)

        with mock.patch.object(mirror, "load_manifest", side_effect=remove_marker_then_load):
            self.last_code = self.run_mirror()
        self.assert_stopped("marker_missing")

    def test_target_folder_swapped_during_export_is_refused(self):
        real_export = mirror.export_commit
        moved = self.drive / "mirror-moved"

        def export_then_swap(settings, sha):
            result = real_export(settings, sha)
            os.rename(self.target, moved)
            self.target.mkdir()
            shutil.copy2(moved / mirror.MARKER_NAME, self.target / mirror.MARKER_NAME)
            shutil.copy2(moved / mirror.INFO_NAME, self.target / mirror.INFO_NAME)
            (self.target / "stray.txt").write_bytes(b"stray file\n")
            return result

        with mock.patch.object(mirror, "export_commit", side_effect=export_then_swap):
            self.last_code = self.run_mirror()
        self.assert_stopped("target_changed")


class H6SourcePinTests(MirrorTestBase):
    """Only this repository's https address; PC-wide git config is ignored."""

    def test_local_path_source_refused_without_the_test_switch(self):
        with mock.patch.object(mirror, "_ALLOW_LOCAL_SOURCE_FOR_TESTS", False):
            self.assertEqual(self.run_mirror(), mirror.EXIT_CONFIG)
        self.assertIn("reason=repo_url_local_path", self.last_stdout)
        self.assertFalse(self.cache.exists())

    def test_only_this_repository_https_address_is_accepted(self):
        with mock.patch.object(mirror, "_ALLOW_LOCAL_SOURCE_FOR_TESTS", False):
            for url in (mirror.DEFAULT_REPO_URL, "https://github.com/x-boundaries/automation",
                        "HTTPS://GitHub.com/X-Boundaries/automation.git"):
                self.write_config(repo_url=url)
                self.assertEqual(mirror.load_settings(str(self.config))["repo_url"], url)
            for url in ("https://github.com/x-boundaries/TRANSPORT-automation.git",
                        "https://github.example.com/x-boundaries/automation.git",
                        "https://github.com/x-boundaries/automation.git/../other"):
                self.write_config(repo_url=url)
                with self.assertRaises(mirror.MirrorError) as caught:
                    mirror.load_settings(str(self.config))
                self.assertEqual(caught.exception.reason, "repo_url_not_this_repo")

    def test_global_git_config_cannot_redirect_the_fetch(self):
        other = self.root / "other-remote"
        other.mkdir()
        self.git("init", "-q", "-b", "main", cwd=other)
        (other / "README.md").write_bytes(b"# NOT the mirrored repo\n")
        self.git("add", "-A", cwd=other)
        self.git("commit", "-q", "-m", "other", cwd=other)
        remote_url = self.remote.as_posix()
        evil = self.root / "evil.gitconfig"
        evil.write_text('[url "%s"]\n\tinsteadOf = %s\n' % (other.as_posix(), remote_url),
                        encoding="utf-8")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(evil)}):
            redirected = self.git("ls-remote", remote_url, "refs/heads/main").split()[0]
            if redirected != self.git("rev-parse", "HEAD", cwd=other):
                self.skipTest("this git does not apply insteadOf to local paths")
            self.write_config(repo_url=remote_url)
            self.assertEqual(self.run_mirror(), 0, self.last_stdout)
        self.assertIn("commit: " + self.git("rev-parse", "HEAD"), self.info_text())
        self.assertEqual(self.mirrored()["README.md"], b"# Fake repo\n")

    def test_git_env_and_command_drop_inherited_git_settings(self):
        inherited = {"GIT_CONFIG_PARAMETERS": "'url.fake.insteadof'='x'", "GIT_CONFIG_COUNT": "1",
                     "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": "X: fake",
                     "GIT_DIR": "fake"}
        with mock.patch.dict(os.environ, inherited):
            env = mirror.git_env()
        for key in inherited:
            self.assertNotIn(key, env)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_SYSTEM"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        with mock.patch.object(mirror, "_ALLOW_LOCAL_SOURCE_FOR_TESTS", False):
            command = mirror.git_base_command()
        self.assertIn("protocol.file.allow=never", command)
        self.assertIn("http.extraHeader=", command)
        self.assertIn("credential.helper=", command)


class LaterItemTests(MirrorTestBase):
    def test_read_only_mirrored_file_does_not_block_the_next_update(self):  # L1
        self.assertEqual(self.run_mirror(), 0)
        os.chmod(self.target / "README.md", stat.S_IREAD)
        self.write_remote({"README.md": b"# Fake repo v5\n"})
        self.commit("fifth")
        self.assertEqual(self.run_mirror(), 0, self.last_stdout)
        self.assertEqual(self.mirrored()["README.md"], b"# Fake repo v5\n")

    def test_staging_error_is_classed_as_export_failed(self):  # L6
        with mock.patch.object(mirror, "prepare_staging", side_effect=OSError("fake")):
            self.assertEqual(self.run_mirror(), mirror.EXIT_EXPORT)
        self.assertEqual(self.log_status()["reason"], "staging_failed")


class StaticFileTests(unittest.TestCase):
    def setUp(self):
        self.installer = INSTALLER.read_text(encoding="utf-8")
        self.code = "\n".join(line for line in self.installer.splitlines()
                              if not line.lstrip().startswith("#"))

    def test_installer_weekly_trigger_and_settings(self):
        self.assertIn("SEPARATE OWNER OK", self.installer)
        for needle in ("-Weekly", "-DaysOfWeek Sunday", ".AddHours(3)", "-StartWhenAvailable",
                       "-MultipleInstances IgnoreNew", "(New-TimeSpan -Minutes 30)",
                       "-LogonType Interactive", "Get-ScheduledTask -TaskName $TaskName"):
            self.assertIn(needle, self.code)
        for banned in ("-Force", "Repetition", "-Daily", "-Once", "S4U", "-RunLevel Highest"):
            self.assertNotIn(banned, self.code)

    def test_installer_needs_full_python_path_and_user_without_default(self):
        self.assertRegex(self.code, r"Mandatory = \$true\)\]\s*\[string\]\$PythonExe\s*,")
        self.assertRegex(self.code, r"Mandatory = \$true\)\]\s*\[string\]\$UserId\s*,")
        self.assertIn("IsPathRooted($PythonExe)", self.code)
        self.assertNotRegex(self.code, r"\$UserId\s*=")

    def test_installer_parses_when_powershell_available(self):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if shell is None:
            self.skipTest("PowerShell is not installed")
        command = ("$e = $null; [System.Management.Automation.Language.Parser]::ParseFile("
                   "$env:XB_MIRROR_INSTALLER, [ref]$null, [ref]$e) | Out-Null; "
                   "if (@($e).Count -gt 0) { exit 1 } else { exit 0 }")
        env = dict(os.environ, XB_MIRROR_INSTALLER=str(INSTALLER))
        done = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                              env=env, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_example_config_has_placeholders_only(self):
        data = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        keys = {key for key in data if not key.startswith("_")}
        self.assertEqual(keys, mirror.SETTINGS_KEYS)
        self.assertEqual(data["repo_url"], mirror.DEFAULT_REPO_URL)
        self.assertEqual(data["branch"], "main")
        self.assertIn("REPLACE-WITH", data["target_dir"])

    def test_local_settings_file_is_gitignored(self):
        if GIT is None:
            self.skipTest("git is not installed")
        done = subprocess.run(["git", "check-ignore", "--no-index", "-q",
                               "config/mirror_main_to_drive.local.json"], cwd=ROOT)
        self.assertEqual(done.returncode, 0)

    def test_runbook_covers_setup_status_and_stop(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        for needle in (mirror.MARKER_NAME, mirror.INFO_NAME, mirror.STATUS_NAME,
                       "Sunday 03:00", "owner OK", "How to stop it", "manual run",
                       "no Claude usage"):
            self.assertIn(needle, text)

    def test_runbook_covers_g2_hardening_notes(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        for needle in ("Viewer", "deleted on the next update", "shortcuts", "previous_run_unfinished",
                       "--status", "first_run_target_not_empty", "My Drive"):
            self.assertIn(needle, text)
        self.assertNotIn("Another run was still going. Nothing was changed.", text)

    def test_new_files_are_plain_ascii(self):
        for path in (SCRIPT, INSTALLER, EXAMPLE_CONFIG, RUNBOOK, Path(__file__)):
            data = path.read_bytes()
            self.assertTrue(all(byte < 128 for byte in data), path.name)


if __name__ == "__main__":
    unittest.main()
