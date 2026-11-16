#!/usr/bin/env python3
"""Lumen Forge — a standalone, GitHub-compatible git forge.

One process, standard library only. It serves four surfaces from its own
in-memory store:

  * git smart HTTP at /{owner}/{repo}.git (upload-pack protocol v2 with a
    v0 fallback, receive-pack v0),
  * the REST v3 API under /api/v3 (repositories, branches, commits,
    contents, labels, issues, comments, pull requests, merges),
  * the GraphQL v4 API at /api/graphql,
  * a Model Context Protocol endpoint at /mcp (JSON-RPC 2.0).

Determinism: a trial runs the same program twice (live, then replay) and
the state the suite exports must match byte for byte, so nothing here
reads the wall clock or an unseeded random source. Every timestamp comes
from a logical clock seeded by MAKEFUN_TRIAL_NOW; every id comes from a
counter. The one credential is the bearer token in MAKEFUN_SHOP_TOKEN;
the token's owner is the contest's fixed viewer login.
"""

import base64
import hashlib
import json
import os
import re
import struct
import sys
import threading
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# --------------------------------------------------------------------------
# configuration and the deterministic clock
# --------------------------------------------------------------------------

TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")

OWNER_LOGIN = "adaiversen"          # the token owner (the contest's viewer)
OWNER_ID = 1000
OWNER_NAME = "ada iversen"
OWNER_EMAIL = "adaiversen@example.invalid"

API_VERSION = "2022-11-28"
INSTALLED_VERSION = "Lumen Forge 1.0 (github-core-clone-01)"


def _trial_now() -> int:
    """MAKEFUN_TRIAL_NOW as epoch seconds; the scenario epoch otherwise."""
    raw = os.environ.get("MAKEFUN_TRIAL_NOW", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            from datetime import datetime

            text = raw.replace("Z", "+00:00")
            moment = datetime.fromisoformat(text)
            return int(moment.timestamp())
        except Exception:
            pass
    return 1_735_689_600  # 2025-01-01T00:00:00Z, the scenario's day one


class Clock:
    """A logical clock: starts at the trial's opening time and advances one
    second per tick, so creation order and timestamps always agree and a
    replay reproduces them exactly."""

    def __init__(self) -> None:
        self._now = _trial_now()
        self._lock = threading.Lock()

    def tick(self) -> int:
        with self._lock:
            self._now += 1
            return self._now

    def peek(self) -> int:
        return self._now


CLOCK = Clock()


def iso(epoch: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_stamp(epoch: int) -> str:
    return f"{epoch} +0000"


def viewer_ident(epoch: int) -> str:
    return f"{OWNER_NAME} <{OWNER_EMAIL}> {git_stamp(epoch)}"


# --------------------------------------------------------------------------
# the git object store
# --------------------------------------------------------------------------

ZERO_SHA = "0" * 40


def hash_object(obj_type: str, content: bytes) -> str:
    header = f"{obj_type} {len(content)}\0".encode()
    return hashlib.sha1(header + content).hexdigest()


class Store:
    """Content-addressed git objects, shared by every repository (objects
    are immutable, so sharing is safe and keeps clones cheap)."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[str, bytes]] = {}
        self.lock = threading.RLock()

    def put(self, obj_type: str, content: bytes) -> str:
        sha = hash_object(obj_type, content)
        with self.lock:
            self.objects.setdefault(sha, (obj_type, content))
        return sha

    def get(self, sha: str) -> tuple[str, bytes] | None:
        return self.objects.get(sha)

    def has(self, sha: str) -> bool:
        return sha in self.objects

    def type_of(self, sha: str) -> str | None:
        entry = self.objects.get(sha)
        return entry[0] if entry else None


STORE = Store()


def parse_tree(content: bytes) -> list[tuple[str, str, str]]:
    """Tree bytes -> [(mode, name, sha hex)], in stored order."""
    entries = []
    pos = 0
    while pos < len(content):
        space = content.index(b" ", pos)
        mode = content[pos:space].decode()
        nul = content.index(b"\0", space)
        name = content[space + 1 : nul].decode()
        sha = content[nul + 1 : nul + 21].hex()
        entries.append((mode, name, sha))
        pos = nul + 21
    return entries


def build_tree(entries: list[tuple[str, str, str]]) -> bytes:
    """[(mode, name, sha hex)] -> tree bytes, git's sort order (a directory
    sorts as if its name ended in '/')."""
    def sort_key(entry: tuple[str, str, str]) -> str:
        mode, name, _ = entry
        return name + "/" if mode == "40000" else name

    out = b""
    for mode, name, sha in sorted(entries, key=sort_key):
        out += f"{mode} {name}".encode() + b"\0" + bytes.fromhex(sha)
    return out


def parse_commit(content: bytes) -> dict:
    headers, _, message = content.partition(b"\n\n")
    parsed: dict = {"parents": [], "message": message.decode("utf-8", "replace")}
    for line in headers.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition(" ")
        if key == "parent":
            parsed["parents"].append(value)
        elif key in ("tree", "author", "committer"):
            parsed[key] = value
    return parsed


def build_commit(tree: str, parents: list[str], author: str, committer: str, message: str) -> bytes:
    lines = [f"tree {tree}"]
    lines.extend(f"parent {parent}" for parent in parents)
    lines.append(f"author {author}")
    lines.append(f"committer {committer}")
    return ("\n".join(lines) + "\n\n" + message).encode()


def parse_tag(content: bytes) -> dict:
    headers, _, message = content.partition(b"\n\n")
    parsed: dict = {"message": message.decode("utf-8", "replace")}
    for line in headers.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition(" ")
        if key in ("object", "type", "tag", "tagger"):
            parsed[key] = value
    return parsed


def flatten_tree(tree_sha: str, prefix: str = "") -> dict[str, tuple[str, str]]:
    """A tree, recursively, as {path: (mode, blob sha)}."""
    flat: dict[str, tuple[str, str]] = {}
    entry = STORE.get(tree_sha)
    if entry is None or entry[0] != "tree":
        return flat
    for mode, name, sha in parse_tree(entry[1]):
        path = f"{prefix}{name}"
        if mode == "40000":
            flat.update(flatten_tree(sha, path + "/"))
        else:
            flat[path] = (mode, sha)
    return flat


def build_tree_from_flat(flat: dict[str, tuple[str, str]]) -> str:
    """The inverse of flatten_tree: nested trees for {path: (mode, sha)},
    stored bottom-up; returns the root tree's sha (the empty tree's when
    the map is empty)."""
    # Group paths by their directory.
    dirs: dict[str, dict[str, tuple[str, str]]] = {"": {}}
    subdirs: dict[str, set[str]] = {"": set()}
    for path, value in flat.items():
        parts = path.split("/")
        for i in range(1, len(parts)):
            directory = "/".join(parts[:i])
            dirs.setdefault(directory, {})
            subdirs.setdefault(directory, set())
            parent = "/".join(parts[: i - 1])
            subdirs.setdefault(parent, set()).add(parts[i - 1])
        dirs["/".join(parts[:-1])][parts[-1]] = value

    def write_tree(directory: str) -> str:
        entries: list[tuple[str, str, str]] = []
        for name, (mode, sha) in dirs.get(directory, {}).items():
            entries.append((mode, name, sha))
        for name in subdirs.get(directory, set()):
            child = f"{directory}/{name}" if directory else name
            entries.append(("40000", name, write_tree(child)))
        return STORE.put("tree", build_tree(entries))

    return write_tree("")


def commit_tree_sha(commit_sha: str) -> str | None:
    entry = STORE.get(commit_sha)
    if entry is None or entry[0] != "commit":
        return None
    return parse_commit(entry[1])["tree"]


def ancestors_of(tip: str) -> set[str]:
    """Every commit reachable from tip (tip included)."""
    seen: set[str] = set()
    stack = [tip]
    while stack:
        sha = stack.pop()
        if sha in seen:
            continue
        entry = STORE.get(sha)
        if entry is None or entry[0] != "commit":
            continue
        seen.add(sha)
        stack.extend(parse_commit(entry[1])["parents"])
    return seen


def is_ancestor(maybe_ancestor: str, tip: str) -> bool:
    return maybe_ancestor in ancestors_of(tip)


def merge_base(a: str, b: str) -> str | None:
    """A best common ancestor of two commits (the first of b's ancestors,
    breadth-first, that is also an ancestor of a)."""
    if a == b:
        return a
    ancestors_a = ancestors_of(a)
    seen: set[str] = set()
    queue = [b]
    while queue:
        sha = queue.pop(0)
        if sha in seen:
            continue
        seen.add(sha)
        if sha in ancestors_a:
            return sha
        entry = STORE.get(sha)
        if entry is not None and entry[0] == "commit":
            queue.extend(parse_commit(entry[1])["parents"])
    return None


def peel_to_commit(sha: str) -> str | None:
    """Tags peel to the commit they name; a commit peels to itself."""
    seen = set()
    while sha not in seen:
        seen.add(sha)
        entry = STORE.get(sha)
        if entry is None:
            return None
        if entry[0] == "commit":
            return sha
        if entry[0] == "tag":
            sha = parse_tag(entry[1]).get("object", "")
            continue
        return None
    return None


# --------------------------------------------------------------------------
# the forge's data model
# --------------------------------------------------------------------------


class Repo:
    def __init__(self, repo_id: int, name: str, private: bool, description: str, now: int) -> None:
        self.id = repo_id
        self.name = name
        self.private = private
        self.description = description
        self.default_branch = "main"
        self.refs: dict[str, str] = {}          # "refs/heads/main" -> commit sha
        self.created_at = now
        self.updated_at = now
        self.next_number = 1                    # issues and pulls share a sequence
        self.issues: dict[int, Issue] = {}
        self.pulls: dict[int, Pull] = {}
        self.labels: dict[str, Label] = {}
        self.next_comment_id = 1

    @property
    def full_name(self) -> str:
        return f"{OWNER_LOGIN}/{self.name}"

    def tip(self, branch: str) -> str | None:
        return self.refs.get(f"refs/heads/{branch}")

    def head_sha(self) -> str | None:
        return self.refs.get(f"refs/heads/{self.default_branch}")


class Label:
    def __init__(self, label_id: int, name: str, color: str, description: str) -> None:
        self.id = label_id
        self.name = name
        self.color = color
        self.description = description


class Issue:
    def __init__(self, issue_id: int, number: int, title: str, body: str,
                 labels: list[str], now: int) -> None:
        self.id = issue_id
        self.number = number
        self.title = title
        self.body = body
        self.state = "open"                     # open | closed
        self.labels = labels                    # label names
        self.created_at = now
        self.updated_at = now
        self.closed_at: int | None = None
        self.comments: list[Comment] = []
        self.is_pull = False


class Pull:
    def __init__(self, pull_id: int, number: int, title: str, body: str,
                 head_ref: str, head_sha: str, base_ref: str, base_sha: str,
                 now: int) -> None:
        self.id = pull_id
        self.number = number
        self.title = title
        self.body = body
        self.state = "open"                     # open | closed
        self.head_ref = head_ref                # branch name
        self.head_sha = head_sha
        self.base_ref = base_ref
        self.base_sha = base_sha
        self.created_at = now
        self.updated_at = now
        self.merged = False
        self.merged_at: int | None = None
        self.merge_commit_sha: str | None = None
        self.comments: list[Comment] = []


class Comment:
    def __init__(self, comment_id: int, body: str, now: int) -> None:
        self.id = comment_id
        self.body = body
        self.created_at = now
        self.updated_at = now


class Forge:
    """All mutable state, behind one lock: the suite drives the forge one
    request at a time in a fixed order, and a single lock keeps concurrent
    workload connections from interleaving half-finished writes."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.repos: dict[str, Repo] = {}        # name -> Repo (the viewer owns all)
        self.next_repo_id = 5001
        self.next_issue_id = 9001
        self.next_label_id = 7001
        self.next_comment_id = 8001
        self.request_counter = 0

    def request_id(self) -> str:
        with self.lock:
            self.request_counter += 1
            return f"lumen-{self.request_counter:06d}"

    def create_repo(self, name: str, private: bool, description: str) -> Repo:
        now = CLOCK.tick()
        repo = Repo(self.next_repo_id, name, private, description, now)
        self.next_repo_id += 1
        self.repos[name] = repo
        return repo


FORGE = Forge()

# --------------------------------------------------------------------------
# REST serializers (GitHub's shapes)
# --------------------------------------------------------------------------


def user_json() -> dict:
    return {
        "login": OWNER_LOGIN,
        "id": OWNER_ID,
        "type": "User",
        "name": OWNER_NAME,
        "email": OWNER_EMAIL,
    }


def repo_json(repo: Repo, base: str) -> dict:
    return {
        "id": repo.id,
        "node_id": global_id("Repository", repo.id),
        "name": repo.name,
        "full_name": repo.full_name,
        "private": repo.private,
        "owner": user_json(),
        "description": repo.description,
        "fork": False,
        "url": f"{base}/api/v3/repos/{repo.full_name}",
        "html_url": f"{base}/{repo.full_name}",
        "clone_url": f"{base}/{repo.full_name}.git",
        "git_url": f"git://{base.split('://', 1)[-1]}/{repo.full_name}.git",
        "ssh_url": f"git@{base.split('://', 1)[-1]}:{repo.full_name}.git",
        "default_branch": repo.default_branch,
        "created_at": iso(repo.created_at),
        "updated_at": iso(repo.updated_at),
        "pushed_at": iso(repo.updated_at),
        "size": 0,
        "stargazers_count": 0,
        "watchers_count": 0,
        "forks_count": 0,
        "open_issues_count": sum(1 for i in repo.issues.values() if i.state == "open"),
        "has_issues": True,
        "has_projects": True,
        "has_wiki": True,
        "has_pages": False,
        "has_downloads": True,
        "archived": False,
        "disabled": False,
        "visibility": "private" if repo.private else "public",
        "license": {"key": "mit", "name": "MIT License", "spdx_id": "MIT"},
    }


def label_json(label: Label, base: str, repo: Repo) -> dict:
    return {
        "id": label.id,
        "node_id": global_id("Label", label.id),
        "name": label.name,
        "color": label.color,
        "description": label.description,
        "default": False,
        "url": f"{base}/api/v3/repos/{repo.full_name}/labels/{label.name}",
    }


