#!/usr/bin/env python3
"""Check for a failing test before a source or test commit.

Subject verdict words produce a note. A local model client judges the witness.
Missing context, client errors and timeouts allow the command and are logged."""

import os
import tempfile
import sys

def fast_path():
    if os.environ.get("HOOK_COMPACTION_AUDITOR") == "1" or os.environ.get("HOOK_RED_WITNESS_SKIP") == "1":
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
import shutil  
import subprocess  
import time  

STATE_DIR = os.environ.get("COMMIT_RED_WITNESS_STATE_DIR") or os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir(), "claude-commit-red-witness")
TIMEOUT_S = 240
ALLOWED = "Read,Grep,Glob,Bash(git diff *),Bash(git status *),Bash(python *)"
DISALLOWED = "Write,Edit,NotebookEdit,Agent,Workflow,WebFetch,WebSearch"
SCRUB = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET",
         "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_ENTRYPOINT",
         "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_EXECPATH")
VERDICT = re.compile(r"\b(GREEN|CLEAN|PASS|PASSED|GO|ZERO|FIXED)\b")
_OPT = r"""(?:-C\s*(?:"[^"]*"|'[^']*'|[^\s;&|]+)|-c\s*[^\s;&|]+|--[\w-]+(?:=[^\s;&|]+)?|-[a-zA-Z]+)"""
GIT_COMMIT = re.compile(r"(?<![\w.-])git(?:\.exe)?((?:\s+" + _OPT + r")*)\s+commit(?![\w-])")
GIT_ANY = re.compile(r"(?<![\w.-])git(?:\.exe)?\s")
C_OPT = re.compile(r"""-C\s*("[^"]*"|'[^']*'|[^\s;&|]+)""")
CD = re.compile(r"""(?:^|[;&|(\n]\s*|&&\s*)(?:cd|pushd|Set-Location|Push-Location|sl)\s+(?:-(?:LiteralPath|Path)\s+)?("[^"]*"|'[^']*'|[^\s;&|]+)""", re.I)
MSG_FLAG = re.compile(r"""(?:(?<=\s)|^)(?:(-[a-zA-Z]*m)|(--message))(?=[\s="'@$]|$)""")
FILE_FLAG = re.compile(r"""(?:(?<=\s)|^)(?:-[a-zA-Z]*F|--file)(?=[\s="']|$)""")
ADDS = re.compile(r"(?<![\w.-])git(?:\.exe)?(?:\s+" + _OPT + r")*\s+add(?![\w-])")
ALL_FLAG = re.compile(r"""(?:(?<=\s)|^)(?:-(?!-)[a-zA-Z]*a[a-zA-Z]*|--all)(?=[\s"']|$)""")
SRC_TESTS = re.compile(r"(^|/)(src|tests)/")

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

def message_of(seg: str):
    mf = MSG_FLAG.search(seg)
    ff = FILE_FLAG.search(seg)
    if ff and (not mf or ff.start() < mf.start()):
        return None, "file"
    if not mf:
        return None, "no -m"
    rest = seg[mf.end():]
    rest = rest[1:] if rest.startswith("=") else rest.lstrip(" \t")
    m = re.match(r"""(['"])\$\(\s*cat\s*<<-?\s*(['"]?)(\w+)\2[^\n]*\n(.*?)\n[ \t]*\3[ \t]*\n""", rest, re.S)
    if m:
        return m.group(4).split("\n")[0], "heredoc"
    m = re.match(r"""@(['"])[ \t]*\r?\n(.*?)\r?\n\1@""", rest, re.S)
    if m:
        return m.group(2).split("\n")[0], "here-string"
    m = re.match(r'"((?:[^"\\`]|\\.|`.)*)"', rest, re.S)
    if m:
        body = m.group(1)
        if body.lstrip().startswith("$"):
            return None, "substitution"
        body = re.sub(r"\\([\"\\$`])", r"\1", body)
        return body.split("\n")[0], "double-quoted"
    m = re.match(r"'([^']*)'", rest, re.S)
    if m:
        return m.group(1).split("\n")[0], "single-quoted"
    m = re.match(r"""([^\s"'$@(][^\s;&|]*)""", rest)
    if m:
        return m.group(1), "bare"
    return None, "unseen"

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

def git_lines(repo: str, args: list) -> list:
    r = subprocess.run(["git", "-C", repo] + args, capture_output=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} exited {r.returncode}: {r.stderr.decode('utf-8', 'replace')[:200]}")
    return [x.strip() for x in r.stdout.decode("utf-8", "replace").splitlines() if x.strip()]

def claude_argv() -> list:
    b = os.environ.get("HOOK_CLAUDE_BIN") or shutil.which("claude") or "claude"
    head = [sys.executable, "-B", b] if b.lower().endswith(".py") else [b]
    return head + ["-p", "--model", "opus", "--max-turns", "25", "--output-format", "json", "--no-session-persistence",
                   "--allowedTools", ALLOWED, "--disallowedTools", DISALLOWED, "--permission-mode", "dontAsk"]

def child_env() -> dict:
    env = dict(os.environ)
    for k in SCRUB:
        env.pop(k, None)
    env["HOOK_COMPACTION_AUDITOR"] = "1"
    env["CLAUDE_CODE_SAFE_MODE"] = "1"
    return env

