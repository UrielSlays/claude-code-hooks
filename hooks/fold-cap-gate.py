#!/usr/bin/env python3
"""Require a fresh surface review before another fix commit on a branch.

The review must be cited as Walk: <path> and have a WALK- basename.
Unreadable messages and git errors fail open."""

import os
import tempfile
import sys

def fast_path():
    if os.environ.get("HOOK_COMPACTION_AUDITOR") == "1" or os.environ.get("HOOK_FOLD_CAP_SKIP") == "1":
        return None
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        return None
    return raw if b"commit" in raw else None

if __name__ == "__main__":
    _RAW = fast_path()
    if _RAW is None:
        sys.exit(0)

import json  
import re  
import subprocess  
import time  

STATE_DIR = os.environ.get("FOLD_CAP_STATE_DIR") or os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir(), "claude-fold-cap")
_OPT = r"""(?:-C\s*(?:"[^"]*"|'[^']*'|[^\s;&|]+)|-c\s*[^\s;&|]+|--[\w-]+(?:=[^\s;&|]+)?|-[a-zA-Z]+)"""
GIT_COMMIT = re.compile(r"(?<![\w.-])git(?:\.exe)?((?:\s+" + _OPT + r")*)\s+commit(?![\w-])")
GIT_ANY = re.compile(r"(?<![\w.-])git(?:\.exe)?\s")
C_OPT = re.compile(r"""-C\s*("[^"]*"|'[^']*'|[^\s;&|]+)""")
CD = re.compile(r"""(?:^|[;&|(\n]\s*|&&\s*)(?:cd|pushd|Set-Location|Push-Location|sl)\s+(?:-(?:LiteralPath|Path)\s+)?("[^"]*"|'[^']*'|[^\s;&|]+)""", re.I)
MSG_FLAG = re.compile(r"""(?:(?<=\s)|^)(?:(-[a-zA-Z]*m)|(--message))(?=[\s="'@$]|$)""")
FILE_FLAG = re.compile(r"""(?:(?<=\s)|^)(?:-[a-zA-Z]*F|--file)(?=[\s="']|$)""")
FIX_SUBJECT = re.compile(r"^\s*fix(\(|:)")
WALK_LINE = re.compile(r"^\s*Walk:\s*(.+?)\s*$", re.M)

def log(event: str, **kw) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": int(time.time()), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **kw}, ensure_ascii=False) + "\n")
    except Exception:
        pass