def issue_json(repo: Repo, issue: Issue, base: str) -> dict:
    body = {
        "id": issue.id,
        "node_id": global_id("Issue", issue.id),
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "user": user_json(),
        "labels": [label_json(repo.labels[name], base, repo) for name in issue.labels
                   if name in repo.labels],
        "assignee": None,
        "assignees": [],
        "comments": len(issue.comments),
        "created_at": iso(issue.created_at),
        "updated_at": iso(issue.updated_at),
        "closed_at": iso(issue.closed_at) if issue.closed_at else None,
        "url": f"{base}/api/v3/repos/{repo.full_name}/issues/{issue.number}",
        "html_url": f"{base}/{repo.full_name}/issues/{issue.number}",
        "comments_url": f"{base}/api/v3/repos/{repo.full_name}/issues/{issue.number}/comments",
        "labels_url": f"{base}/api/v3/repos/{repo.full_name}/issues/{issue.number}/labels",
    }
    if issue.is_pull:
        body["pull_request"] = {
            "url": f"{base}/api/v3/repos/{repo.full_name}/pulls/{issue.number}",
            "html_url": f"{base}/{repo.full_name}/pull/{issue.number}",
        }
    return body


def pull_json(repo: Repo, pull: Pull, base: str) -> dict:
    return {
        "id": pull.id,
        "node_id": global_id("PullRequest", pull.id),
        "number": pull.number,
        "title": pull.title,
        "body": pull.body,
        "state": pull.state,
        "user": user_json(),
        "labels": [],
        "created_at": iso(pull.created_at),
        "updated_at": iso(pull.updated_at),
        "closed_at": iso(pull.merged_at) if pull.state == "closed" else None,
        "merged_at": iso(pull.merged_at) if pull.merged_at else None,
        "merged": pull.merged,
        "merge_commit_sha": pull.merge_commit_sha,
        "head": {
            "label": f"{OWNER_LOGIN}:{pull.head_ref}",
            "ref": pull.head_ref,
            "sha": repo.tip(pull.head_ref) or pull.head_sha,
            "user": user_json(),
            "repo": repo_json(repo, base),
        },
        "base": {
            "label": f"{OWNER_LOGIN}:{pull.base_ref}",
            "ref": pull.base_ref,
            "sha": repo.tip(pull.base_ref) or pull.base_sha,
            "user": user_json(),
            "repo": repo_json(repo, base),
        },
        "url": f"{base}/api/v3/repos/{repo.full_name}/pulls/{pull.number}",
        "html_url": f"{base}/{repo.full_name}/pull/{pull.number}",
        "comments_url": f"{base}/api/v3/repos/{repo.full_name}/issues/{pull.number}/comments",
        "commits_url": f"{base}/api/v3/repos/{repo.full_name}/pulls/{pull.number}/commits",
        "mergeable": None,
        "draft": False,
    }


def comment_json(repo: Repo, issue_number: int, comment: Comment, base: str) -> dict:
    return {
        "id": comment.id,
        "node_id": global_id("IssueComment", comment.id),
        "body": comment.body,
        "user": user_json(),
        "created_at": iso(comment.created_at),
        "updated_at": iso(comment.updated_at),
        "url": f"{base}/api/v3/repos/{repo.full_name}/issues/comments/{comment.id}",
        "html_url": f"{base}/{repo.full_name}/issues/{issue_number}#issuecomment-{comment.id}",
        "issue_url": f"{base}/api/v3/repos/{repo.full_name}/issues/{issue_number}",
    }


# --------------------------------------------------------------------------
# GraphQL global ids: opaque, stable, and self-describing so node(id:)
# round-trips. GitHub's own ids are base64 of a type-and-id string; these
# are base64 of "<Type>:<database id>".
# --------------------------------------------------------------------------

_ID_TYPES = {"Repository", "Issue", "PullRequest", "Label", "IssueComment", "User"}


def global_id(type_name: str, db_id: int) -> str:
    return base64.b64encode(f"{type_name}:{db_id}".encode()).decode()


def parse_global_id(value: str) -> tuple[str, int] | None:
    try:
        decoded = base64.b64decode(value, validate=True).decode()
        type_name, _, number = decoded.rpartition(":")
        if type_name in _ID_TYPES:
            return type_name, int(number)
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# git operations behind the REST surface
# --------------------------------------------------------------------------


def split_ident(line: str) -> dict:
    """'Name <email> epoch +0000' -> the REST author object."""
    name, _, rest = line.partition(" <")
    email, _, tail = rest.partition("> ")
    epoch = int(tail.split(" ")[0]) if tail else 0
    return {"name": name, "email": email, "date": iso(epoch)}


def commit_json(repo: Repo, sha: str, base: str) -> dict:
    entry = STORE.get(sha)
    parsed = parse_commit(entry[1]) if entry else {"tree": "", "parents": [], "message": "",
                                                   "author": "", "committer": ""}
    author = split_ident(parsed.get("author", ""))
    committer = split_ident(parsed.get("committer", ""))
    linked = user_json() if author["email"] == OWNER_EMAIL else None
    return {
        "sha": sha,
        "node_id": global_id("Repository", repo.id) + sha[:8],
        "commit": {
            "author": author,
            "committer": committer,
            "message": parsed["message"],
            "tree": {"sha": parsed["tree"],
                     "url": f"{base}/api/v3/repos/{repo.full_name}/git/trees/{parsed['tree']}"},
            "url": f"{base}/api/v3/repos/{repo.full_name}/git/commits/{sha}",
            "comment_count": 0,
        },
        "author": linked,
        "committer": linked,
        "parents": [{"sha": parent,
                     "url": f"{base}/api/v3/repos/{repo.full_name}/git/commits/{parent}",
                     "html_url": f"{base}/{repo.full_name}/commit/{parent}"}
                    for parent in parsed["parents"]],
        "url": f"{base}/api/v3/repos/{repo.full_name}/commits/{sha}",
        "html_url": f"{base}/{repo.full_name}/commit/{sha}",
        "comments_url": f"{base}/api/v3/repos/{repo.full_name}/commits/{sha}/comments",
    }


def list_commits(repo: Repo, start_sha: str) -> list[str]:
    """Commits reachable from a tip, newest first (committer time, then the
    walk's discovery order — deterministic for a fixed history)."""
    seen: list[str] = []
    known: set[str] = set()
    stack = [start_sha]
    while stack:
        sha = stack.pop()
        if sha in known:
            continue
        entry = STORE.get(sha)
        if entry is None or entry[0] != "commit":
            continue
        known.add(sha)
        seen.append(sha)
        stack.extend(parse_commit(entry[1])["parents"])

    def epoch_of(sha: str) -> int:
        entry = STORE.get(sha)
        ident = parse_commit(entry[1]).get("committer", "") if entry else ""
        try:
            return int(ident.rsplit(" ", 2)[1])
        except Exception:
            return 0

    return sorted(seen, key=epoch_of, reverse=True)


def apply_tree_change(repo: Repo, branch: str, new_flat: dict[str, tuple[str, str]],
                      message: str, now: int) -> str:
    """Commit a whole-tree change on a branch and move the branch."""
    tree_sha = build_tree_from_flat(new_flat)
    tip = repo.tip(branch)
    ident = viewer_ident(now)
    parents = [tip] if tip else []
    commit_sha = STORE.put("commit", build_commit(tree_sha, parents, ident, ident, message))
    repo.refs[f"refs/heads/{branch}"] = commit_sha
    repo.updated_at = now
    return commit_sha


def branch_flat(repo: Repo, branch: str) -> dict[str, tuple[str, str]]:
    tip = repo.tip(branch)
    if tip is None:
        return {}
    tree = commit_tree_sha(tip)
    return flatten_tree(tree) if tree else {}


def squash_merge(repo: Repo, pull: Pull, now: int) -> str | None:
    """Squash the pull's head into its base. Returns the merge commit's sha,
    or None when the two sides changed the same path incompatibly."""
    head_tip = repo.tip(pull.head_ref)
    base_tip = repo.tip(pull.base_ref)
    if head_tip is None or base_tip is None:
        return None
    base_commit = merge_base(head_tip, base_tip)
    mb_flat = {}
    if base_commit is not None:
        tree = commit_tree_sha(base_commit)
        mb_flat = flatten_tree(tree) if tree else {}
    base_flat = branch_flat(repo, pull.base_ref)
    head_flat = branch_flat(repo, pull.head_ref)

    merged: dict[str, tuple[str, str]] = {}
    paths = set(mb_flat) | set(base_flat) | set(head_flat)
    for path in paths:
        mb_v = mb_flat.get(path)
        base_v = base_flat.get(path)
        head_v = head_flat.get(path)
        if base_v == head_v:
            chosen = base_v
        elif mb_v == base_v:
            chosen = head_v
        elif mb_v == head_v:
            chosen = base_v
        else:
            return None  # both sides changed this path differently
        if chosen is not None:
            merged[path] = chosen

    message = f"{pull.title} (#{pull.number})"
    if pull.body:
        message += f"\n\n{pull.body}"
    return apply_tree_change(repo, pull.base_ref, merged, message, now)


def pull_mergeable(repo: Repo, pull: Pull) -> str:
    """MERGEABLE or CONFLICTING, by the same tree merge the merge performs."""
    head_tip = repo.tip(pull.head_ref)
    base_tip = repo.tip(pull.base_ref)
    if head_tip is None or base_tip is None:
        return "UNKNOWN"
    base_commit = merge_base(head_tip, base_tip)
    mb_flat = {}
    if base_commit is not None:
        tree = commit_tree_sha(base_commit)
        mb_flat = flatten_tree(tree) if tree else {}
    base_flat = branch_flat(repo, pull.base_ref)
    head_flat = branch_flat(repo, pull.head_ref)
    for path in set(mb_flat) | set(base_flat) | set(head_flat):
        mb_v, base_v, head_v = mb_flat.get(path), base_flat.get(path), head_flat.get(path)
        if base_v != head_v and mb_v != base_v and mb_v != head_v:
            return "CONFLICTING"
    return "MERGEABLE"


def pull_files(repo: Repo, pull: Pull) -> list[dict]:
    """The changed paths between the pull's merge base and its head tip,
    with line-change counts."""
    import difflib

    head_tip = repo.tip(pull.head_ref) or pull.head_sha
    base_tip = repo.tip(pull.base_ref) or pull.base_sha
    base_commit = merge_base(head_tip, base_tip)
    mb_flat: dict[str, tuple[str, str]] = {}
    if base_commit is not None:
        tree = commit_tree_sha(base_commit)
        mb_flat = flatten_tree(tree) if tree else {}
    head_tree = commit_tree_sha(head_tip)
    head_flat = flatten_tree(head_tree) if head_tree else {}

    files = []
    for path in sorted(set(mb_flat) | set(head_flat)):
        old = mb_flat.get(path)
        new = head_flat.get(path)
        if old == new:
            continue
        if old is None:
            status = "added"
        elif new is None:
            status = "removed"
        else:
            status = "modified"
        old_lines: list[str] = []
        new_lines: list[str] = []
        if old is not None:
            entry = STORE.get(old[1])
            if entry:
                old_lines = entry[1].decode("utf-8", "replace").splitlines()
        if new is not None:
            entry = STORE.get(new[1])
            if entry:
                new_lines = entry[1].decode("utf-8", "replace").splitlines()
        additions = deletions = 0
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes():
            if tag in ("replace", "delete"):
                deletions += i2 - i1
            if tag in ("replace", "insert"):
                additions += j2 - j1
        files.append({
            "sha": new[1] if new else old[1],
            "filename": path,
            "status": status,
            "additions": additions,
            "deletions": deletions,
            "changes": additions + deletions,
        })
    return files


# --------------------------------------------------------------------------
# packfiles and pkt-lines
# --------------------------------------------------------------------------

TYPE_CODES = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
CODE_TYPES = {code: name for name, code in TYPE_CODES.items()}


