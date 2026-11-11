"""minigit: a small Git implementation in pure Python.

It reads and writes the real on-disk formats (objects, index, refs), so a repository made
with minigit can be opened with git, and the other way round.

    python minigit.py init | add <path>... | status | commit -m <msg> | log
    python minigit.py cat-file (-p|-t) <object> | ls-files | ls-tree <tree> | hash-object [-w] <file>
    python minigit.py explain (index|<object>)     label every byte of the index or of an object
"""
import argparse
import hashlib
import os
import re
import stat
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

SHA_RE = re.compile(r"[0-9a-f]{40}")
ENTRY_HEAD = struct.Struct("!10L20sH")  # ctime, mtime (s+ns each), dev, ino, mode, uid, gid, size, sha, flags


class GitError(Exception):
    pass


@dataclass
class IndexEntry:
    path: str
    sha: str
    mode: int
    size: int = 0
    ctime: tuple = (0, 0)
    mtime: tuple = (0, 0)
    dev: int = 0
    ino: int = 0
    uid: int = 0
    gid: int = 0


def _write_atomic(path, data):
    """Write to a temporary file, then rename. A crash never leaves a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class Repo:
    def __init__(self, root):
        self.root = Path(os.path.abspath(root))
        self.git = self.root / ".git"

    @classmethod
    def find(cls, start="."):
        """Look for .git in `start` and then in each parent directory."""
        current = Path(os.path.abspath(start))
        for folder in (current, *current.parents):
            if (folder / ".git").is_dir():
                return cls(folder)
        raise GitError("not a git repository (or any of the parent directories): .git")

    @classmethod
    def init(cls, root="."):
        repo = cls(root)
        if (repo.git / "HEAD").exists():
            raise GitError(f"repository already exists in {repo.git}")
        for folder in ("objects", "refs/heads", "refs/tags"):
            (repo.git / folder).mkdir(parents=True, exist_ok=True)
        (repo.git / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        (repo.git / "config").write_bytes(
            b"[core]\n\trepositoryformatversion = 0\n\tfilemode = false\n\tbare = false\n")
        return repo

    # ---- objects -----------------------------------------------------------------------

    def hash_object(self, data, kind="blob", write=True):
        """An object's name is the SHA-1 of '<type> <size>\\0<content>'. It is stored zlib-compressed."""
        store = f"{kind} {len(data)}".encode() + b"\0" + data
        sha = hashlib.sha1(store).hexdigest()
        path = self.git / "objects" / sha[:2] / sha[2:]
        if write and not path.exists():  # same content, same name: nothing to do
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(path, zlib.compress(store))
        return sha

    def read_object(self, sha):
        if not SHA_RE.fullmatch(sha):  # also keeps "../.." out of the file path below
            raise GitError(f"not a valid object name: {sha}")
        path = self.git / "objects" / sha[:2] / sha[2:]
        if not path.exists():
            raise GitError(f"object {sha} not found (packed objects are not supported)")
        try:
            raw = zlib.decompress(path.read_bytes())
            header, _, body = raw.partition(b"\0")
            kind, size = header.decode("ascii").split(" ")
            if int(size) != len(body):
                raise ValueError("size mismatch")
        except (zlib.error, ValueError, UnicodeDecodeError) as exc:
            raise GitError(f"object {sha} is corrupt: {exc}") from exc
        return kind, body

    def resolve(self, name):
        """Turn HEAD, a branch name, a full SHA or a unique SHA prefix into a full SHA."""
        if name == "HEAD":
            sha = self.head()[1]
            if sha is None:
                raise GitError("HEAD does not point to a commit yet")
            return sha
        branch = self._read_ref(f"refs/heads/{name}") if re.fullmatch(r"[\w./-]+", name) and ".." not in name else None
        if branch:
            return branch
        if SHA_RE.fullmatch(name):
            return name
        if re.fullmatch(r"[0-9a-f]{4,39}", name):
            folder = self.git / "objects" / name[:2]
            matches = [name[:2] + f.name for f in folder.iterdir() if f.name.startswith(name[2:])] if folder.is_dir() else []
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise GitError(f"short object name {name} is ambiguous")
        raise GitError(f"unknown revision or object: {name}")

    # ---- refs --------------------------------------------------------------------------

    def _read_ref(self, ref):
        path = self.git / ref
        if path.is_file():
            return path.read_text("ascii").strip()
        packed = self.git / "packed-refs"  # git moves old refs into this single file
        if packed.is_file():
            for line in packed.read_text("utf-8").splitlines():
                if line.endswith(" " + ref) and SHA_RE.fullmatch(line[:40]):
                    return line[:40]
        return None

    def head(self):
        """Returns (ref name or None if detached, commit SHA or None before the first commit)."""
        content = (self.git / "HEAD").read_text("ascii").strip()
        if content.startswith("ref: "):
            return content[5:], self._read_ref(content[5:])
        return None, content

    def _update_head(self, sha):
        ref = self.head()[0]
        target = self.git / ref if ref else self.git / "HEAD"
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(target, (sha + "\n").encode())

    # ---- index (staging area) ----------------------------------------------------------

    def read_index(self):
        """Returns {path: IndexEntry}. The index is a sorted binary list of staged files."""
        path = self.git / "index"
        if not path.exists():
            return {}
        data = path.read_bytes()
        if len(data) < 32 or hashlib.sha1(data[:-20]).digest() != data[-20:]:
            raise GitError("index file is corrupt (bad checksum)")
        signature, version, count = struct.unpack("!4sLL", data[:12])
        if signature != b"DIRC" or version != 2:
            raise GitError(f"unsupported index version {version} (only version 2 is supported)")
        entries, pos = {}, 12
        for _ in range(count):
            fields = ENTRY_HEAD.unpack_from(data, pos)
            start = pos + ENTRY_HEAD.size
            length = fields[11] & 0xFFF
            end = data.index(b"\0", start) if length == 0xFFF else start + length  # 0xFFF: name is longer
            name = data[start:end].decode("utf-8")
            entries[name] = IndexEntry(name, fields[10].hex(), fields[6], fields[9], fields[0:2], fields[2:4],
                                       fields[4], fields[5], fields[7], fields[8])
            # Each entry is NUL-padded to a multiple of 8 bytes, with at least one NUL.
            pos += (ENTRY_HEAD.size + (end - start) + 8) // 8 * 8
        return entries  # anything after the entries is an optional extension and is ignored

    def write_index(self, entries):
        body = bytearray(struct.pack("!4sLL", b"DIRC", 2, len(entries)))
        for entry in sorted(entries.values(), key=lambda e: e.path.encode()):
            name = entry.path.encode()
            body += ENTRY_HEAD.pack(*entry.ctime, *entry.mtime, entry.dev, entry.ino, entry.mode, entry.uid,
                                    entry.gid, entry.size, bytes.fromhex(entry.sha), min(len(name), 0xFFF))
            body += name + b"\0" * (8 - (ENTRY_HEAD.size + len(name)) % 8)
        body += hashlib.sha1(body).digest()
        lock = self.git / "index.lock"
        try:
            # Creating the lock file fails if it exists, so two processes cannot both write the index.
            with open(lock, "xb") as handle:
                handle.write(body)
        except FileExistsError:
            raise GitError("index.lock exists: another git process seems to be running") from None
        os.replace(lock, self.git / "index")

    def _relative(self, path):
        absolute = Path(os.path.abspath(path))
        try:
            relative = absolute.relative_to(self.root).as_posix()
        except ValueError:
            raise GitError(f"{path} is outside the repository") from None
        if relative == ".git" or relative.startswith(".git/"):
            raise GitError(f"{path} is inside .git")
        return absolute, relative

    def _walk(self, folder):
        for current, folders, files in os.walk(folder):
            folders[:] = sorted(f for f in folders if f != ".git")
            for name in sorted(files):
                yield Path(current) / name

    def add(self, paths):
        entries = self.read_index()
        for given in paths:
            absolute, relative = self._relative(given)
            if absolute.is_dir():
                files = list(self._walk(absolute))
            elif absolute.is_file():
                files = [absolute]
            else:
                # The path is gone from disk: staging it means staging its deletion.
                gone = [p for p in entries if p == relative or p.startswith(relative + "/")]
                if not gone:
                    raise GitError(f"pathspec '{given}' did not match any files")
                for path in gone:
                    del entries[path]
                continue
            for file in files:
                name = file.relative_to(self.root).as_posix()
                info = file.stat()
                # A path cannot be a file and a directory at once: drop entries that conflict.
                for other in [p for p in entries if p.startswith(name + "/") or name.startswith(p + "/")]:
                    del entries[other]
                executable = os.name != "nt" and info.st_mode & stat.S_IXUSR  # Windows has no executable bit
                entries[name] = IndexEntry(
                    name, self.hash_object(file.read_bytes()), 0o100755 if executable else 0o100644,
                    info.st_size & 0xFFFFFFFF,
                    (int(info.st_ctime) & 0xFFFFFFFF, info.st_ctime_ns % 10**9),
                    (int(info.st_mtime) & 0xFFFFFFFF, info.st_mtime_ns % 10**9))
        self.write_index(entries)

    # ---- trees -------------------------------------------------------------------------

    def write_tree(self, entries=None):
        """Builds one tree object per directory from the flat index and returns the root tree SHA."""
        nested = {}
        for path, entry in (self.read_index() if entries is None else entries).items():
            *folders, name = path.split("/")
            node = nested
            for folder in folders:
                node = node.setdefault(folder, {})
            node[name] = entry

        def build(node):
            rows = []
            for name, value in node.items():
                if isinstance(value, dict):
                    # Git sorts a directory as if its name ended with "/". Sorting plain names
                    # gives a different order for e.g. "a", "a-b", "a.txt" and thus another hash.
                    rows.append((name.encode() + b"/", b"40000", name, build(value)))
                else:
                    rows.append((name.encode(), f"{value.mode:o}".encode(), name, value.sha))
            rows.sort(key=lambda row: row[0])
            return self.hash_object(
                b"".join(mode + b" " + name.encode() + b"\0" + bytes.fromhex(sha) for _, mode, name, sha in rows),
                "tree")

        return build(nested)

    def read_tree(self, sha):
        """Returns [(mode, name, sha)] for one tree object."""
        kind, body = self.read_object(sha)
        if kind != "tree":
            raise GitError(f"{sha} is a {kind}, not a tree")
        rows, pos = [], 0
        while pos < len(body):
            space, nul = body.index(b" ", pos), body.index(b"\0", pos)
            rows.append((body[pos:space].decode(), body[space + 1:nul].decode("utf-8"), body[nul + 1:nul + 21].hex()))
            pos = nul + 21
        return rows

    def flatten_tree(self, sha, prefix=""):
        """Returns {path: (mode, sha)} for every file below a tree."""
        files = {}
        for mode, name, child in self.read_tree(sha):
            if mode == "40000":
                files.update(self.flatten_tree(child, prefix + name + "/"))
            else:
                files[prefix + name] = (int(mode, 8), child)
        return files

    # ---- commits -----------------------------------------------------------------------

    def _identity(self):
        name, email = os.environ.get("GIT_AUTHOR_NAME"), os.environ.get("GIT_AUTHOR_EMAIL")
        for config in (Path.home() / ".gitconfig", self.git / "config"):  # the repository config wins
            if not config.is_file():
                continue
            section = None
            for line in config.read_text("utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("["):
                    section = line.strip("[]").strip().lower()
                elif section == "user" and "=" in line:
                    key, value = (part.strip() for part in line.split("=", 1))
                    if key.lower() == "name":
                        name = os.environ.get("GIT_AUTHOR_NAME") or value
                    elif key.lower() == "email":
                        email = os.environ.get("GIT_AUTHOR_EMAIL") or value
        if not name or not email:
            raise GitError("author unknown: set GIT_AUTHOR_NAME and GIT_AUTHOR_EMAIL, or user.name and user.email")
        return f"{name} <{email}>"

    def read_commit(self, sha):
        kind, body = self.read_object(sha)
        if kind != "commit":
            raise GitError(f"{sha} is a {kind}, not a commit")
        head, _, message = body.decode("utf-8", errors="replace").partition("\n\n")
        commit = {"sha": sha, "parents": [], "message": message}
        for line in head.splitlines():
            key, _, value = line.partition(" ")
            if key == "parent":
                commit["parents"].append(value)
            elif key in ("tree", "author", "committer"):
                commit[key] = value
        return commit

    def commit(self, message, when=None):
        if not message.strip():
            raise GitError("aborting commit due to empty commit message")
        entries = self.read_index()
        tree = self.write_tree(entries)
        parent = self.head()[1]
        if (parent and self.read_commit(parent)["tree"] == tree) or (not parent and not entries):
            raise GitError("nothing to commit")
        when = int(time.time()) if when is None else when
        offset = time.localtime(when).tm_gmtoff // 60
        stamp = f"{when} {'+' if offset >= 0 else '-'}{abs(offset) // 60:02d}{abs(offset) % 60:02d}"
        who = self._identity()
        lines = [f"tree {tree}"] + ([f"parent {parent}"] if parent else [])
        lines += [f"author {who} {stamp}", f"committer {who} {stamp}", "", message.strip() + "\n"]
        sha = self.hash_object("\n".join(lines).encode(), "commit")
        self._update_head(sha)  # the object is on disk before any ref points to it
        return sha

    def log(self, start="HEAD", limit=None):
        """Yields commits from `start` backwards, following first parents."""
        sha = self.head()[1] if start == "HEAD" else self.resolve(start)
        seen = 0
        while sha and (limit is None or seen < limit):
            commit = self.read_commit(sha)
            yield commit
            sha = commit["parents"][0] if commit["parents"] else None
            seen += 1

    # ---- status ------------------------------------------------------------------------

    def status(self):
        """Compares HEAD with the index (staged) and the index with the working tree (unstaged)."""
        head_sha = self.head()[1]
        committed = self.flatten_tree(self.read_commit(head_sha)["tree"]) if head_sha else {}
        index = self.read_index()
        result = {"staged_new": [], "staged_modified": [], "staged_deleted": [],
                  "modified": [], "deleted": [], "untracked": []}
        for path, entry in index.items():
            if path not in committed:
                result["staged_new"].append(path)
            elif committed[path] != (entry.mode, entry.sha):
                result["staged_modified"].append(path)
            file = self.root / path
            if not file.is_file():
                result["deleted"].append(path)
            # shortcut: equal-sized files are always re-hashed. Git first compares the cached
            # mtime/ctime and skips the hash when they match. Add that if status gets slow.
            elif file.stat().st_size != entry.size or self.hash_object(file.read_bytes(), write=False) != entry.sha:
                result["modified"].append(path)
        result["staged_deleted"] = [p for p in committed if p not in index]
        result["untracked"] = [p for p in (f.relative_to(self.root).as_posix() for f in self._walk(self.root))
                               if p not in index]
        return {key: sorted(value) for key, value in result.items()}


def _segments(data, parts):
    """Turns [(length, field, kind, value)] into segments with offsets and checks they cover `data`."""
    segments, offset = [], 0
    for length, name, kind, value in parts:
        if length:
            segments.append({"offset": offset, "length": length, "field": name, "kind": kind, "value": value})
            offset += length
    if offset != len(data):
        raise GitError(f"byte map covers {offset} of {len(data)} bytes")
    return segments


def explain_index(repo):
    """Labels every byte of .git/index. Returns (raw bytes, segments)."""
    path = repo.git / "index"
    if not path.exists():
        raise GitError("there is no index yet: nothing has been staged")
    data = path.read_bytes()
    repo.read_index()  # validates the signature, version and checksum first
    count = struct.unpack("!L", data[8:12])[0]
    parts = [(4, "signature", "meta", data[:4].decode("ascii")),
             (4, "version", "meta", str(struct.unpack("!L", data[4:8])[0])),
             (4, "entry count", "meta", str(count))]
    pos = 12
    for _ in range(count):
        f = ENTRY_HEAD.unpack_from(data, pos)
        start = pos + ENTRY_HEAD.size
        length = f[11] & 0xFFF
        end = data.index(b"\0", start) if length == 0xFFF else start + length
        name = data[start:end].decode("utf-8", errors="replace")
        size = (ENTRY_HEAD.size + (end - start) + 8) // 8 * 8
        stamp = lambda seconds: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))
        parts += [(8, "ctime", "stat", f"{stamp(f[0])}, metadata changed"),
                  (8, "mtime", "stat", f"{stamp(f[2])}, content changed"),
                  (4, "dev", "stat", str(f[4])), (4, "ino", "stat", str(f[5])),
                  (4, "mode", "mode", f"{f[6]:o}, regular file" + (", executable" if f[6] & 0o111 else "")),
                  (4, "uid", "stat", str(f[7])), (4, "gid", "stat", str(f[8])),
                  (4, "size", "stat", f"{f[9]} bytes on disk"),
                  (20, "blob SHA-1", "sha", f[10].hex()),
                  (2, "flags", "meta", f"path length {length}" + (" (longer, NUL-terminated)" if length == 0xFFF else "")),
                  (end - start, "path", "name", name),
                  (pos + size - end, "padding", "pad", f"pads the entry to {size} bytes, a multiple of 8")]
        pos += size
    parts += [(len(data) - 20 - pos, "extensions", "pad", "optional cached data, ignored by minigit"),
              (20, "checksum", "check", f"SHA-1 of everything before it: {data[-20:].hex()}")]
    return data, _segments(data, parts)