def unq(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    return tok

def commit_segment(cmd: str, m) -> str:
    nxt = GIT_ANY.search(cmd, m.end())
    return cmd[m.end(): nxt.start() if nxt else len(cmd)]

def repo_of(cmd: str, m, cwd: str) -> str:
    target = None
    c = C_OPT.search(m.group(1) or "")
    if c:
        target = unq(c.group(1))
    else:
        cds = [x for x in CD.finditer(cmd[: m.start()])]
        if cds:
            target = unq(cds[-1].group(1))
    if target:
        target = os.path.expanduser(target)
        if not os.path.isabs(target) and cwd:
            target = os.path.join(cwd, target)
        if os.path.isdir(target):
            return target
    return cwd

def message_of(seg: str, repo: str):
    mf = MSG_FLAG.search(seg)
    ff = FILE_FLAG.search(seg)
    if ff and (not mf or ff.start() < mf.start()):
        rest = seg[ff.end():]
        rest = rest[1:] if rest.startswith("=") else rest.lstrip(" \t")
        m = re.match(r"""("[^"]*"|'[^']*'|[^\s;&|]+)""", rest)
        if not m:
            return None, "file-unparsed"
        path = os.path.expanduser(unq(m.group(1)).replace("`", ""))
        if not os.path.isabs(path):
            path = os.path.join(repo, path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(), "file"
        except Exception:
            return None, "file-unreadable"
    if not mf:
        return None, "no -m"
    rest = seg[mf.end():]
    rest = rest[1:] if rest.startswith("=") else rest.lstrip(" \t")
    m = re.match(r"""(['"])\$\(\s*cat\s*<<-?\s*(['"]?)(\w+)\2[^\n]*\n(.*?)\n[ \t]*\3[ \t]*\n""", rest, re.S)
    if m:
        return m.group(4), "heredoc"
    m = re.match(r"""@(['"])[ \t]*\r?\n(.*?)\r?\n\1@""", rest, re.S)
    if m:
        return m.group(2), "here-string"
    m = re.match(r'"((?:[^"\\`]|\\.|`.)*)"', rest, re.S)
    if m:
        body = m.group(1)
        if body.lstrip().startswith("$"):
            return None, "substitution"
        return re.sub(r"\\([\"\\$`])", r"\1", body), "double-quoted"
    m = re.match(r"'([^']*)'", rest, re.S)
    if m:
        return m.group(1), "single-quoted"
    m = re.match(r"""([^\s"'$@(][^\s;&|]*)""", rest)
    if m:
        return m.group(1), "bare"
    return None, "unseen"

def git(repo: str, args: list) -> str:
    r = subprocess.run(["git", "-C", repo] + args, capture_output=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} exited {r.returncode}: {r.stderr.decode('utf-8', 'replace')[:200]}")
    return r.stdout.decode("utf-8", "replace")

def refuse(msg: str) -> int:
    sys.stderr.buffer.write((msg + "\n").encode("utf-8"))
    sys.stderr.flush()
    return 2

def gate(raw: bytes) -> int:
    try:
        p = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return 0
    if not isinstance(p, dict):
        return 0
    ti = p.get("tool_input")
    cmd = ti.get("command") if isinstance(ti, dict) else None
    if not isinstance(cmd, str):
        return 0
    commits = list(GIT_COMMIT.finditer(cmd))
    if not commits:
        return 0
    cwd = p.get("cwd") or os.getcwd()
    for m in commits:
        seg = commit_segment(cmd, m)
        repo = repo_of(cmd, m, cwd)
        message, how = message_of(seg, repo)
        if message is None:
            log("fail-open-message-unseen", how=how, cmd=cmd[:200])
            continue
        subject = message.lstrip("\ufeff").split("\n")[0]
        if not FIX_SUBJECT.search(subject):
            log("allow-not-a-fix", subject=subject[:120])
            continue
        try:
            branch = git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
            prior = [ln for ln in git(repo, ["log", "--format=%H%x09%ct%x09%s", "origin/main..HEAD"]).splitlines() if ln.strip()]
        except Exception as e:
            log("fail-open-git", repo=repo, error=str(e)[:200])
            continue
        prior_fix = [ln.split("\t", 2) for ln in prior if len(ln.split("\t", 2)) == 3 and FIX_SUBJECT.search(ln.split("\t", 2)[2])]
        if not prior_fix:
            log("allow-first-fix-on-branch", repo=repo, branch=branch, subject=subject[:120])
            continue
        last_sha, last_ct, last_subject = prior_fix[0]
        try:
            last_ct = int(last_ct)
        except Exception:
            log("fail-open-ct", repo=repo)
            continue
        walks = [w.strip() for w in WALK_LINE.findall(message)]
        fresh = None
        reasons = []
        for w in walks:
            path = os.path.expanduser(unq(w).replace("`", ""))
            if not os.path.isabs(path):
                path = os.path.join(repo, path)
            if not os.path.isfile(path):
                reasons.append(f"{w}: no such file")
                continue
            if not os.path.basename(path).startswith("WALK-"):
                reasons.append(f"{w}: the basename does not start with WALK-")
                continue
            try:
                mt = os.path.getmtime(path)
            except Exception:
                reasons.append(f"{w}: mtime unreadable")
                continue
            if mt <= last_ct:
                reasons.append(f"{w}: last written before the previous fix commit {last_sha[:8]} ({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(last_ct))})")
                continue
            fresh = path
            break
        if fresh:
            log("allow-fresh-walk", repo=repo, branch=branch, walk=fresh, prior_fix=len(prior_fix))
            continue
        log("refused-no-fresh-walk", repo=repo, branch=branch, subject=subject[:120], prior_fix=len(prior_fix), last_fix=last_sha[:8], walks=walks[:5], reasons=reasons[:5])
        return refuse(
            f"fold cap: refused. `{subject[:100]}` would be fix commit number {len(prior_fix) + 1} on `{branch}` since origin/main (the last: "
            f"{last_sha[:8]} \"{last_subject[:80]}\"), and the message cites no fresh whole-surface walk"
            + (f" ({'; '.join(reasons[:3])})" if reasons else "")
            + ". A second instance of a class after its walk-and-close is the stop, never a third: the walk was scoped by the finding — "
            "re-enumerate the surface one level up, from the type or the API, bank it as a file whose name starts with WALK- written "
            "after that commit, and cite it in the message as `Walk: <path>`. "
            "This refusal is the driver's to clear: never hand the commit to the owner."
        )
    return 0

def main(raw: bytes) -> int:
    try:
        return gate(raw)
    except Exception as e:  
        log("fail-open-error", error=repr(e)[:300])
        return 0

if __name__ == "__main__":
    sys.exit(main(_RAW))