def _read_varint_size(data: bytes, pos: int) -> tuple[int, int, int]:
    """A pack entry header: (type code, size, next pos)."""
    byte = data[pos]
    pos += 1
    obj_type = (byte >> 4) & 0x7
    size = byte & 0x0F
    shift = 4
    while byte & 0x80:
        byte = data[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        shift += 7
    return obj_type, size, pos


def apply_delta(base: bytes, delta: bytes) -> bytes:
    def read_varint(pos: int) -> tuple[int, int]:
        value = 0
        shift = 0
        while True:
            byte = delta[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value, pos

    pos = 0
    _source_size, pos = read_varint(pos)
    target_size, pos = read_varint(pos)
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            offset = 0
            size = 0
            for bit in range(4):
                if op & (1 << bit):
                    offset |= delta[pos] << (8 * bit)
                    pos += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    size |= delta[pos] << (8 * bit)
                    pos += 1
            if size == 0:
                size = 0x10000
            out += base[offset : offset + size]
        elif op:
            out += delta[pos : pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != target_size:
        raise ValueError("delta size mismatch")
    return bytes(out)


def read_pack(data: bytes) -> dict[str, tuple[str, bytes]]:
    """Parse a whole pack (thin packs included: a delta base the pack does
    not carry is read from the store). Returns {sha: (type, content)}."""
    if len(data) < 12 + 20 or data[:4] != b"PACK":
        raise ValueError("not a pack")
    version, count = struct.unpack(">II", data[4:12])
    if version != 2:
        raise ValueError("pack version")
    if hashlib.sha1(data[:-20]).digest() != data[-20:]:
        raise ValueError("pack checksum")

    raw: dict[int, tuple[int, bytes, int]] = {}   # offset -> (type, payload, base offset/sha info)
    entries: list[tuple[int, int, bytes]] = []    # (offset, type, payload or delta)
    bases: dict[int, tuple[str, object]] = {}     # offset -> ("ofs", base offset) | ("ref", sha)
    pos = 12
    for _ in range(count):
        start = pos
        obj_type, _size, pos = _read_varint_size(data, pos)
        if obj_type == 6:  # ofs-delta
            byte = data[pos]
            pos += 1
            offset = byte & 0x7F
            while byte & 0x80:
                byte = data[pos]
                pos += 1
                offset = ((offset + 1) << 7) | (byte & 0x7F)
            bases[start] = ("ofs", start - offset)
        elif obj_type == 7:  # ref-delta
            bases[start] = ("ref", data[pos : pos + 20].hex())
            pos += 20
        inflater = zlib.decompressobj()
        payload = inflater.decompress(data[pos:])
        pos += len(data[pos:]) - len(inflater.unused_data)
        entries.append((start, obj_type, payload))

    by_offset: dict[int, tuple[str, bytes]] = {}
    by_sha: dict[str, tuple[str, bytes]] = {}

    def resolve(offset: int) -> tuple[str, bytes]:
        if offset in by_offset:
            return by_offset[offset]
        _start, obj_type, payload = next(e for e in entries if e[0] == offset)
        if obj_type in CODE_TYPES:
            resolved = (CODE_TYPES[obj_type], payload)
        else:
            kind, target = bases[offset]
            if kind == "ofs":
                base_type, base_content = resolve(int(target))  # type: ignore[arg-type]
            else:
                sha = str(target)
                if sha in by_sha:
                    base_type, base_content = by_sha[sha]
                elif STORE.has(sha):
                    base_type, base_content = STORE.get(sha)  # type: ignore[misc]
                else:
                    raise ValueError(f"thin pack base {sha} not available")
            resolved = (base_type, apply_delta(base_content, payload))
        by_offset[offset] = resolved
        by_sha[hash_object(resolved[0], resolved[1])] = resolved
        return resolved

    for offset, _t, _p in entries:
        resolve(offset)
    return by_sha


def write_pack(objects: list[tuple[str, bytes]]) -> bytes:
    """A version-2 pack of full objects (no deltas: the protocol allows a
    server to send every object whole)."""
    out = bytearray()
    out += b"PACK" + struct.pack(">II", 2, len(objects))
    for obj_type, content in objects:
        size = len(content)
        first = (TYPE_CODES[obj_type] << 4) | (size & 0x0F)
        size >>= 4
        header = bytearray()
        if size:
            header.append(first | 0x80)
            while size:
                byte = size & 0x7F
                size >>= 7
                header.append(byte | (0x80 if size else 0))
        else:
            header.append(first)
        out += header
        out += zlib.compress(content, 1)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


def collect_pack_objects(want_tips: list[str], exclude_roots: list[str],
                         depth: int | None = None,
                         shallow_existing: set[str] | None = None) -> tuple[list[tuple[str, bytes]], list[str]]:
    """The objects a fetch answers: everything reachable from the wants,
    minus everything reachable from the excludes (the client's haves).

    With a depth, only that many commit generations are walked and the
    boundary commits are returned as the new shallow list. Trees and blobs
    of every included commit are included in full.
    """
    excluded: set[str] = set()
    for root in exclude_roots:
        excluded |= ancestors_of(root)

    included_commits: list[str] = []
    shallow: list[str] = []
    seen: set[str] = set()
    # Breadth-first by generation so depth counts generations exactly.
    frontier = list(want_tips)
    generation = 0
    while frontier:
        generation += 1
        next_frontier: list[str] = []
        for sha in frontier:
            if sha in seen or sha in excluded:
                continue
            entry = STORE.get(sha)
            if entry is None or entry[0] != "commit":
                continue
            seen.add(sha)
            included_commits.append(sha)
            parents = parse_commit(entry[1])["parents"]
            if depth is not None and generation >= depth:
                if parents:
                    shallow.append(sha)
                continue
            next_frontier.extend(parents)
        frontier = next_frontier

    objects: dict[str, tuple[str, bytes]] = {}

    def add(sha: str) -> None:
        if sha in objects or sha in excluded_objects:
            return
        entry = STORE.get(sha)
        if entry is None:
            return
        objects[sha] = entry
        if entry[0] == "tree":
            for _mode, _name, child in parse_tree(entry[1]):
                add(child)

    excluded_objects: set[str] = set()
    for sha in excluded:
        entry = STORE.get(sha)
        if entry is not None and entry[0] == "commit":
            tree = parse_commit(entry[1])["tree"]
            stack = [tree]
            while stack:
                current = stack.pop()
                if current in excluded_objects:
                    continue
                excluded_objects.add(current)
                current_entry = STORE.get(current)
                if current_entry is not None and current_entry[0] == "tree":
                    stack.extend(child for _m, _n, child in parse_tree(current_entry[1]))

    for sha in included_commits:
        entry = STORE.get(sha)
        if entry is None:
            continue
        objects[sha] = entry
        tree = parse_commit(entry[1])["tree"]
        add(tree)
    # Tags pointing into the set ride along (annotated tags on wanted refs).
    return list(objects.values()), shallow


def pkt_line(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode() + payload


def pkt_text(text: str) -> bytes:
    return pkt_line(text.encode())


PKT_FLUSH = b"0000"
PKT_DELIM = b"0001"
PKT_RESPONSE_END = b"0002"


def read_pkt_lines(data: bytes, pos: int = 0) -> tuple[list[tuple[str, bytes]], int]:
    """Parse pkt-lines into (kind, payload) pairs, kind one of "line",
    "delim" (0001), "flush" (0000) or "end" (0002). Parsing stops after a
    flush; returns (packets, next pos)."""
    packets: list[tuple[str, bytes]] = []
    while pos + 4 <= len(data):
        length = int(data[pos : pos + 4], 16)
        pos += 4
        if length == 0:
            packets.append(("flush", b""))
            return packets, pos
        if length == 1:
            packets.append(("delim", b""))
            continue
        if length == 2:
            packets.append(("end", b""))
            continue
        packets.append(("line", data[pos : pos + length - 4]))
        pos += length - 4
    return packets, pos


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------


class Reply:
    def __init__(self, status: int, body: bytes, content_type: str,
                 headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}


def json_reply(status: int, value: object, headers: dict[str, str] | None = None) -> Reply:
    return Reply(status, json.dumps(value).encode(), "application/json; charset=utf-8", headers)


def error_reply(status: int, message: str) -> Reply:
    return json_reply(status, {"message": message})


class Request:
    def __init__(self, method: str, path: str, query: dict[str, list[str]],
                 headers: dict[str, str], body: bytes) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        host = headers.get("host", "127.0.0.1")
        self.base = f"http://{host}"

    def query_one(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def json_body(self) -> tuple[dict | None, Reply | None]:
        """The request's JSON object body: ({...}, None), or (None, a 400
        reply) when a body is present but is not a JSON object."""
        if not self.body:
            return {}, None
        try:
            value = json.loads(self.body)
        except Exception:
            return None, error_reply(400, "Problems parsing JSON")
        if not isinstance(value, dict):
            return None, error_reply(400, "Problems parsing JSON")
        return value, None

    def authorized(self) -> bool:
        return self.headers.get("authorization", "") == f"Bearer {TOKEN}"


def find_repo(owner: str, name: str) -> Repo | None:
    if owner != OWNER_LOGIN:
        return None
    return FORGE.repos.get(name)


def paginate(request: Request, items: list) -> tuple[list, dict[str, str]]:
    """GitHub-style page/per_page slicing with a Link header."""
    try:
        per_page = max(1, min(100, int(request.query_one("per_page", "30"))))
    except ValueError:
        per_page = 30
    try:
        page = max(1, int(request.query_one("page", "1")))
    except ValueError:
        page = 1
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    start = (page - 1) * per_page
    sliced = items[start : start + per_page]
    headers: dict[str, str] = {}
    if page < total_pages:
        parsed = urlparse(request.path)
        base_url = f"{request.base}{parsed.path}"
        links = []
        params = {k: v[0] for k, v in request.query.items()}
        for rel, target in (("next", page + 1), ("last", total_pages)):
            merged = dict(params, page=str(target), per_page=str(per_page))
            qs = "&".join(f"{k}={v}" for k, v in sorted(merged.items()))
            links.append(f'<{base_url}?{qs}>; rel="{rel}"')
        headers["Link"] = ", ".join(links)
    return sliced, headers


# --------------------------------------------------------------------------
# REST v3: repositories, branches, commits, contents, git refs
# --------------------------------------------------------------------------


def rest_root(request: Request) -> Reply:
    base = request.base
    doc = {
        "current_user_url": f"{base}/api/v3/user",
        "current_user_repositories_url": f"{base}/api/v3/user/repos",
        "repository_url": f"{base}/api/v3/repos/{{owner}}/{{repo}}",
        "issue_search_url": f"{base}/api/v3/search/issues",
        "rate_limit_url": f"{base}/api/v3/rate_limit",
    }
    return json_reply(200, doc, {"X-OAuth-Scopes": "repo, read:org"})


def rest_meta(request: Request) -> Reply:
    return json_reply(200, {
        "installed_version": INSTALLED_VERSION,
        "verifiable_password_authentication": False,
        "ssh_key_fingerprints": {},
        "api_url": f"{request.base}/api/v3/",
        "web_url": f"{request.base}/",
    })


def rest_create_repo(request: Request) -> Reply:
    body, error = request.json_body()
    if error:
        return error
    name = str(body.get("name", "")).strip()
    if not name:
        return error_reply(422, "Repository creation failed.")
    with FORGE.lock:
        if name in FORGE.repos:
            return json_reply(422, {"message": "Repository creation failed.",
                                    "errors": [{"resource": "Repository", "code": "custom",
                                                "field": "name", "message": "name already exists on this account"}]})
        repo = FORGE.create_repo(name, bool(body.get("private", False)),
                                 str(body.get("description", "") or ""))
        if body.get("auto_init"):
            now = CLOCK.tick()
            blob = STORE.put("blob", f"# {name}\n".encode())
            apply_tree_change(repo, repo.default_branch, {"README.md": ("100644", blob)},
                              "Initial commit", now)
        return json_reply(201, repo_json(repo, request.base))


def rest_list_user_repos(request: Request) -> Reply:
    with FORGE.lock:
        repos = sorted(FORGE.repos.values(), key=lambda r: r.id)
        page, headers = paginate(request, repos)
        return json_reply(200, [repo_json(r, request.base) for r in page], headers)


def rest_repo(request: Request, owner: str, name: str) -> Reply:
    repo = find_repo(owner, name)
    if repo is None:
        return error_reply(404, "Not Found")
    if request.method == "GET":
        return json_reply(200, repo_json(repo, request.base))
    if request.method == "PATCH":
        body, error = request.json_body()
        if error:
            return error
        with FORGE.lock:
            if "description" in body:
                repo.description = str(body["description"] or "")
            if "private" in body:
                repo.private = bool(body["private"])
            if "default_branch" in body and repo.tip(str(body["default_branch"])):
                repo.default_branch = str(body["default_branch"])
            repo.updated_at = CLOCK.tick()
            return json_reply(200, repo_json(repo, request.base))
    if request.method == "DELETE":
        with FORGE.lock:
            del FORGE.repos[name]
        return Reply(204, b"", "application/json; charset=utf-8")
    return error_reply(404, "Not Found")


def rest_branches(request: Request, owner: str, name: str, branch: str | None) -> Reply:
    repo = find_repo(owner, name)
    if repo is None:
        return error_reply(404, "Not Found")
    base = request.base

    def branch_json(branch_name: str, sha: str) -> dict:
        return {"name": branch_name,
                "commit": {"sha": sha,
                           "url": f"{base}/api/v3/repos/{repo.full_name}/commits/{sha}"},
                "protected": False}

    if branch is None:
        items = [branch_json(ref[len("refs/heads/"):], sha)
                 for ref, sha in sorted(repo.refs.items()) if ref.startswith("refs/heads/")]
        page, headers = paginate(request, items)
        return json_reply(200, page, headers)
    sha = repo.tip(branch)
    if sha is None:
        return error_reply(404, "Not Found")
    return json_reply(200, branch_json(branch, sha))


def rest_commits(request: Request, owner: str, name: str) -> Reply:
    repo = find_repo(owner, name)
    if repo is None:
        return error_reply(404, "Not Found")
    ref = request.query_one("sha", repo.default_branch)
    tip = repo.refs.get(f"refs/heads/{ref}") or (ref if STORE.type_of(ref or "") == "commit" else None)
    if tip is None:
        return error_reply(409, "Git Repository is empty.")
    with FORGE.lock:
        shas = list_commits(repo, tip)
        page, headers = paginate(request, shas)
        return json_reply(200, [commit_json(repo, sha, request.base) for sha in page], headers)


def rest_git_ref(request: Request, owner: str, name: str, ref: str | None) -> Reply:
    repo = find_repo(owner, name)
    if repo is None:
        return error_reply(404, "Not Found")
    base = request.base

    def ref_json(ref_name: str, sha: str) -> dict:
        obj_type = STORE.type_of(sha) or "commit"
        return {"ref": ref_name,
                "node_id": global_id("Repository", repo.id),
                "url": f"{base}/api/v3/repos/{repo.full_name}/git/refs/{ref_name[len('refs/'):]}",
                "object": {"sha": sha, "type": obj_type,
                           "url": f"{base}/api/v3/repos/{repo.full_name}/git/{obj_type}s/{sha}"}}

    if request.method == "POST" and ref is None:
        body, error = request.json_body()
        if error:
            return error
        ref_name = str(body.get("ref", ""))
        sha = str(body.get("sha", ""))
        if not ref_name.startswith("refs/") or not STORE.has(sha):
            return error_reply(422, "Invalid ref or sha.")
        with FORGE.lock:
            if ref_name in repo.refs:
                return error_reply(422, "Reference already exists")
            repo.refs[ref_name] = sha
            return json_reply(201, ref_json(ref_name, sha))
    if request.method == "GET" and ref is not None:
        sha = repo.refs.get(ref)
        if sha is None:
            return error_reply(404, "Not Found")
        return json_reply(200, ref_json(ref, sha))
    return error_reply(404, "Not Found")


def rest_git_object(request: Request, owner: str, name: str, kind: str, sha: str) -> Reply:
    repo = find_repo(owner, name)
    if repo is None:
        return error_reply(404, "Not Found")
    entry = STORE.get(sha)
    if entry is None:
        return error_reply(404, "Not Found")
    obj_type, content = entry
    base = request.base
    if kind == "commits" and obj_type == "commit":
        parsed = parse_commit(content)
        return json_reply(200, {
            "sha": sha,
            "author": split_ident(parsed.get("author", "")),
            "committer": split_ident(parsed.get("committer", "")),
            "message": parsed["message"],
            "tree": {"sha": parsed["tree"],
                     "url": f"{base}/api/v3/repos/{repo.full_name}/git/trees/{parsed['tree']}"},
            "parents": [{"sha": p} for p in parsed["parents"]],
            "url": f"{base}/api/v3/repos/{repo.full_name}/git/commits/{sha}",
        })
    if kind == "trees" and obj_type == "tree":
        return json_reply(200, {
            "sha": sha,
            "tree": [{"path": entry_name, "mode": mode, "type": "tree" if mode == "40000" else "blob",
                      "sha": child,
                      "url": f"{base}/api/v3/repos/{repo.full_name}/git/trees/{child}"}
                     for mode, entry_name, child in parse_tree(content)],
            "truncated": False,
            "url": f"{base}/api/v3/repos/{repo.full_name}/git/trees/{sha}",
        })
    if kind == "blobs" and obj_type == "blob":
        return json_reply(200, {
            "sha": sha,
            "size": len(content),
            "encoding": "base64",
            "content": base64.encodebytes(content).decode(),
            "url": f"{base}/api/v3/repos/{repo.full_name}/git/blobs/{sha}",
        })
    return error_reply(404, "Not Found")


def _contents_entry(repo: Repo, base: str, path: str, mode: str, sha: str) -> dict:
    name = path.rsplit("/", 1)[-1]
    is_dir = mode == "40000"
    size = 0
    if not is_dir:
        entry = STORE.get(sha)
        size = len(entry[1]) if entry else 0
    return {
        "type": "dir" if is_dir else "file",
        "size": size,
        "name": name,
        "path": path,
        "sha": sha,
        "url": f"{base}/api/v3/repos/{repo.full_name}/contents/{path}",
        "git_url": f"{base}/api/v3/repos/{repo.full_name}/git/{'trees' if is_dir else 'blobs'}/{sha}",
        "html_url": f"{base}/{repo.full_name}/blob/{repo.default_branch}/{path}",
        "download_url": None if is_dir else f"{base}/raw/{repo.full_name}/{path}",
        "_links": {"self": f"{base}/api/v3/repos/{repo.full_name}/contents/{path}"},
    }


def rest_contents(request: Request, owner: str, name: str, path: str) -> Reply:
    repo = find_repo(owner, name)
    if repo is None:
        return error_reply(404, "Not Found")
    base = request.base
    path = path.strip("/")

    if request.method == "GET":
        branch = request.query_one("ref", repo.default_branch)
        tip = repo.tip(branch)
        if tip is None:
            return error_reply(404, "Not Found")
        tree = commit_tree_sha(tip)
        if tree is None:
            return error_reply(404, "Not Found")
        if path:
            # Walk to the target.
            current = tree
            segments = path.split("/")
            for depth, segment in enumerate(segments):
                entry = STORE.get(current)
                if entry is None or entry[0] != "tree":
                    return error_reply(404, "Not Found")
                found = None
                for mode, entry_name, child in parse_tree(entry[1]):
                    if entry_name == segment:
                        found = (mode, child)
                        break
                if found is None:
                    return error_reply(404, "Not Found")
                mode, child = found
                if depth == len(segments) - 1:
                    if mode == "40000":
                        listing = []
                        subtree = STORE.get(child)
                        for sub_mode, sub_name, sub_sha in parse_tree(subtree[1]):
                            listing.append(_contents_entry(repo, base, f"{path}/{sub_name}", sub_mode, sub_sha))
                        return json_reply(200, listing)
                    blob = STORE.get(child)
                    content = blob[1] if blob else b""
                    result = _contents_entry(repo, base, path, mode, child)
                    result.update({
                        "encoding": "base64",
                        "content": base64.encodebytes(content).decode(),
                    })
                    return json_reply(200, result)
                if mode != "40000":
                    return error_reply(404, "Not Found")
                current = child
        root = STORE.get(tree)
        listing = [_contents_entry(repo, base, entry_name, mode, child)
                   for mode, entry_name, child in parse_tree(root[1])]
        return json_reply(200, listing)

    if request.method == "PUT":
        body, error = request.json_body()
        if error:
            return error
        branch = str(body.get("branch", repo.default_branch))
        message = str(body.get("message", f"Update {path}"))
        try:
            content = base64.b64decode(str(body.get("content", "")), validate=False)
        except Exception:
            return error_reply(422, "content is not valid base64")
        with FORGE.lock:
            flat = branch_flat(repo, branch)
            existing = flat.get(path)
            supplied_sha = body.get("sha")
            if existing is not None:
                if supplied_sha is None:
                    return error_reply(422, "\"sha\" wasn't supplied.")
                if str(supplied_sha) != existing[1]:
                    return error_reply(409, f"{path} sha does not match")
            blob_sha = STORE.put("blob", content)
            flat[path] = ("100644", blob_sha)
            now = CLOCK.tick()
            commit_sha = apply_tree_change(repo, branch, flat, message, now)
            result = _contents_entry(repo, base, path, "100644", blob_sha)
            result.update({"encoding": "base64",
                           "content": base64.encodebytes(content).decode()})
            return json_reply(200 if existing is not None else 201,
                              {"content": result,
                               "commit": commit_json(repo, commit_sha, base)})

    if request.method == "DELETE":
        body, error = request.json_body()
        if error:
            return error
        branch = str(body.get("branch", repo.default_branch))
        message = str(body.get("message", f"Delete {path}"))
        with FORGE.lock:
            flat = branch_flat(repo, branch)
            existing = flat.get(path)
            if existing is None:
                return error_reply(404, "Not Found")
            if body.get("sha") is not None and str(body["sha"]) != existing[1]:
                return error_reply(409, f"{path} sha does not match")
            del flat[path]
            now = CLOCK.tick()
            commit_sha = apply_tree_change(repo, branch, flat, message, now)
            return json_reply(200, {"content": None,
                                    "commit": commit_json(repo, commit_sha, base)})
    return error_reply(404, "Not Found")


# --------------------------------------------------------------------------
# REST v3: labels, issues, comments, pull requests
# --------------------------------------------------------------------------

LABEL_COLORS = ["b60205", "d93f0b", "fbca04", "0e8a16", "006b75", "1d76db",
                "0052cc", "5319e7", "e99695", "f9d0c4"]


def _label_color(name: str) -> str:
    return LABEL_COLORS[int(hashlib.sha1(name.encode()).hexdigest(), 16) % len(LABEL_COLORS)]


def rest_labels(request: Request, owner: str, name: str, label_name: str | None) -> Reply:
    repo = find_repo(owner, name)
    if repo is None:
        return error_reply(404, "Not Found")
    if request.method == "POST" and label_name is None:
        body, error = request.json_body()
        if error:
            return error
        label_text = str(body.get("name", "")).strip()
        if not label_text:
            return error_reply(422, "Validation Failed: name is required")
        with FORGE.lock:
            if label_text in repo.labels:
                return error_reply(422, "Validation Failed: label already exists")
            color = str(body.get("color", "") or _label_color(label_text)).lstrip("#")
            label = Label(FORGE.next_label_id, label_text, color,
                          str(body.get("description", "") or ""))
            FORGE.next_label_id += 1
            repo.labels[label_text] = label
            return json_reply(201, label_json(label, request.base, repo))
    if request.method == "GET":
        if label_name is None:
            items = [label_json(label, request.base, repo)
                     for label in sorted(repo.labels.values(), key=lambda l: l.id)]
            page, headers = paginate(request, items)
            return json_reply(200, page, headers)
        label = repo.labels.get(label_name)
        if label is None:
            return error_reply(404, "Not Found")
        return json_reply(200, label_json(label, request.base, repo))
    return error_reply(404, "Not Found")


def _issue_holder(repo: Repo, number: int) -> Issue | Pull | None:
    if number in repo.issues:
        return repo.issues[number]
    return repo.pulls.get(number)


def rest_issues(request: Request, owner: str, name: str, number: int | None) -> Reply:
    repo = find_repo(owner, name)
    if repo is None:
        return error_reply(404, "Not Found")
    base = request.base

    if number is None:
        if request.method == "POST":
            body, error = request.json_body()
            if error:
                return error
            title = str(body.get("title", ""))
            label_names = [str(item.get("name", item)) if isinstance(item, dict) else str(item)
                           for item in body.get("labels", [])]
            with FORGE.lock:
                for label_name in label_names:
                    if label_name not in repo.labels:
                        return json_reply(422, {"message": "Validation Failed: label does not exist",
                                                "errors": [{"resource": "Issue", "code": "custom",
                                                            "field": "labels",
                                                            "message": f"label '{label_name}' does not exist"}]})
                now = CLOCK.tick()
                issue = Issue(FORGE.next_issue_id, repo.next_number, title,
                              str(body.get("body", "") or ""), label_names, now)
                FORGE.next_issue_id += 1
                repo.next_number += 1
                repo.issues[issue.number] = issue
                return json_reply(201, issue_json(repo, issue, base))
        if request.method == "GET":
            state = request.query_one("state", "open")
            sort = request.query_one("sort", "created")
            direction = request.query_one("direction", "desc")
            holders: list[Issue] = list(repo.issues.values())
            items = [issue_json(repo, issue, base) for issue in holders
                     if state == "all" or issue.state == state]
            order = {issue.number: (issue.created_at, issue.number) for issue in holders}
            items.sort(key=lambda item: order[item["number"]],
                       reverse=(direction != "asc"))
            page, headers = paginate(request, items)
            return json_reply(200, page, headers)
        return error_reply(404, "Not Found")

    holder = _issue_holder(repo, number)
    if holder is None:
        return error_reply(404, "Not Found")
    if isinstance(holder, Pull):
        if request.method == "GET":
            view = issue_json(repo, Issue(holder.id, holder.number, holder.title,
                                          holder.body, [], holder.created_at), base)
            view["state"] = holder.state
            view["pull_request"] = {"url": f"{base}/api/v3/repos/{repo.full_name}/pulls/{number}",
                                    "html_url": f"{base}/{repo.full_name}/pull/{number}"}
            return json_reply(200, view)
        return error_reply(404, "Not Found")
    if request.method == "GET":
        return json_reply(200, issue_json(repo, holder, base))
    if request.method == "PATCH":
        body, error = request.json_body()
        if error:
            return error
        with FORGE.lock:
            now = CLOCK.tick()
            if "title" in body:
                holder.title = str(body["title"])
            if "body" in body:
                holder.body = str(body["body"] or "")
            if "labels" in body:
                names = [str(item.get("name", item)) if isinstance(item, dict) else str(item)
                         for item in body["labels"]]
                for label_name in names:
                    if label_name not in repo.labels:
                        return error_reply(422, "Validation Failed: label does not exist")
                holder.labels = names
            if "state" in body:
                new_state = str(body["state"])
                if new_state in ("open", "closed"):
                    holder.state = new_state
                    holder.closed_at = now if new_state == "closed" else None
            holder.updated_at = now
            return json_reply(200, issue_json(repo, holder, base))
    return error_reply(404, "Not Found")


def rest_comments(request: Request, owner: str, name: str, number: int) -> Reply:
    repo = find_repo(owner, name)
    if repo is None:
        return error_reply(404, "Not Found")
    holder = _issue_holder(repo, number)
    if holder is None:
        return error_reply(404, "Not Found")
    if request.method == "POST":
        body, error = request.json_body()
        if error:
            return error
        with FORGE.lock:
            now = CLOCK.tick()
            comment = Comment(FORGE.next_comment_id, str(body.get("body", "") or ""), now)
            FORGE.next_comment_id += 1
            holder.comments.append(comment)
            return json_reply(201, comment_json(repo, number, comment, request.base))
    if request.method == "GET":
        items = [comment_json(repo, number, comment, request.base) for comment in holder.comments]
        page, headers = paginate(request, items)
        return json_reply(200, page, headers)
    return error_reply(404, "Not Found")


def rest_pulls(request: Request, owner: str, name: str, number: int | None,
               action: str | None) -> Reply:
    repo = find_repo(owner, name)
    if repo is None:
        return error_reply(404, "Not Found")
    base = request.base

    if number is None:
        if request.method == "POST":
            body, error = request.json_body()
            if error:
                return error
            head = str(body.get("head", "")).split(":")[-1]
            base_ref = str(body.get("base", repo.default_branch)).split(":")[-1]
            with FORGE.lock:
                head_sha = repo.tip(head)
                base_sha = repo.tip(base_ref)
                if head_sha is None or base_sha is None:
                    return error_reply(422, "Validation Failed: head or base branch does not exist")
                for existing in repo.pulls.values():
                    if (existing.state == "open" and existing.head_ref == head
                            and existing.base_ref == base_ref):
                        return error_reply(422, "Validation Failed: a pull request already exists for this branch")
                now = CLOCK.tick()
                pull = Pull(FORGE.next_issue_id, repo.next_number, str(body.get("title", "")),
                            str(body.get("body", "") or ""), head, head_sha, base_ref, base_sha, now)
                FORGE.next_issue_id += 1
                repo.next_number += 1
                repo.pulls[pull.number] = pull
                return json_reply(201, pull_json(repo, pull, base))
        if request.method == "GET":
            state = request.query_one("state", "open")
            items = [pull_json(repo, pull, base) for pull in
                     sorted(repo.pulls.values(), key=lambda p: p.number)
                     if state == "all" or pull.state == state
                     or (state == "closed" and pull.state == "closed")]
            if state == "open":
                items = [item for item in items if item["state"] == "open"]
            page, headers = paginate(request, items)
            return json_reply(200, page, headers)
        return error_reply(404, "Not Found")

    pull = repo.pulls.get(number)
    if pull is None:
        return error_reply(404, "Not Found")
    if action == "files" and request.method == "GET":
        with FORGE.lock:
            items = pull_files(repo, pull)
        page, headers = paginate(request, items)
        return json_reply(200, page, headers)
    if action == "merge" and request.method == "PUT":
        body, error = request.json_body()
        if error:
            return error
        method = str(body.get("merge_method", "merge")).lower()
        with FORGE.lock:
            if pull.state != "open":
                return error_reply(405, "Pull Request is not mergeable")
            now = CLOCK.tick()
            sha = squash_merge(repo, pull, now)
            if sha is None:
                return error_reply(405, "Pull Request is not mergeable")
            pull.state = "closed"
            pull.merged = True
            pull.merged_at = now
            pull.merge_commit_sha = sha
            pull.updated_at = now
            return json_reply(200, {"sha": sha, "merged": True,
                                    "message": "Pull Request successfully merged"})
    if request.method == "GET" and action is None:
        return json_reply(200, pull_json(repo, pull, base))
    if request.method == "PATCH" and action is None:
        body, error = request.json_body()
        if error:
            return error
        with FORGE.lock:
            if "title" in body:
                pull.title = str(body["title"])
            if "body" in body:
                pull.body = str(body["body"] or "")
            if "state" in body and str(body["state"]) in ("open", "closed"):
                pull.state = str(body["state"])
            pull.updated_at = CLOCK.tick()
            return json_reply(200, pull_json(repo, pull, base))
    return error_reply(404, "Not Found")


# --------------------------------------------------------------------------
# the REST dispatcher
# --------------------------------------------------------------------------

_REPO = r"([^/]+)/([^/]+)"


def dispatch_rest(request: Request) -> Reply:
    path = request.path
    method = request.method
    if not request.authorized():
        return error_reply(401, "Bad credentials")

    if path in ("/api/v3", "/api/v3/"):
        if method == "GET":
            return rest_root(request)
        return error_reply(404, "Not Found")
    if path == "/api/v3/meta" and method == "GET":
        return rest_meta(request)
    if path == "/api/v3/user" and method == "GET":
        return json_reply(200, user_json())
    if path == "/api/v3/user/repos":
        if method == "GET":
            return rest_list_user_repos(request)
        if method == "POST":
            return rest_create_repo(request)
        return error_reply(404, "Not Found")

    match = re.fullmatch(rf"/api/v3/repos/{_REPO}", path)
    if match:
        return rest_repo(request, match.group(1), match.group(2))
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/branches", path)
    if match:
        return rest_branches(request, match.group(1), match.group(2), None)
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/branches/(.+)", path)
    if match:
        return rest_branches(request, match.group(1), match.group(2), unquote(match.group(3)))
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/commits", path)
    if match:
        return rest_commits(request, match.group(1), match.group(2))
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/git/refs", path)
    if match:
        return rest_git_ref(request, match.group(1), match.group(2), None)
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/git/(?:refs|ref)/(.+)", path)
    if match:
        ref = match.group(3)
        if not ref.startswith("refs/"):
            ref = f"refs/{ref}"
        return rest_git_ref(request, match.group(1), match.group(2), ref)
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/git/(commits|trees|blobs)/([0-9a-f]{{40}})", path)
    if match:
        return rest_git_object(request, match.group(1), match.group(2),
                               match.group(3), match.group(4))
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/contents(?:/(.*))?", path)
    if match:
        return rest_contents(request, match.group(1), match.group(2),
                             unquote(match.group(3) or ""))
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/labels", path)
    if match:
        return rest_labels(request, match.group(1), match.group(2), None)
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/labels/(.+)", path)
    if match:
        return rest_labels(request, match.group(1), match.group(2), unquote(match.group(3)))
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/issues", path)
    if match:
        return rest_issues(request, match.group(1), match.group(2), None)
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/issues/(\d+)/comments", path)
    if match:
        return rest_comments(request, match.group(1), match.group(2), int(match.group(3)))
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/issues/(\d+)", path)
    if match:
        return rest_issues(request, match.group(1), match.group(2), int(match.group(3)))
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/pulls", path)
    if match:
        return rest_pulls(request, match.group(1), match.group(2), None, None)
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/pulls/(\d+)/(files|merge)", path)
    if match:
        return rest_pulls(request, match.group(1), match.group(2),
                          int(match.group(3)), match.group(4))
    match = re.fullmatch(rf"/api/v3/repos/{_REPO}/pulls/(\d+)", path)
    if match:
        return rest_pulls(request, match.group(1), match.group(2), int(match.group(3)), None)
    return error_reply(404, "Not Found")


# --------------------------------------------------------------------------
# git smart HTTP
# --------------------------------------------------------------------------

UPLOAD_CAPS_V0 = ("multi_ack_detailed side-band-64k thin-pack ofs-delta shallow "
                  "agent=lumen-forge/1.0")
RECEIVE_CAPS_V0 = "report-status delete-refs side-band-64k ofs-delta agent=lumen-forge/1.0"
V2_CAPS = ["agent=lumen-forge/1.0", "ls-refs=unborn", "fetch=shallow filter",
           "object-format=sha1"]


def _sorted_refs(repo: Repo) -> list[tuple[str, str]]:
    return sorted(repo.refs.items())


def _v0_advertisement(repo: Repo, service: str) -> bytes:
    caps = UPLOAD_CAPS_V0 if service == "git-upload-pack" else RECEIVE_CAPS_V0
    caps = f"{caps} symref=HEAD:refs/heads/{repo.default_branch}"
    out = pkt_text(f"# service={service}\n") + PKT_FLUSH
    refs = _sorted_refs(repo)
    if not refs:
        out += pkt_line(f"{ZERO_SHA} capabilities^{{}}\0{caps}".encode()) + PKT_FLUSH
        return out
    head_sha = repo.head_sha() or ZERO_SHA
    lines = [f"{head_sha} HEAD\0{caps}"]
    for ref_name, sha in refs:
        lines.append(f"{sha} {ref_name}")
        peeled = peel_to_commit(sha)
        if STORE.type_of(sha) == "tag" and peeled:
            lines.append(f"{peeled} {ref_name}^{{}}")
    for line in lines:
        out += pkt_line(line.encode() + b"\n")
    return out + PKT_FLUSH


def _v2_advertisement() -> bytes:
    out = pkt_text("# service=git-upload-pack\n") + PKT_FLUSH
    out += pkt_text("version 2\n")
    for cap in V2_CAPS:
        out += pkt_text(cap + "\n")
    return out + PKT_FLUSH


def git_info_refs(request: Request, owner: str, name: str, service: str) -> Reply:
    repo = find_repo(owner, name)
    if repo is None:
        return error_reply(404, "Not Found")
    if not request.authorized():
        return Reply(401, b"authentication required\n", "text/plain; charset=utf-8")
    content_type = f"application/x-{service}-advertisement"
    wants_v2 = "version=2" in request.headers.get("git-protocol", "")
    if service == "git-upload-pack" and wants_v2:
        return Reply(200, _v2_advertisement(), content_type)
    return Reply(200, _v0_advertisement(repo, service), content_type)


def _sideband_packets(pack: bytes) -> bytes:
    out = b""
    for start in range(0, len(pack), 65000):
        out += pkt_line(b"\x01" + pack[start : start + 65000])
    return out


def _ls_refs(repo: Repo, args: list[str]) -> bytes:
    peel = "peel" in args
    symrefs = "symrefs" in args
    unborn = "unborn" in args
    prefixes = [arg[len("ref-prefix "):] for arg in args if arg.startswith("ref-prefix ")]

    def wanted(ref_name: str) -> bool:
        return not prefixes or any(ref_name.startswith(p) for p in prefixes)

    out = b""
    head_ref = f"refs/heads/{repo.default_branch}"
    head_sha = repo.refs.get(head_ref)
    if wanted("HEAD"):
        if head_sha is not None:
            line = f"{head_sha} HEAD"
            if symrefs:
                line += f" symref-target:{head_ref}"
            out += pkt_text(line + "\n")
        elif unborn:
            line = "unborn HEAD"
            if symrefs:
                line += f" symref-target:{head_ref}"
            out += pkt_text(line + "\n")
    for ref_name, sha in _sorted_refs(repo):
        if not wanted(ref_name):
            continue
        line = f"{sha} {ref_name}"
        if peel and STORE.type_of(sha) == "tag":
            peeled = peel_to_commit(sha)
            if peeled:
                line += f" peeled:{peeled}"
        out += pkt_text(line + "\n")
    return out + PKT_FLUSH


_FETCH_CACHE: dict[tuple, tuple[bytes, list[str], list[str]]] = {}


def _fetch_objects(repo: Repo, wants: list[str], haves: list[str],
                   shallow_client: list[str], depth: int | None) -> tuple[bytes, list[str], list[str]]:
    """Compute the pack for a fetch. Returns (pack, new shallow, unshallow).

    Identical fetches repeat constantly (concurrent clones of one repo), and
    git history is immutable, so a computed pack stays valid while the
    repository's refs stand still: the result is memoized on the request
    plus a fingerprint of the ref table."""
    fingerprint = hash(tuple(sorted(repo.refs.items())))
    key = (repo.name, fingerprint, tuple(wants), tuple(haves),
           tuple(shallow_client), depth)
    cached = _FETCH_CACHE.get(key)
    if cached is not None:
        return cached
    result = _fetch_objects_fresh(repo, wants, haves, shallow_client, depth)
    if len(_FETCH_CACHE) > 64:
        _FETCH_CACHE.clear()
    _FETCH_CACHE[key] = result
    return result


def _fetch_objects_fresh(repo: Repo, wants: list[str], haves: list[str],
                         shallow_client: list[str], depth: int | None) -> tuple[bytes, list[str], list[str]]:
    shallow_set = set(shallow_client)
    excluded_commits: set[str] = set()
    for have in haves:
        if have in shallow_set:
            excluded_commits.add(have)
        else:
            excluded_commits |= ancestors_of(have)

    excluded_objects: set[str] = set()
    for sha in excluded_commits:
        entry = STORE.get(sha)
        if entry is not None and entry[0] == "commit":
            stack = [parse_commit(entry[1])["tree"]]
            while stack:
                current = stack.pop()
                if current in excluded_objects:
                    continue
                excluded_objects.add(current)
                current_entry = STORE.get(current)
                if current_entry is not None and current_entry[0] == "tree":
                    stack.extend(child for _m, _n, child in parse_tree(current_entry[1]))

    included: dict[str, tuple[str, bytes]] = {}
    traversed: set[str] = set()
    new_shallow: list[str] = []

    def include_tree(tree_sha: str) -> None:
        stack = [tree_sha]
        while stack:
            current = stack.pop()
            if current in included or current in excluded_objects:
                continue
            entry = STORE.get(current)
            if entry is None:
                continue
            included[current] = entry
            if entry[0] == "tree":
                stack.extend(child for _m, _n, child in parse_tree(entry[1]))

    # Breadth-first by generation; a tag want contributes its commit at the
    # same generation.
    frontier: list[str] = []
    for want in wants:
        entry = STORE.get(want)
        if entry is None:
            continue
        if entry[0] == "tag":
            if want not in excluded_objects:
                included[want] = entry
            target = peel_to_commit(want)
            if target:
                frontier.append(target)
        elif entry[0] == "commit":
            frontier.append(want)

    generation = 0
    seen: set[str] = set()
    while frontier:
        generation += 1
        next_frontier: list[str] = []
        for sha in frontier:
            if sha in seen:
                continue
            seen.add(sha)
            traversed.add(sha)
            entry = STORE.get(sha)
            if entry is None or entry[0] != "commit":
                continue
            parents = parse_commit(entry[1])["parents"]
            if sha not in excluded_commits:
                included[sha] = entry
                include_tree(parse_commit(entry[1])["tree"])
                if depth is not None and generation >= depth and parents:
                    new_shallow.append(sha)
            if depth is None or generation < depth:
                next_frontier.extend(parents)
        frontier = next_frontier

    # Tag objects of this repository that point into the included set ride
    # along, as git's tag following sends them.
    included_commits = {sha for sha, (obj_type, _c) in included.items() if obj_type == "commit"}
    for ref_name, sha in repo.refs.items():
        if not ref_name.startswith("refs/tags/"):
            continue
        if STORE.type_of(sha) == "tag" and sha not in included and sha not in excluded_objects:
            if peel_to_commit(sha) in included_commits:
                entry = STORE.get(sha)
                if entry:
                    included[sha] = entry

    unshallow = [sha for sha in shallow_client
                 if sha not in new_shallow and sha in traversed]
    pack = write_pack(list(included.values()))
    return pack, new_shallow, unshallow


def _parse_fetch_args(lines: list[str]) -> dict:
    args: dict = {"wants": [], "haves": [], "shallow": [], "depth": None, "done": False}
    for line in lines:
        if line.startswith("want "):
            args["wants"].append(line[5:])
        elif line.startswith("have "):
            args["haves"].append(line[5:])
        elif line.startswith("shallow "):
            args["shallow"].append(line[8:])
        elif line.startswith("deepen "):
            try:
                args["depth"] = int(line[7:])
            except ValueError:
                pass
        elif line == "done":
            args["done"] = True
    return args


def _upload_pack_v2(request: Request, repo: Repo, packets: list[tuple[str, bytes]]) -> Reply:
    lines = [payload.decode("utf-8", "replace").rstrip("\n")
             for kind, payload in packets if kind == "line"]
    if not lines:
        return Reply(400, b"empty request\n", "text/plain")
    command = lines[0]
    # Arguments follow the first delimiter; capabilities sit before it.
    delim_index = next((i for i, (kind, _p) in enumerate(packets) if kind == "delim"), None)
    arg_lines: list[str] = []
    if delim_index is not None:
        arg_lines = [payload.decode("utf-8", "replace").rstrip("\n")
                     for kind, payload in packets[delim_index + 1:] if kind == "line"]

    if command == "command=ls-refs":
        return Reply(200, _ls_refs(repo, arg_lines), "application/x-git-upload-pack-result")
    if command == "command=fetch":
        args = _parse_fetch_args(arg_lines)
        with FORGE.lock:
            pack, new_shallow, unshallow = _fetch_objects(
                repo, args["wants"], args["haves"], args["shallow"], args["depth"])
        known_haves = [h for h in args["haves"] if STORE.type_of(h) == "commit"]
        out = pkt_text("acknowledgments\n")
        if known_haves:
            for have in known_haves:
                out += pkt_text(f"ACK {have}\n")
            out += pkt_text("ready\n")
        else:
            out += pkt_text("NAK\n")
        out += PKT_DELIM
        if new_shallow or unshallow:
            out += pkt_text("shallow-info\n")
            for sha in new_shallow:
                out += pkt_text(f"shallow {sha}\n")
            for sha in unshallow:
                out += pkt_text(f"unshallow {sha}\n")
            out += PKT_DELIM
        out += pkt_text("packfile\n")
        out += _sideband_packets(pack)
        out += PKT_FLUSH
        return Reply(200, out, "application/x-git-upload-pack-result")
    return Reply(400, b"unknown command\n", "text/plain")


def _upload_pack_v0(request: Request, repo: Repo, packets: list[tuple[str, bytes]],
                    rest: bytes) -> Reply:
    # Two sections: wants (flush) haves+done (flush). `packets` holds the
    # first section; the second is parsed from the remaining bytes.
    first = [payload.decode("utf-8", "replace").rstrip("\n")
             for kind, payload in packets if kind == "line"]
    wants: list[str] = []
    shallow_client: list[str] = []
    depth: int | None = None
    for line in first:
        if line.startswith("want "):
            wants.append(line[5:].split(" ")[0])
        elif line.startswith("shallow "):
            shallow_client.append(line[8:])
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:])
            except ValueError:
                pass
    second_packets, _ = read_pkt_lines(rest)
    haves = [payload.decode("utf-8", "replace").rstrip("\n")[5:]
             for kind, payload in second_packets
             if kind == "line" and payload.startswith(b"have ")]
    with FORGE.lock:
        pack, new_shallow, unshallow = _fetch_objects(repo, wants, haves, shallow_client, depth)
    out = b""
    for sha in new_shallow:
        out += pkt_text(f"shallow {sha}\n")
    for sha in unshallow:
        out += pkt_text(f"unshallow {sha}\n")
    known_haves = [h for h in haves if STORE.type_of(h) == "commit"]
    if known_haves:
        out += pkt_text(f"ACK {known_haves[-1]}\n")
    else:
        out += pkt_text("NAK\n")
    out += _sideband_packets(pack)
    out += PKT_FLUSH
    return Reply(200, out, "application/x-git-upload-pack-result")


def git_upload_pack(request: Request, owner: str, name: str) -> Reply:
    repo = find_repo(owner, name)
    if repo is None:
        return error_reply(404, "Not Found")
    if not request.authorized():
        return Reply(401, b"authentication required\n", "text/plain; charset=utf-8")
    packets, pos = read_pkt_lines(request.body)
    wants_v2 = "version=2" in request.headers.get("git-protocol", "")
    if wants_v2:
        return _upload_pack_v2(request, repo, packets)
    return _upload_pack_v0(request, repo, packets, request.body[pos:])


def git_receive_pack(request: Request, owner: str, name: str) -> Reply:
    repo = find_repo(owner, name)
    if repo is None:
        return error_reply(404, "Not Found")
    if not request.authorized():
        return Reply(401, b"authentication required\n", "text/plain; charset=utf-8")
    packets, pos = read_pkt_lines(request.body)
    commands: list[tuple[str, str, str]] = []
    capabilities = ""
    for index, (kind, payload) in enumerate(packets):
        if kind != "line":
            continue
        text = payload.decode("utf-8", "replace").rstrip("\n")
        if index == 0 and "\0" in text:
            text, capabilities = text.split("\0", 1)
        parts = text.split(" ")
        if len(parts) == 3:
            commands.append((parts[0], parts[1], parts[2]))
    pack_data = request.body[pos:]

    with FORGE.lock:
        unpack_ok = True
        if pack_data:
            try:
                objects = read_pack(pack_data)
                for sha, (obj_type, content) in objects.items():
                    STORE.objects.setdefault(sha, (obj_type, content))
            except Exception:
                unpack_ok = False

        reports: list[tuple[str, str | None]] = []
        changed = False
        for old, new, ref in commands:
            if not unpack_ok:
                reports.append((ref, "unpacker error"))
                continue
            current = repo.refs.get(ref, ZERO_SHA)
            if current != old:
                reports.append((ref, "stale info"))
                continue
            if new == ZERO_SHA:  # delete
                if ref == f"refs/heads/{repo.default_branch}" and current != ZERO_SHA:
                    reports.append((ref, "deletion of the current branch prohibited"))
                    continue
                repo.refs.pop(ref, None)
                changed = True
                reports.append((ref, None))
                continue
            if not STORE.has(new):
                reports.append((ref, "not our ref"))
                continue
            if ref.startswith("refs/heads/") and STORE.type_of(new) != "commit":
                reports.append((ref, "not a commit"))
                continue
            if old != ZERO_SHA and ref.startswith("refs/heads/"):
                if not is_ancestor(old, new):
                    reports.append((ref, "non-fast-forward"))
                    continue
            repo.refs[ref] = new
            changed = True
            reports.append((ref, None))
        if changed:
            repo.updated_at = CLOCK.tick()

    report = pkt_text("unpack ok\n" if unpack_ok else "unpack error\n")
    for ref, problem in reports:
        report += pkt_text(f"ok {ref}\n" if problem is None else f"ng {ref} {problem}\n")
    report += PKT_FLUSH
    if "side-band-64k" in capabilities:
        body = _sideband_packets(report) + PKT_FLUSH
    else:
        body = report
    return Reply(200, body, "application/x-git-receive-pack-result")


# --------------------------------------------------------------------------
# GraphQL v4
# --------------------------------------------------------------------------

GQL_TYPE_FIELDS: dict[str, list[str]] = {
    "Query": ["viewer", "repository", "node", "__type", "__schema", "search", "rateLimit"],
    "Mutation": ["createIssue", "closeIssue", "createPullRequest", "mergePullRequest", "addComment"],
    "User": ["id", "login", "name", "email", "databaseId", "url", "createdAt", "updatedAt"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner", "description",
                   "isPrivate", "url", "defaultBranchRef", "viewerPermission",
                   "hasIssuesEnabled", "hasPullRequestsEnabled", "issue", "issues",
                   "pullRequest", "pullRequests", "createdAt", "updatedAt",
                   "stargazerCount", "forkCount", "primaryLanguage"],
    "Issue": ["id", "databaseId", "number", "title", "state", "body", "author",
              "repository", "labels", "comments", "createdAt", "updatedAt", "url", "closed"],
    "PullRequest": ["id", "databaseId", "number", "title", "state", "body", "merged",
                    "mergeable", "headRefName", "baseRefName", "headRef", "baseRef",
                    "files", "author", "repository", "mergedAt", "mergeCommit",
                    "createdAt", "updatedAt", "url", "isDraft", "additions",
                    "deletions", "changedFiles"],
    "IssueConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestChangedFileConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "LabelConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "IssueCommentConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "IssueEdge": ["node", "cursor"],
    "PullRequestEdge": ["node", "cursor"],
    "PullRequestChangedFile": ["path", "additions", "deletions", "changeType"],
    "IssueComment": ["id", "databaseId", "body", "author", "createdAt", "updatedAt", "url"],
    "Label": ["id", "name", "color", "description"],
    "Ref": ["name", "prefix", "target"],
    "Commit": ["id", "oid", "message", "abbreviatedOid"],
    "PageInfo": ["hasNextPage", "hasPreviousPage", "startCursor", "endCursor"],
    "RateLimit": ["limit", "cost", "remaining", "resetAt", "used"],
    "CreateIssuePayload": ["clientMutationId", "issue"],
    "CloseIssuePayload": ["clientMutationId", "issue"],
    "CreatePullRequestPayload": ["clientMutationId", "pullRequest"],
    "MergePullRequestPayload": ["clientMutationId", "pullRequest"],
    "AddCommentPayload": ["clientMutationId", "commentEdge"],
    "IssueCommentEdge": ["node", "cursor"],
    "__Type": ["name", "kind", "description", "fields", "enumValues", "possibleTypes",
               "inputFields", "ofType", "specifiedByURL"],
    "__Field": ["name", "description", "args", "type", "isDeprecated", "deprecationReason"],
    "__EnumValue": ["name", "description", "isDeprecated", "deprecationReason"],
    "__InputValue": ["name", "description", "type", "defaultValue"],
    "__Schema": ["types", "queryType", "mutationType", "directives"],
    "__Directive": ["name", "description", "locations", "args"],
}

GQL_ENUMS: dict[str, list[str]] = {
    "SearchType": ["ISSUE", "REPOSITORY", "USER", "DISCUSSION"],
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
    "OrderDirection": ["ASC", "DESC"],
    "IssueOrderField": ["CREATED_AT", "UPDATED_AT", "COMMENTS"],
    "RepositoryPermission": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"],
    "PullRequestMergeMethod": ["MERGE", "SQUASH", "REBASE"],
}

GQL_SCALARS = {"String", "Int", "Float", "Boolean", "ID", "URI", "DateTime", "HTML", "GitObjectID"}


class GQLError(Exception):
    pass


def _gql_tokens(text: str) -> list[tuple[str, object]]:
    tokens: list[tuple[str, object]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n,":
            i += 1
        elif ch == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("...", i):
            tokens.append(("...", "..."))
            i += 3
        elif ch in "{}()[]:$=!@|&":
            tokens.append((ch, ch))
            i += 1
        elif ch == '"':
            if text.startswith('"""', i):
                end = text.index('"""', i + 3)
                tokens.append(("STRING", text[i + 3 : end]))
                i = end + 3
            else:
                j = i + 1
                buf = ""
                while j < n and text[j] != '"':
                    if text[j] == "\\" and j + 1 < n:
                        esc = text[j + 1]
                        buf += {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\",
                                "/": "/", "b": "\b", "f": "\f"}.get(esc, esc)
                        j += 2
                    else:
                        buf += text[j]
                        j += 1
                tokens.append(("STRING", buf))
                i = j + 1
        elif ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] in ".eE+-"):
                j += 1
            raw = text[i:j]
            tokens.append(("NUMBER", float(raw) if ("." in raw or "e" in raw or "E" in raw) else int(raw)))
            i = j
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("NAME", text[i:j]))
            i = j
        else:
            raise GQLError(f"unexpected character {ch!r} in document")
    tokens.append(("EOF", None))
    return tokens


class _GQLParser:
    def __init__(self, text: str):
        self.tokens = _gql_tokens(text)
        self.pos = 0

    def peek(self) -> str:
        return self.tokens[self.pos][0]

    def next(self) -> tuple[str, object]:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect(self, kind: str) -> object:
        token = self.next()
        if token[0] != kind:
            raise GQLError(f"expected {kind}, found {token[0]}")
        return token[1]

    def parse_document(self) -> dict:
        operations = []
        fragments: dict[str, dict] = {}
        while self.peek() != "EOF":
            if self.peek() == "{":
                operations.append({"kind": "query", "name": None, "var_defs": {},
                                   "selection": self.parse_selection_set()})
                continue
            keyword = self.expect("NAME")
            if keyword in ("query", "mutation", "subscription"):
                name = self.next()[1] if self.peek() == "NAME" else None
                var_defs = self.parse_var_defs() if self.peek() == "(" else {}
                self.skip_directives()
                operations.append({"kind": keyword, "name": name, "var_defs": var_defs,
                                   "selection": self.parse_selection_set()})
            elif keyword == "fragment":
                frag_name = self.expect("NAME")
                self.expect("NAME")  # 'on'
                on_type = self.expect("NAME")
                self.skip_directives()
                fragments[str(frag_name)] = {"on": on_type, "selection": self.parse_selection_set()}
            else:
                raise GQLError(f"unexpected definition {keyword!r}")
        return {"operations": operations, "fragments": fragments}

    def parse_var_defs(self) -> dict:
        defs: dict[str, object] = {}
        self.expect("(")
        while self.peek() != ")":
            self.expect("$")
            name = self.expect("NAME")
            self.expect(":")
            self.parse_type()
            default = None
            if self.peek() == "=":
                self.next()
                default = self.parse_value(const=True)
            self.skip_directives()
            defs[str(name)] = default
        self.expect(")")
        return defs

    def parse_type(self) -> None:
        if self.peek() == "[":
            self.next()
            self.parse_type()
            self.expect("]")
        else:
            self.expect("NAME")
        if self.peek() == "!":
            self.next()

    def skip_directives(self) -> None:
        while self.peek() == "@":
            self.next()
            self.expect("NAME")
            if self.peek() == "(":
                self.parse_arguments()

    def parse_arguments(self) -> dict:
        args: dict[str, object] = {}
        self.expect("(")
        while self.peek() != ")":
            name = self.expect("NAME")
            self.expect(":")
            args[str(name)] = self.parse_value()
        self.expect(")")
        return args

    def parse_value(self, const: bool = False) -> tuple:
        kind, value = self.next()
        if kind == "$":
            name = self.expect("NAME")
            return ("var", str(name))
        if kind == "STRING":
            return ("lit", value)
        if kind == "NUMBER":
            return ("lit", value)
        if kind == "NAME":
            if value == "true":
                return ("lit", True)
            if value == "false":
                return ("lit", False)
            if value == "null":
                return ("lit", None)
            return ("enum", value)
        if kind == "[":
            items = []
            while self.peek() != "]":
                items.append(self.parse_value(const))
            self.expect("]")
            return ("list", items)
        if kind == "{":
            obj = {}
            while self.peek() != "}":
                name = self.expect("NAME")
                self.expect(":")
                obj[str(name)] = self.parse_value(const)
            self.expect("}")
            return ("obj", obj)
        raise GQLError(f"unexpected value token {kind}")

    def parse_selection_set(self) -> list[dict]:
        self.expect("{")
        selections = []
        while self.peek() != "}":
            if self.peek() == "...":
                self.next()
                if self.peek() == "NAME" and self.tokens[self.pos][1] == "on":
                    self.next()
                    on_type = self.expect("NAME")
                    self.skip_directives()
                    selections.append({"inline_on": on_type,
                                       "selection": self.parse_selection_set()})
                else:
                    name = self.expect("NAME")
                    self.skip_directives()
                    selections.append({"spread": name})
                continue
            alias = None
            name = self.expect("NAME")
            if self.peek() == ":":
                self.next()
                alias = name
                name = self.expect("NAME")
            args = self.parse_arguments() if self.peek() == "(" else {}
            directives: dict[str, object] = {}
            while self.peek() == "@":
                self.next()
                directive = self.expect("NAME")
                if self.peek() == "(":
                    directives[str(directive)] = self.parse_arguments()
            selection = self.parse_selection_set() if self.peek() == "{" else None
            selections.append({"name": name, "alias": alias, "args": args,
                               "selection": selection, "directives": directives})
        self.expect("}")
        return selections


def _gql_eval(node: tuple, variables: dict) -> object:
    kind, value = node
    if kind == "var":
        return variables.get(value)  # type: ignore[arg-type]
    if kind == "lit":
        return value
    if kind == "enum":
        return value
    if kind == "list":
        return [_gql_eval(item, variables) for item in value]  # type: ignore[union-attr]
    if kind == "obj":
        return {key: _gql_eval(item, variables) for key, item in value.items()}  # type: ignore[union-attr]
    return None


def _gql_cursor(index: int) -> str:
    return base64.b64encode(f"cursor:{index}".encode()).decode()


def _gql_cursor_index(cursor: object) -> int:
    try:
        raw = base64.b64decode(str(cursor)).decode()
        return int(raw.split(":", 1)[1])
    except Exception:
        return -1


def _gql_paginate(edge_type: str, nodes: list, args: dict) -> dict:
    first = args.get("first")
    after = args.get("after")
    start = _gql_cursor_index(after) + 1 if after else 0
    if isinstance(first, int) and first >= 0:
        page = nodes[start : start + first]
    else:
        page = nodes[start:]
    edges = [(edge_type, {"node": node, "cursor": _gql_cursor(start + i)})
             for i, node in enumerate(page)]
    page_info = {
        "hasNextPage": start + len(page) < len(nodes),
        "hasPreviousPage": start > 0,
        "startCursor": _gql_cursor(start) if page else None,
        "endCursor": _gql_cursor(start + len(page) - 1) if page else None,
    }
    return {"nodes": page, "edges": edges, "pageInfo": ("PageInfo", page_info),
            "totalCount": len(nodes)}


def _repo_by_dbid(dbid: int) -> Repo | None:
    for repo in FORGE.repos.values():
        if repo.id == dbid:
            return repo
    return None


def _issue_by_dbid(dbid: int) -> tuple[Repo, Issue] | None:
    for repo in FORGE.repos.values():
        for issue in repo.issues.values():
            if issue.id == dbid:
                return repo, issue
    return None


def _pull_by_dbid(dbid: int) -> tuple[Repo, Pull] | None:
    for repo in FORGE.repos.values():
        for pull in repo.pulls.values():
            if pull.id == dbid:
                return repo, pull
    return None


def _gql_field(typename: str, source: object, field: str, args: dict, ctx: dict) -> object:
    if isinstance(source, dict):
        return source.get(field)
    base = ctx["base"]

    if typename == "Query":
        if field == "viewer":
            return ("User", None)
        if field == "repository":
            if args.get("owner") != OWNER_LOGIN:
                return None
            repo = FORGE.repos.get(str(args.get("name")))
            return ("Repository", repo) if repo is not None else None
        if field == "node":
            parsed = parse_global_id(str(args.get("id", "")))
            if parsed is None:
                return None
            type_name, dbid = parsed
            if type_name == "Repository":
                repo = _repo_by_dbid(dbid)
                return ("Repository", repo) if repo is not None else None
            if type_name == "Issue":
                found = _issue_by_dbid(dbid)
                return ("Issue", found) if found else None
            if type_name == "PullRequest":
                found = _pull_by_dbid(dbid)
                return ("PullRequest", found) if found else None
            if type_name == "User" and dbid == OWNER_ID:
                return ("User", None)
            return None
        if field == "__type":
            name = str(args.get("name", ""))
            if name in GQL_TYPE_FIELDS or name in GQL_ENUMS or name in GQL_SCALARS:
                return ("__Type", name)
            return None
        if field == "__schema":
            return ("__Schema", None)
        if field == "rateLimit":
            return ("RateLimit", {"limit": 5000, "cost": 1, "remaining": 4999,
                                  "resetAt": iso(_trial_now() + 3600), "used": 1})
        return None

    if typename == "Mutation":
        inp = args.get("input") or {}
        if field == "createIssue":
            parsed = parse_global_id(str(inp.get("repositoryId", "")))
            repo = _repo_by_dbid(parsed[1]) if parsed and parsed[0] == "Repository" else None
            if repo is None:
                raise GQLError("Could not resolve repository")
            now = CLOCK.tick()
            issue = Issue(FORGE.next_issue_id, repo.next_number, str(inp.get("title", "")),
                          str(inp.get("body") or ""), [], now)
            FORGE.next_issue_id += 1
            repo.next_number += 1
            repo.issues[issue.number] = issue
            repo.updated_at = now
            return ("CreateIssuePayload", {"clientMutationId": inp.get("clientMutationId"),
                                           "issue": ("Issue", (repo, issue))})
        if field == "closeIssue":
            parsed = parse_global_id(str(inp.get("issueId", "")))
            found = _issue_by_dbid(parsed[1]) if parsed and parsed[0] == "Issue" else None
            if found is None:
                raise GQLError("Could not resolve issue")
            repo, issue = found
            now = CLOCK.tick()
            issue.state = "closed"
            issue.closed_at = now
            issue.updated_at = now
            return ("CloseIssuePayload", {"clientMutationId": inp.get("clientMutationId"),
                                          "issue": ("Issue", (repo, issue))})
        if field == "createPullRequest":
            parsed = parse_global_id(str(inp.get("repositoryId", "")))
            repo = _repo_by_dbid(parsed[1]) if parsed and parsed[0] == "Repository" else None
            if repo is None:
                raise GQLError("Could not resolve repository")
            head = str(inp.get("headRefName", ""))
            base_ref = str(inp.get("baseRefName", ""))
            head_sha = repo.tip(head)
            base_sha = repo.tip(base_ref)
            if head_sha is None or base_sha is None:
                raise GQLError("Head or base ref does not exist")
            for existing in repo.pulls.values():
                if (existing.state == "open" and existing.head_ref == head
                        and existing.base_ref == base_ref):
                    raise GQLError("A pull request already exists for this branch")
            now = CLOCK.tick()
            pull = Pull(FORGE.next_issue_id, repo.next_number, str(inp.get("title", "")),
                        str(inp.get("body") or ""), head, head_sha, base_ref, base_sha, now)
            FORGE.next_issue_id += 1
            repo.next_number += 1
            repo.pulls[pull.number] = pull
            return ("CreatePullRequestPayload",
                    {"clientMutationId": inp.get("clientMutationId"),
                     "pullRequest": ("PullRequest", (repo, pull))})
        if field == "mergePullRequest":
            parsed = parse_global_id(str(inp.get("pullRequestId", "")))
            found = _pull_by_dbid(parsed[1]) if parsed and parsed[0] == "PullRequest" else None
            if found is None:
                raise GQLError("Could not resolve pull request")
            repo, pull = found
            if pull.state != "open":
                raise GQLError("Pull request is not open")
            now = CLOCK.tick()
            merged_sha = squash_merge(repo, pull, now)
            if merged_sha is None:
                raise GQLError("Pull request is not mergeable")
            pull.merged = True
            pull.merged_at = now
            pull.state = "closed"
            pull.merge_commit_sha = merged_sha
            pull.updated_at = now
            return ("MergePullRequestPayload",
                    {"clientMutationId": inp.get("clientMutationId"),
                     "pullRequest": ("PullRequest", (repo, pull))})
        if field == "addComment":
            parsed = parse_global_id(str(inp.get("subjectId", "")))
            holder: Issue | Pull | None = None
            repo = None
            if parsed and parsed[0] == "Issue":
                found_issue = _issue_by_dbid(parsed[1])
                if found_issue:
                    repo, holder = found_issue
            elif parsed and parsed[0] == "PullRequest":
                found_pull = _pull_by_dbid(parsed[1])
                if found_pull:
                    repo, holder = found_pull
            if holder is None or repo is None:
                raise GQLError("Could not resolve subject")
            now = CLOCK.tick()
            comment = Comment(FORGE.next_comment_id, str(inp.get("body", "")), now)
            FORGE.next_comment_id += 1
            holder.comments.append(comment)
            return ("AddCommentPayload",
                    {"clientMutationId": inp.get("clientMutationId"),
                     "commentEdge": ("IssueCommentEdge",
                                     {"node": ("IssueComment", (repo, holder, comment)),
                                      "cursor": _gql_cursor(0)})})
        return None

    if typename == "User":
        return {"id": global_id("User", OWNER_ID), "login": OWNER_LOGIN, "name": OWNER_NAME,
                "email": OWNER_EMAIL, "databaseId": OWNER_ID, "url": f"{base}/{OWNER_LOGIN}",
                "createdAt": iso(_trial_now()), "updatedAt": iso(_trial_now())}[field]

    if typename == "Repository":
        repo = source  # type: ignore[assignment]
        assert isinstance(repo, Repo)
        if field == "id":
            return global_id("Repository", repo.id)
        if field == "databaseId":
            return repo.id
        if field == "name":
            return repo.name
        if field == "nameWithOwner":
            return repo.full_name
        if field == "owner":
            return ("User", None)
        if field == "description":
            return repo.description
        if field == "isPrivate":
            return repo.private
        if field == "url":
            return f"{base}/{repo.full_name}"
        if field == "defaultBranchRef":
            if repo.head_sha() is None:
                return None
            return ("Ref", (repo, repo.default_branch))
        if field == "viewerPermission":
            return "ADMIN"
        if field in ("hasIssuesEnabled", "hasPullRequestsEnabled"):
            return True
        if field == "issue":
            issue = repo.issues.get(int(args.get("number", 0)))
            return ("Issue", (repo, issue)) if issue is not None else None
        if field == "pullRequest":
            pull = repo.pulls.get(int(args.get("number", 0)))
            return ("PullRequest", (repo, pull)) if pull is not None else None
        if field == "issues":
            items = [issue for issue in repo.issues.values() if not issue.is_pull]
            states = args.get("states")
            if states:
                items = [i for i in items if i.state.upper() in set(states)]
            order = args.get("orderBy") or {}
            items.sort(key=lambda i: (i.created_at, i.number),
                       reverse=(order.get("direction", "DESC") != "ASC"))
            nodes = [("Issue", (repo, i)) for i in items]
            return ("IssueConnection", _gql_paginate("IssueEdge", nodes, args))
        if field == "pullRequests":
            items = list(repo.pulls.values())
            states = args.get("states")
            if states:
                def pr_state(pull: Pull) -> str:
                    return "MERGED" if pull.merged else pull.state.upper()
                items = [p for p in items if pr_state(p) in set(states)]
            items.sort(key=lambda p: (p.created_at, p.number), reverse=True)
            nodes = [("PullRequest", (repo, p)) for p in items]
            return ("PullRequestConnection", _gql_paginate("PullRequestEdge", nodes, args))
        if field == "createdAt":
            return iso(repo.created_at)
        if field == "updatedAt":
            return iso(repo.updated_at)
        if field in ("stargazerCount", "forkCount"):
            return 0
        return None

    if typename == "Issue":
        repo, issue = source  # type: ignore[misc]
        if field == "id":
            return global_id("Issue", issue.id)
        if field == "databaseId":
            return issue.id
        if field == "number":
            return issue.number
        if field == "title":
            return issue.title
        if field == "state":
            return issue.state.upper()
        if field == "body":
            return issue.body
        if field == "author":
            return ("User", None)
        if field == "repository":
            return ("Repository", repo)
        if field == "labels":
            nodes = [("Label", repo.labels[name]) for name in issue.labels if name in repo.labels]
            return ("LabelConnection", _gql_paginate("IssueEdge", nodes, args))
        if field == "comments":
            nodes = [("IssueComment", (repo, issue, c)) for c in issue.comments]
            return ("IssueCommentConnection", _gql_paginate("IssueEdge", nodes, args))
        if field == "createdAt":
            return iso(issue.created_at)
        if field == "updatedAt":
            return iso(issue.updated_at)
        if field == "url":
            return f"{base}/{repo.full_name}/issues/{issue.number}"
        if field == "closed":
            return issue.state == "closed"
        return None

    if typename == "PullRequest":
        repo, pull = source  # type: ignore[misc]
        if field == "id":
            return global_id("PullRequest", pull.id)
        if field == "databaseId":
            return pull.id
        if field == "number":
            return pull.number
        if field == "title":
            return pull.title
        if field == "state":
            return "MERGED" if pull.merged else pull.state.upper()
        if field == "body":
            return pull.body
        if field == "merged":
            return pull.merged
        if field == "mergeable":
            if pull.merged or pull.state != "open":
                return "UNKNOWN"
            return pull_mergeable(repo, pull)
        if field == "headRefName":
            return pull.head_ref
        if field == "baseRefName":
            return pull.base_ref
        if field == "headRef":
            return ("Ref", (repo, pull.head_ref)) if repo.tip(pull.head_ref) else None
        if field == "baseRef":
            return ("Ref", (repo, pull.base_ref)) if repo.tip(pull.base_ref) else None
        if field == "files":
            nodes = [("PullRequestChangedFile",
                      {"path": f.get("filename") or f.get("path", ""), "additions": f.get("additions", 0),
                       "deletions": f.get("deletions", 0),
                       "changeType": str(f.get("status", "modified")).upper()})
                     for f in pull_files(repo, pull)]
            return ("PullRequestChangedFileConnection",
                    _gql_paginate("PullRequestEdge", nodes, args))
        if field == "author":
            return ("User", None)
        if field == "repository":
            return ("Repository", repo)
        if field == "mergedAt":
            return iso(pull.merged_at) if pull.merged_at is not None else None
        if field == "mergeCommit":
            return ("Commit", pull.merge_commit_sha) if pull.merge_commit_sha else None
        if field == "createdAt":
            return iso(pull.created_at)
        if field == "updatedAt":
            return iso(pull.updated_at)
        if field == "url":
            return f"{base}/{repo.full_name}/pull/{pull.number}"
        if field == "isDraft":
            return False
        if field in ("additions", "deletions"):
            files = pull_files(repo, pull)
            return sum(int(f.get(field, 0)) for f in files)
        if field == "changedFiles":
            return len(pull_files(repo, pull))
        return None

    if typename == "IssueComment":
        repo, _holder, comment = source  # type: ignore[misc]
        if field == "id":
            return global_id("IssueComment", comment.id)
        if field == "databaseId":
            return comment.id
        if field == "body":
            return comment.body
        if field == "author":
            return ("User", None)
        if field == "createdAt":
            return iso(comment.created_at)
        if field == "updatedAt":
            return iso(comment.updated_at)
        if field == "url":
            return f"{base}/{repo.full_name}"
        return None

    if typename == "Label":
        label = source  # type: ignore[assignment]
        assert isinstance(label, Label)
        return {"id": global_id("Label", label.id), "name": label.name,
                "color": label.color, "description": label.description}[field]

    if typename == "Ref":
        repo, branch = source  # type: ignore[misc]
        if field == "name":
            return branch
        if field == "prefix":
            return "refs/heads/"
        if field == "target":
            sha = repo.tip(branch)
            return ("Commit", sha) if sha else None
        return None

    if typename == "Commit":
        sha = str(source)
        if field == "id":
            return base64.b64encode(f"Commit:{sha}".encode()).decode()
        if field == "oid":
            return sha
        if field == "abbreviatedOid":
            return sha[:7]
        if field == "message":
            entry = STORE.get(sha)
            if entry and entry[0] == "commit":
                return parse_commit(entry[1])["message"]
            return ""
        return None

    if typename == "__Type":
        type_name = str(source)
        if field == "name":
            return type_name
        if field == "kind":
            if type_name in GQL_ENUMS:
                return "ENUM"
            if type_name in GQL_SCALARS:
                return "SCALAR"
            return "OBJECT"
        if field == "description":
            return None
        if field == "fields":
            names = GQL_TYPE_FIELDS.get(type_name, [])
            return [("__Field", {"name": name, "description": None, "args": [],
                                 "type": ("__Type", "String"), "isDeprecated": False,
                                 "deprecationReason": None}) for name in names]
        if field == "enumValues":
            values = GQL_ENUMS.get(type_name)
            if values is None:
                return None
            return [("__EnumValue", {"name": value, "description": None,
                                     "isDeprecated": False, "deprecationReason": None})
                    for value in values]
        if field == "possibleTypes":
            return []
        if field == "inputFields":
            return None
        return None

    if typename == "__Schema":
        if field == "types":
            names = list(GQL_TYPE_FIELDS) + list(GQL_ENUMS) + sorted(GQL_SCALARS)
            return [("__Type", name) for name in names]
        if field == "queryType":
            return ("__Type", "Query")
        if field == "mutationType":
            return ("__Type", "Mutation")
        if field == "directives":
            return []
        return None

    return None


def _gql_execute_selection(typename: str, source: object, selections: list[dict],
                           ctx: dict) -> dict:
    out: dict[str, object] = {}
    for selection in selections:
        if "inline_on" in selection:
            if selection["inline_on"] in (typename, "Node"):
                out.update(_gql_execute_selection(typename, source,
                                                  selection["selection"], ctx))
            continue
        if "spread" in selection:
            fragment = ctx["fragments"].get(selection["spread"])
            if fragment and fragment["on"] in (typename, "Node"):
                out.update(_gql_execute_selection(typename, source,
                                                  fragment["selection"], ctx))
            continue
        field = str(selection["name"])
        key = selection.get("alias") or field
        directives = selection.get("directives") or {}
        skipped = False
        for directive, dargs in directives.items():
            values = {k: _gql_eval(v, ctx["variables"]) for k, v in dargs.items()}
            if directive == "skip" and values.get("if"):
                skipped = True
            if directive == "include" and not values.get("if", True):
                skipped = True
        if skipped:
            continue
        if field == "__typename":
            out[key] = typename
            continue
        known = GQL_TYPE_FIELDS.get(typename)
        if known is not None and field not in known and field not in ("__type", "__schema"):
            ctx["errors"].append({"message": f"Field '{field}' doesn't exist on type '{typename}'"})
            out[key] = None
            continue
        args = {name: _gql_eval(value, ctx["variables"])
                for name, value in (selection.get("args") or {}).items()}
        try:
            value = _gql_field(typename, source, field, args, ctx)
        except GQLError as exc:
            ctx["errors"].append({"message": str(exc)})
            value = None
        sub = selection.get("selection")
        if sub is not None and value is not None:
            if isinstance(value, list):
                value = [_gql_execute_selection(item[0], item[1], sub, ctx)
                         if isinstance(item, tuple) else item for item in value]
            elif isinstance(value, tuple):
                value = _gql_execute_selection(value[0], value[1], sub, ctx)
        out[key] = value
    return out


def _gql_run(document_text: str, variables: dict, operation_name: str | None,
             base: str) -> dict:
    try:
        document = _GQLParser(document_text).parse_document()
    except GQLError as exc:
        return {"errors": [{"message": str(exc)}]}
    operations = document["operations"]
    operation = None
    if operation_name:
        operation = next((op for op in operations if op.get("name") == operation_name), None)
    if operation is None and operations:
        operation = operations[0]
    if operation is None:
        return {"errors": [{"message": "no operation in document"}]}
    merged_variables = {}
    for name, default in operation["var_defs"].items():
        if default is not None:
            merged_variables[name] = _gql_eval(default, {})
    merged_variables.update(variables or {})
    ctx = {"variables": merged_variables, "fragments": document["fragments"],
           "errors": [], "base": base}
    root_type = "Mutation" if operation["kind"] == "mutation" else "Query"
    with FORGE.lock:
        data = _gql_execute_selection(root_type, None, operation["selection"], ctx)
    result: dict[str, object] = {"data": data}
    if ctx["errors"]:
        result["errors"] = ctx["errors"]
    return result


def graphql_endpoint(request: Request) -> Reply:
    if not request.authorized():
        return json_reply(401, {"message": "Bad credentials",
                                "documentation_url": "https://docs.github.com/graphql"})
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        return json_reply(400, {"errors": [{"message": "Problems parsing JSON"}]})
    if isinstance(payload, list):
        results = []
        for item in payload:
            if not isinstance(item, dict):
                results.append({"errors": [{"message": "invalid operation"}]})
                continue
            results.append(_gql_run(str(item.get("query", "")),
                                    item.get("variables") or {},
                                    item.get("operationName"), request.base))
        return json_reply(200, results)
    if not isinstance(payload, dict):
        return json_reply(400, {"errors": [{"message": "invalid request body"}]})
    result = _gql_run(str(payload.get("query", "")), payload.get("variables") or {},
                      payload.get("operationName"), request.base)
    return json_reply(200, result)


# --------------------------------------------------------------------------
# MCP (JSON-RPC 2.0, Streamable HTTP)
# --------------------------------------------------------------------------

import socket
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MCP_TOOLS: list[dict] = [
    {"name": "create_repo",
     "description": "Create a repository owned by the token's user.",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string"},
                                    "description": {"type": "string"},
                                    "private": {"type": "boolean"}},
                     "required": ["name"]}},
    {"name": "get_repo",
     "description": "Read one repository by owner and name.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"}},
                     "required": ["owner", "repo"]}},
    {"name": "create_issue",
     "description": "Open an issue in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "title": {"type": "string"},
                                    "body": {"type": "string"}},
                     "required": ["owner", "repo", "title"]}},
    {"name": "list_issues",
     "description": "List a repository's issues, optionally filtered by state.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "state": {"type": "string"}},
                     "required": ["owner", "repo"]}},
    {"name": "create_pull_request",
     "description": "Open a pull request from a head branch into a base branch.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "title": {"type": "string"},
                                    "head": {"type": "string"},
                                    "base": {"type": "string"}},
                     "required": ["owner", "repo", "title", "head", "base"]}},
    {"name": "merge_pull_request",
     "description": "Squash-merge an open pull request.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "pull_number": {"type": "integer"}},
                     "required": ["owner", "repo", "pull_number"]}},
]