def explain_object(repo, sha):
    """Labels every byte of an object as it is stored (before zlib compression)."""
    kind, body = repo.read_object(sha)
    data = f"{kind} {len(body)}".encode() + b"\0" + body
    parts = [(len(kind) + 1, "type", "meta", kind),
             (len(str(len(body))), "content size", "meta", f"{len(body)} bytes"),
             (1, "NUL", "pad", "end of the header")]
    if kind == "tree":
        pos = 0
        while pos < len(body):
            space, nul = body.index(b" ", pos), body.index(b"\0", pos)
            mode, name = body[pos:space].decode(), body[space + 1:nul].decode("utf-8", errors="replace")
            parts += [(space - pos + 1, "mode", "mode", mode + (", directory" if mode == "40000" else ", file")),
                      (nul - space, "name", "name", name),
                      (20, "SHA-1 (raw, not hex)", "sha", body[nul + 1:nul + 21].hex())]
            pos = nul + 21
    elif kind == "commit":
        head, _, _ = body.partition(b"\n\n")
        pos = 0
        for line in head.split(b"\n"):
            key = line.split(b" ", 1)[0].decode("ascii", errors="replace")
            value = line.split(b" ", 1)[-1].decode("utf-8", errors="replace")
            parts.append((len(line) + 1, key, "sha" if key in ("tree", "parent") else "name", value))
            pos += len(line) + 1
        parts += [(1, "blank line", "pad", "separates the headers from the message"),
                  (len(body) - pos - 1, "message", "text", body[pos + 1:].decode("utf-8", errors="replace").strip())]
    else:
        parts.append((len(body), "content", "text", f"{len(body)} bytes of file content"))
    return data, _segments(data, parts)


