"""Tests. Where possible the result is checked against the real git binary."""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import minigit
from minigit import GitError, Repo

GIT = shutil.which("git")
IDENTITY = {"GIT_AUTHOR_NAME": "Test User", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test User", "GIT_COMMITTER_EMAIL": "test@example.com"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = unittest.mock.patch.dict(os.environ, IDENTITY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = Repo.init(self.root)

    def write(self, path, content):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content.encode() if isinstance(content, str) else content)
        return file

    def add(self, *paths):
        self.repo.add([self.root / path for path in paths])

    def git(self, *args):
        """Runs the real git in the test repository with a neutral configuration."""
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        result = subprocess.run([GIT, "-c", "core.autocrlf=false", "-c", "core.fileMode=false", *args],
                                cwd=self.root, env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout


class Objects(Base):
    def test_known_hashes(self):
        self.assertEqual(self.repo.hash_object(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")
        self.assertEqual(self.repo.hash_object(b""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
        self.assertEqual(self.repo.write_tree({}), "4b825dc642cb6eb9a060e54bf8d69288fbee4904")

    def test_round_trip_and_prefix(self):
        sha = self.repo.hash_object(b"\x00binary\xff\r\n")
        self.assertEqual(self.repo.read_object(sha), ("blob", b"\x00binary\xff\r\n"))
        self.assertEqual(self.repo.resolve(sha[:7]), sha)

    def test_rejects_bad_names_and_corruption(self):
        for bad in ("../../../etc/passwd", "zz" * 20, "abc"):
            with self.assertRaises(GitError):
                self.repo.read_object(bad)
        sha = self.repo.hash_object(b"data")
        (self.repo.git / "objects" / sha[:2] / sha[2:]).write_bytes(b"not zlib")
        with self.assertRaises(GitError):
            self.repo.read_object(sha)


class Index(Base):
    def test_round_trip_for_every_padding_length(self):
        for length in range(1, 20):  # the padding depends on the name length modulo 8
            self.write("x" * length, f"{length}")
        self.add(".")
        entries = self.repo.read_index()
        self.assertEqual(sorted(entries), sorted("x" * n for n in range(1, 20)))
        self.assertEqual(entries["xxx"].sha, self.repo.hash_object(b"3"))

    def test_detects_corruption(self):
        self.write("a.txt", "a")
        self.add("a.txt")
        index = self.repo.git / "index"
        data = bytearray(index.read_bytes())
        data[20] ^= 0xFF
        index.write_bytes(data)
        with self.assertRaises(GitError):
            self.repo.read_index()

    def test_stale_lock_blocks_writes(self):
        (self.repo.git / "index.lock").write_bytes(b"")
        self.write("a.txt", "a")
        with self.assertRaises(GitError):
            self.add("a.txt")

    def test_add_stages_deletion_and_replaces_file_with_directory(self):
        self.write("a", "file")
        self.write("gone.txt", "x")
        self.add(".")
        (self.root / "gone.txt").unlink()
        (self.root / "a").unlink()
        self.write("a/b.txt", "now a directory")
        self.add("gone.txt", "a")
        self.assertEqual(sorted(self.repo.read_index()), ["a/b.txt"])

    def test_rejects_paths_outside_repository_and_inside_dot_git(self):
        for bad in (self.root.parent / "elsewhere.txt", self.root / ".git" / "config", self.root / "missing.txt"):
            with self.assertRaises(GitError):
                self.add(bad)


class Commits(Base):
    def test_commit_log_and_nothing_to_commit(self):
        with self.assertRaises(GitError):
            self.repo.commit("empty")
        self.write("a.txt", "one\n")
        self.add("a.txt")
        first = self.repo.commit("first")
        with self.assertRaises(GitError):
            self.repo.commit("same tree again")
        self.write("a.txt", "two\n")
        self.add("a.txt")
        second = self.repo.commit("second\n\nwith a body")
        log = list(self.repo.log())
        self.assertEqual([c["sha"] for c in log], [second, first])
        self.assertEqual(log[0]["parents"], [first])
        self.assertEqual(log[0]["message"], "second\n\nwith a body\n")
        self.assertEqual(self.repo.resolve("main"), second)

    def test_status_categories(self):
        for name in ("keep.txt", "edit.txt", "remove.txt", "restage.txt"):
            self.write(name, name)
        self.add(".")
        self.repo.commit("base")
        self.write("edit.txt", "changed on disk only")
        (self.root / "remove.txt").unlink()
        self.write("restage.txt", "changed and staged")
        self.write("new.txt", "staged new file")
        self.add("restage.txt", "new.txt")
        self.write("untracked.txt", "?")
        status = self.repo.status()
        self.assertEqual(status, {"staged_new": ["new.txt"], "staged_modified": ["restage.txt"],
                                  "staged_deleted": [], "modified": ["edit.txt"], "deleted": ["remove.txt"],
                                  "untracked": ["untracked.txt"]})
        self.add("remove.txt")
        self.assertEqual(self.repo.status()["staged_deleted"], ["remove.txt"])

    def test_same_size_edit_is_detected(self):
        self.write("a.txt", "aaaa")
        self.add("a.txt")
        self.repo.commit("base")
        self.write("a.txt", "bbbb")
        self.assertEqual(self.repo.status()["modified"], ["a.txt"])


@unittest.skipUnless(GIT, "git is not installed")
class AgainstRealGit(Base):
    def populate(self):
        # "a-b" < "a.txt" < "a/" in git's tree order, although "a" < "a-b" as plain strings.
        for path in ("a/inner.txt", "a-b", "a.txt", "src/deep/er/file.py", "README.md", "ünïcödé.txt"):
            self.write(path, f"content of {path}\n")
        self.write("binary.bin", bytes(range(256)))
        self.add(".")

    def test_git_accepts_our_repository(self):
        self.populate()
        first = self.repo.commit("first commit")
        self.write("a/inner.txt", "changed\n")
        self.add("a")
        second = self.repo.commit("second commit")

        self.git("fsck", "--strict")
        self.assertEqual(self.git("log", "--format=%H").split(), [second, first])
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("log", "-1", "--format=%an <%ae>|%s").strip(),
                         "Test User <test@example.com>|second commit")
        self.assertEqual(self.git("cat-file", "-p", "HEAD:a/inner.txt"), "changed\n")

    def test_tree_and_index_match_git(self):
        self.populate()
        ours = self.repo.write_tree()
        self.assertEqual(self.git("write-tree").strip(), ours)
        self.assertEqual(self.git("-c", "core.quotePath=false", "ls-files", "-s").splitlines(),
                         [f"{e.mode:o} {e.sha} 0\t{e.path}" for e in self.repo.read_index().values()])

    def test_we_can_read_a_repository_made_by_git(self):
        shutil.rmtree(self.repo.git)
        self.git("init", "-q", "-b", "trunk")
        self.write("docs/guide.md", "# guide\n")
        self.write("main.py", "print('hi')\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "made by git")
        self.write("main.py", "print('changed')\n")
        self.write("new.txt", "x")

        repo = Repo.find(self.root / "docs")
        self.assertEqual(repo.head()[0], "refs/heads/trunk")
        self.assertEqual([c["message"] for c in repo.log()], ["made by git\n"])
        self.assertEqual(sorted(repo.read_index()), ["docs/guide.md", "main.py"])
        status = repo.status()
        self.assertEqual((status["modified"], status["untracked"]), (["main.py"], ["new.txt"]))
        tree = repo.read_commit(repo.resolve("HEAD"))["tree"]
        self.assertEqual(sorted(repo.flatten_tree(tree)), ["docs/guide.md", "main.py"])


class ByteMaps(Base):
    def assert_covers(self, data, segments):
        """Every byte belongs to exactly one segment: no gaps, no overlaps."""
        position = 0
        for segment in segments:
            self.assertEqual(segment["offset"], position)
            self.assertGreater(segment["length"], 0)
            position += segment["length"]
        self.assertEqual(position, len(data))

    def test_index_map_labels_every_byte(self):
        for name in ("a", "bb.txt", "dir/a-longer-name.py", "x" * 23):
            self.write(name, name)
        self.add(".")
        data, segments = minigit.explain_index(self.repo)
        self.assert_covers(data, segments)
        self.assertEqual(data, (self.repo.git / "index").read_bytes())
        paths = [s["value"] for s in segments if s["field"] == "path"]
        self.assertEqual(paths, sorted(self.repo.read_index()))
        for segment in (s for s in segments if s["field"] == "padding"):
            raw = data[segment["offset"]:segment["offset"] + segment["length"]]
            self.assertEqual(raw, b"\0" * len(raw))
            self.assertTrue(1 <= len(raw) <= 8)
        shas = [s["value"] for s in segments if s["field"] == "blob SHA-1"]
        self.assertEqual(shas, [e.sha for e in self.repo.read_index().values()])

    def test_object_maps_label_every_byte(self):
        self.write("src/main.py", "print('hi')\n")
        self.write("README.md", "# readme\n")
        self.add(".")
        commit = self.repo.commit("first\n\nbody line")
        tree = self.repo.read_commit(commit)["tree"]
        blob = self.repo.read_index()["README.md"].sha
        for sha in (commit, tree, blob):
            data, segments = minigit.explain_object(self.repo, sha)
            self.assert_covers(data, segments)
        _, segments = minigit.explain_object(self.repo, tree)
        self.assertEqual([s["value"] for s in segments if s["field"] == "name"], ["README.md", "src"])
        self.assertEqual([s["value"] for s in segments if s["kind"] == "sha"],
                         [child for _, _, child in self.repo.read_tree(tree)])
        _, segments = minigit.explain_object(self.repo, commit)
        self.assertEqual(segments[3]["value"], tree)
        self.assertEqual(segments[-1]["value"], "first\n\nbody line")

    def test_index_map_needs_an_index(self):
        with self.assertRaises(GitError):
            minigit.explain_index(self.repo)


class CommandLine(Base):
    def test_commands_and_exit_codes(self):
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        silence = contextlib.ExitStack()  # keep the commands' own output out of the test report
        self.addCleanup(silence.close)
        silence.enter_context(contextlib.redirect_stdout(io.StringIO()))
        silence.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.write("a.txt", "a\n")
        self.assertEqual(minigit.main(["add", "a.txt"]), 0)
        self.assertEqual(minigit.main(["commit", "-m", "from the cli"]), 0)
        self.assertEqual(minigit.main(["status"]), 0)
        self.assertEqual(minigit.main(["log"]), 0)
        self.assertEqual(minigit.main(["cat-file", "-t", "HEAD"]), 0)
        self.assertEqual(minigit.main(["explain", "index"]), 0)
        self.assertEqual(minigit.main(["explain", "HEAD"]), 0)
        self.assertEqual(minigit.main(["commit", "-m", "nothing changed"]), 128)
        self.assertEqual(minigit.main(["add", "missing.txt"]), 128)


if __name__ == "__main__":
    unittest.main()