MCP_TOOL_REQUIRED = {tool["name"]: tool["inputSchema"].get("required", []) for tool in MCP_TOOLS}
MCP_SERVER_INFO = {"name": "lumen-forge", "version": "1.0.0"}
MCP_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]


def _mcp_tool_result(value: object) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value)}],
            "structuredContent": value, "isError": False}


def _mcp_tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _mcp_call_tool(name: str, args: dict, base: str) -> dict:
    missing = [key for key in MCP_TOOL_REQUIRED.get(name, []) if key not in args]
    if missing:
        return _mcp_tool_error(f"missing required argument(s): {', '.join(missing)}")
    with FORGE.lock:
        if name == "create_repo":
            repo_name = str(args["name"])
            if repo_name in FORGE.repos:
                return _mcp_tool_error(f"repository {repo_name} already exists")
            now = CLOCK.tick()
            repo = Repo(FORGE.next_repo_id, repo_name, bool(args.get("private", False)),
                        str(args.get("description", "") or ""), now)
            FORGE.next_repo_id += 1
            FORGE.repos[repo_name] = repo
            return _mcp_tool_result(repo_json(repo, base))
        repo = find_repo(str(args.get("owner", "")), str(args.get("repo", "")))
        if repo is None:
            return _mcp_tool_error("repository not found")
        if name == "get_repo":
            return _mcp_tool_result(repo_json(repo, base))
        if name == "create_issue":
            now = CLOCK.tick()
            issue = Issue(FORGE.next_issue_id, repo.next_number, str(args["title"]),
                          str(args.get("body", "") or ""), [], now)
            FORGE.next_issue_id += 1
            repo.next_number += 1
            repo.issues[issue.number] = issue
            repo.updated_at = now
            return _mcp_tool_result(issue_json(repo, issue, base))
        if name == "list_issues":
            state = str(args.get("state", "open"))
            items = [issue_json(repo, issue, base) for issue in repo.issues.values()
                     if state == "all" or issue.state == state]
            items.sort(key=lambda item: item["number"])
            return _mcp_tool_result(items)
        if name == "create_pull_request":
            head = str(args["head"])
            base_ref = str(args["base"])
            head_sha = repo.tip(head)
            base_sha = repo.tip(base_ref)
            if head_sha is None or base_sha is None:
                return _mcp_tool_error("head or base branch does not exist")
            for existing in repo.pulls.values():
                if (existing.state == "open" and existing.head_ref == head
                        and existing.base_ref == base_ref):
                    return _mcp_tool_error("a pull request already exists for this branch")
            now = CLOCK.tick()
            pull = Pull(FORGE.next_issue_id, repo.next_number, str(args["title"]), "",
                        head, head_sha, base_ref, base_sha, now)
            FORGE.next_issue_id += 1
            repo.next_number += 1
            repo.pulls[pull.number] = pull
            return _mcp_tool_result(pull_json(repo, pull, base))
        if name == "merge_pull_request":
            pull = repo.pulls.get(int(args["pull_number"]))
            if pull is None:
                return _mcp_tool_error("pull request not found")
            if pull.state != "open":
                return _mcp_tool_error("pull request is not open")
            now = CLOCK.tick()
            merged_sha = squash_merge(repo, pull, now)
            if merged_sha is None:
                return _mcp_tool_error("pull request is not mergeable")
            pull.merged = True
            pull.merged_at = now
            pull.state = "closed"
            pull.merge_commit_sha = merged_sha
            pull.updated_at = now
            return _mcp_tool_result(pull_json(repo, pull, base))
    return _mcp_tool_error(f"unknown tool {name}")


