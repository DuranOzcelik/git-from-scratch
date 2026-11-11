"""Repository explorer: shows how commits, trees and blobs link together.

    python web.py [repository path] [port]   ->  http://127.0.0.1:8054
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from minigit import GitError, Repo, explain_index, explain_object

INDEX = Path(__file__).with_name("index.html")
MAX_TEXT = 20_000
MAX_BYTES = 2048


def byte_map(repo, target):
    data, segments = explain_index(repo) if target == "index" else explain_object(repo, repo.resolve(target))
    shown = [s for s in segments if s["offset"] < MAX_BYTES]
    return {"target": target, "size": len(data), "hex": data[:MAX_BYTES].hex(), "segments": shown,
            "truncated": len(data) > MAX_BYTES}


def repo_summary(repo):
    ref, sha = repo.head()
    commits = []
    for commit in repo.log(limit=200) if sha else []:
        author, seconds, _zone = commit["author"].rsplit(" ", 2)
        commits.append({"sha": commit["sha"], "tree": commit["tree"], "parents": commit["parents"],
                        "author": author.split(" <")[0], "subject": commit["message"].split("\n")[0],
                        "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(int(seconds)))})
    index = [{"mode": f"{e.mode:o}", "sha": e.sha, "path": e.path, "size": e.size}
             for e in repo.read_index().values()]
    return {"name": repo.root.name, "ref": ref, "head": sha, "commits": commits,
            "index": index, "status": repo.status()}


def object_detail(repo, name):
    sha = repo.resolve(name)
    kind, body = repo.read_object(sha)
    stored = repo.git / "objects" / sha[:2] / sha[2:]
    detail = {"sha": sha, "type": kind, "size": len(body), "compressed": stored.stat().st_size,
              "file": f".git/objects/{sha[:2]}/{sha[2:]}"}
    if kind == "commit":
        detail["commit"] = repo.read_commit(sha)
        detail["text"] = body.decode("utf-8", errors="replace")
    elif kind == "tree":
        detail["entries"] = [{"mode": mode.zfill(6), "type": "tree" if mode == "40000" else "blob",
                              "name": entry, "sha": child} for mode, entry, child in repo.read_tree(sha)]
    elif b"\0" in body[:8000]:
        detail["binary"] = True
    else:
        detail["text"] = body[:MAX_TEXT].decode("utf-8", errors="replace")
        detail["truncated"] = len(body) > MAX_TEXT
    return detail


class Handler(BaseHTTPRequestHandler):
    repo = None

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        try:
            if url.path == "/api/repo":
                body = repo_summary(self.repo)
            elif url.path == "/api/bytes":
                body = byte_map(self.repo, parse_qs(url.query).get("of", [""])[0])
            elif url.path == "/api/object":
                body = object_detail(self.repo, parse_qs(url.query).get("sha", [""])[0])
            else:
                return self.send_error(404)
        except GitError as exc:
            return self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
        self._send(200, json.dumps(body).encode(), "application/json")

    def _send(self, status, payload, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    try:
        Handler.repo = Repo.find(sys.argv[1] if len(sys.argv) > 1 else ".")
    except GitError as exc:
        sys.exit(f"fatal: {exc}")
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8054
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # loopback only: it exposes file contents
    print(f"Exploring {Handler.repo.root}\nhttp://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