def _format_time(stamp):
    seconds, zone = stamp.split(" ")
    offset = (int(zone[1:3]) * 60 + int(zone[3:5])) * 60 * (-1 if zone[0] == "-" else 1)
    return time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(int(seconds) + offset)) + " " + zone


def main(argv=None):
    parser = argparse.ArgumentParser(prog="minigit", description="A small Git implementation.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("add").add_argument("paths", nargs="+")
    sub.add_parser("status")
    sub.add_parser("commit").add_argument("-m", "--message", required=True)
    sub.add_parser("log")
    sub.add_parser("ls-files")
    sub.add_parser("write-tree")
    sub.add_parser("ls-tree").add_argument("object")
    sub.add_parser("explain").add_argument("target", help="'index' or an object name")
    cat = sub.add_parser("cat-file")
    mode = cat.add_mutually_exclusive_group(required=True)
    mode.add_argument("-p", action="store_const", const="-p", dest="mode", help="print the content")
    mode.add_argument("-t", action="store_const", const="-t", dest="mode", help="print the type")
    cat.add_argument("object")
    hasher = sub.add_parser("hash-object")
    hasher.add_argument("-w", action="store_true", dest="write")
    hasher.add_argument("file")
    args = parser.parse_args(argv)
    out = sys.stdout

    try:
        if args.command == "init":
            print(f"Initialized empty Git repository in {Repo.init().git}")
            return 0
        repo = Repo.find()
        if args.command == "add":
            repo.add(args.paths)
        elif args.command == "commit":
            sha = repo.commit(args.message)
            branch = (repo.head()[0] or "detached HEAD").removeprefix("refs/heads/")
            print(f"[{branch} {sha[:7]}] {args.message.strip().splitlines()[0]}")
        elif args.command == "log":
            for commit in repo.log():
                author, stamp = commit["author"].rsplit(" ", 2)[0], " ".join(commit["author"].rsplit(" ", 2)[1:])
                print(f"commit {commit['sha']}\nAuthor: {author}\nDate:   {_format_time(stamp)}\n")
                print("".join(f"    {line}\n" for line in commit["message"].rstrip("\n").split("\n")))
        elif args.command == "status":
            status = repo.status()
            branch = (repo.head()[0] or "detached HEAD").removeprefix("refs/heads/")
            print(f"On branch {branch}")
            groups = [("Changes to be committed:", [("new file", "staged_new"), ("modified", "staged_modified"),
                                                    ("deleted", "staged_deleted")]),
                      ("Changes not staged for commit:", [("modified", "modified"), ("deleted", "deleted")]),
                      ("Untracked files:", [("", "untracked")])]
            clean = True
            for title, kinds in groups:
                lines = [f"\t{label + ':':<12}{path}" if label else f"\t{path}"
                         for label, key in kinds for path in status[key]]
                if lines:
                    clean = False
                    print("\n" + title + "\n" + "\n".join(lines))
            if clean:
                print("nothing to commit, working tree clean")
        elif args.command == "ls-files":
            for entry in repo.read_index().values():
                print(f"{entry.mode:o} {entry.sha} 0\t{entry.path}")
        elif args.command == "write-tree":
            print(repo.write_tree())
        elif args.command == "ls-tree":
            sha = repo.resolve(args.object)
            if repo.read_object(sha)[0] == "commit":
                sha = repo.read_commit(sha)["tree"]
            for mode, name, child in repo.read_tree(sha):
                print(f"{int(mode):06d} {'tree' if mode == '40000' else 'blob'} {child}\t{name}")
        elif args.command == "explain":
            data, segments = (explain_index(repo) if args.target == "index"
                              else explain_object(repo, repo.resolve(args.target)))
            for segment in segments:
                raw = data[segment["offset"]:segment["offset"] + segment["length"]]
                shown = raw[:12].hex(" ") + (" .." if len(raw) > 12 else "")
                print(f"{segment['offset']:>6}  {segment['length']:>5}  {segment['field']:<22} {shown:<39} {segment['value']}")
        elif args.command == "cat-file":
            sha = repo.resolve(args.object)
            kind, body = repo.read_object(sha)
            if args.mode == "-t":
                print(kind)
            elif kind == "tree":
                for mode, name, child in repo.read_tree(sha):
                    print(f"{int(mode):06d} {'tree' if mode == '40000' else 'blob'} {child}\t{name}")
            else:
                out.flush()
                out.buffer.write(body)
        elif args.command == "hash-object":
            print(repo.hash_object(Path(args.file).read_bytes(), write=args.write))
    except (GitError, OSError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 128
    return 0


if __name__ == "__main__":
    sys.exit(main())