def _mcp_handle_one(message: object, base: str) -> tuple[int, dict | None]:
    """One JSON-RPC message -> (HTTP status, response object or None)."""
    if not isinstance(message, dict):
        return 400, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "Invalid Request"}}
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if method is None:
        if request_id is None:
            return 202, None
        return 400, {"jsonrpc": "2.0", "id": request_id,
                     "error": {"code": -32600, "message": "Invalid Request: no method"}}
    if request_id is None:  # a notification: accept and stay silent
        return 202, None
    if method == "server/discover":
        result = {"serverInfo": MCP_SERVER_INFO,
                  "capabilities": {"tools": {"listChanged": False}},
                  "supportedVersions": MCP_VERSIONS,
                  "protocolVersion": "2026-07-28"}
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": result}
    if method == "initialize":
        result = {"protocolVersion": str(params.get("protocolVersion") or "2025-11-25"),
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": MCP_SERVER_INFO,
                  "instructions": "Lumen Forge: a git forge with repositories, issues and pull requests."}
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": result}
    if method == "ping":
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        tool_name = str(params.get("name", ""))
        if tool_name not in MCP_TOOL_REQUIRED:
            return 200, {"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": -32602, "message": f"unknown tool: {tool_name}"}}
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        result = _mcp_call_tool(tool_name, arguments, base)
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": result}
    return 200, {"jsonrpc": "2.0", "id": request_id,
                 "error": {"code": -32601, "message": f"method not found: {method}"}}