def kill_tree(proc) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass

def strip_fence(t: str) -> str:
    m = re.search(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", t, re.S)
    return m.group(1).strip() if m else t.strip()

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
        subject, how = message_of(commit_segment(cmd, m))
        if subject is None:
            log("subject-unseen", how=how, cmd=cmd[:200])
            continue
        w = VERDICT.search(subject)
        if w:

            log("noted-verdict-word", word=w.group(1), subject=subject[:200])
            try:
                sys.stdout.write(f"commit-red-witness: the subject carries the verdict word '{w.group(1)}' — a note, not a refusal "
                                 "(canon: no verdict words in commit subjects while a seat is open).\n")
                sys.stdout.flush()
            except Exception:
                pass

    m = commits[0]
    seg = commit_segment(cmd, m)
    repo = repo_of(cmd, m, cwd)
    try:
        staged = git_lines(repo, ["diff", "--cached", "--name-only"])
        cands = set(staged)
        if ADDS.search(cmd) or ALL_FLAG.search(seg):
            cands |= set(git_lines(repo, ["diff", "--name-only"]))
            cands |= set(git_lines(repo, ["ls-files", "--others", "--exclude-standard"]))
    except Exception as e:
        log("fail-open-git", repo=repo, error=str(e)[:200])
        return 0
    relevant = sorted(f for f in cands if SRC_TESTS.search(f.replace("\\", "/")))
    if not relevant:
        log("allow-no-src-tests", repo=repo, staged=len(staged))
        return 0
    tpath = p.get("transcript_path") or ""
    if not tpath or not os.path.isfile(tpath):
        log("fail-open-no-transcript", transcript=tpath)
        return 0
    shown = ", ".join(relevant[:40]) + (f" … (+{len(relevant) - 40})" if len(relevant) > 40 else "")
    prompt = (
        f"A `git commit` is about to run in `{repo}`: `{cmd[:600]}`. Run `git diff --cached --name-only`. "
        f"(The hook saw these src/tests candidates — staged now, plus what the command itself stages: {shown}.) "
        f"For every staged file under `tests/` or `src/`, find in the session transcript `{tpath}` (iterate lines with Python; only the part "
        "after its last `compact_boundary` line and the 400 lines before that) OR in this session's subagent transcripts "
        f"`{os.path.join(os.path.dirname(tpath), str(p.get('session_id') or ''), 'subagents')}` (`agent-*.jsonl`, each read whole — a worker's own "
        "red counts when it names the staged test file) the RED witness: a test-run output showing `failed` that names one "
        "of the staged test files (or a `RED-*.log` banked by name) BEFORE the staged `src/` change was written. Reply JSON only: "
        '{"ok":true|false,"reason":"≤ 400 chars naming the file with no red, or \'red witnessed at line N\'"}. '
        "If no `tests/` or `src/` file is staged, ok:true."
        
        " Tool note: you may use Read, Grep, Glob and Bash commands whose FIRST word is `python`, `git diff` or `git status`; `cd`, "
        "`&&` / `;` chains, any other program and PowerShell are denied."
    )
    timeout = int(os.environ.get("HOOK_RED_WITNESS_TIMEOUT_S") or TIMEOUT_S)
    argv = claude_argv()
    t0 = time.time()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=repo, env=child_env(), creationflags=flags)
    try:
        out, err = proc.communicate(input=prompt.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        log("fail-open-timeout", repo=repo, timeout_s=timeout, files=relevant[:20])
        return 0
    wall = round(time.time() - t0, 1)
    text = out.decode("utf-8", "replace")
    denials = None
    try:
        env = json.loads(text)
        res = env.get("result") if isinstance(env, dict) else None
        denials = len(env.get("permission_denials") or []) if isinstance(env, dict) else None
        v = json.loads(strip_fence(res)) if isinstance(res, str) else None
    except Exception:
        v = None
    ok = v.get("ok") if isinstance(v, dict) else None
    if not isinstance(ok, bool):
        log("fail-open-unparsed", repo=repo, wall_s=wall, denials=denials, rc=proc.returncode, stdout=text[:400], stderr=err.decode("utf-8", "replace")[:200])
        return 0
    reason = str(v.get("reason") or "")[:400]
    if ok:
        log("allow-red-witnessed", repo=repo, wall_s=wall, reason=reason, files=relevant[:20], denials=denials)
        return 0
    log("refused-no-red", repo=repo, wall_s=wall, reason=reason, files=relevant[:20], denials=denials)
    return refuse(
        f"commit refused by the red-witness gate: {reason} (the src/tests files: {shown[:600]}). Run the failing test so its output shows in this "
        "session (or bank a RED-*.log by name) BEFORE the src change, then commit again — the gate re-reads the transcript; it is a gate, not a wall "
        "(a model judged it). This refusal is the driver's to clear: never hand the commit to the owner and never ask the owner to run a command.")

def main(raw: bytes) -> int:
    try:
        return gate(raw)
    except Exception as e:  
        log("fail-open-error", error=repr(e)[:300])
        return 0

if __name__ == "__main__":
    sys.exit(main(_RAW))