def mcp_endpoint(request: Request) -> Reply:
    if not request.authorized():
        return json_reply(401, {"message": "Bad credentials"})
    if request.method != "POST":
        return json_reply(405, {"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32600, "message": "method not allowed"}})
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return json_reply(400, {"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32700, "message": "Parse error"}})
    if isinstance(payload, list):
        responses = []
        status = 200
        for item in payload:
            item_status, response = _mcp_handle_one(item, request.base)
            if response is not None:
                responses.append(response)
            status = item_status if item_status != 202 else status
        if not responses:
            return Reply(202, b"", "application/json; charset=utf-8")
        return json_reply(status, responses)
    status, response = _mcp_handle_one(payload, request.base)
    if response is None:
        return Reply(202, b"", "application/json; charset=utf-8")
    return json_reply(status, response)


# --------------------------------------------------------------------------
# HTTP plumbing + lifecycle
# --------------------------------------------------------------------------

def dispatch(request: Request) -> Reply:
    path = request.path
    if path == "/healthz":
        return json_reply(200, {"status": "ok"})
    if path == "/api/graphql":
        if request.method != "POST":
            return error_reply(404, "Not Found")
        return graphql_endpoint(request)
    if path == "/mcp":
        return mcp_endpoint(request)
    if path == "/api/v3" or path.startswith("/api/v3/"):
        return dispatch_rest(request)
    parts = [part for part in path.split("/") if part != ""]
    action = None
    if len(parts) == 4 and parts[2] == "info" and parts[3] == "refs":
        action = "info/refs"
    elif len(parts) == 3 and parts[2] in ("git-upload-pack", "git-receive-pack"):
        action = parts[2]
    if action is not None:
        owner = parts[0]
        repo_name = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
        if action == "info/refs":
            service = request.query_one("service", "")
            if service not in ("git-upload-pack", "git-receive-pack"):
                return error_reply(404, "Not Found")
            return git_info_refs(request, owner, repo_name, service)
        if action == "git-upload-pack":
            return git_upload_pack(request, owner, repo_name)
        return git_receive_pack(request, owner, repo_name)
    return error_reply(404, "Not Found")


class ForgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LumenForge/1.0"

    def setup(self) -> None:
        super().setup()
        try:
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib name
        pass

    def _handle(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        parsed = urlparse(self.path)
        headers = {key.lower(): value for key, value in self.headers.items()}
        request = Request(method, unquote(parsed.path), parse_qs(parsed.query), headers, body)
        try:
            reply = dispatch(request)
        except Exception:  # the forge must survive any single bad request
            reply = error_reply(500, "Internal Server Error")
        with FORGE.lock:
            FORGE.request_counter += 1
            request_id = f"lumen-{FORGE.request_counter:06d}"
        payload = reply.body
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-GitHub-Request-Id", request_id)
        for key, value in reply.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_HEAD(self) -> None:
        self._handle("HEAD")


def _env_request(url: str, payload: dict | None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.status, json.loads(response.read().decode() or "{}")


def _register_and_wait(env_url: str, port: int) -> None:
    delay = 0.25
    while True:
        try:
            status, _ = _env_request(f"{env_url}/register", {"port": port})
            if status in (200, 409):
                break
            if status in (400, 405):
                raise RuntimeError(f"register answered HTTP {status}")
        except urllib.error.HTTPError as exc:
            if exc.code in (200, 409):
                break
            if exc.code in (400, 405):
                raise RuntimeError(f"register answered HTTP {exc.code}") from exc
        except Exception:
            pass
        time.sleep(delay)
        delay = min(2.0, delay * 2)
    delay = 0.25
    while True:
        try:
            _status, body = _env_request(f"{env_url}/done", None)
            if body.get("done") is True:
                return
        except Exception:
            pass
        time.sleep(delay)
        delay = min(2.0, delay * 2)


class ForgeServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 512


def main() -> int:
    server = ForgeServer(("0.0.0.0", 0), ForgeHandler)
    port = int(server.server_address[1])
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not env_url:
        print(f"[lumen-forge] listening on port {port} (standalone dev mode)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[lumen-forge] listening on port {port}; registering with {env_url}", flush=True)
    _register_and_wait(env_url, port)
    server.shutdown()
    print("[lumen-forge] done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
