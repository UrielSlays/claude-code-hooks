#!/usr/bin/env python3
"""Offline regression tests. Run with python -B. Scratch files stay under the configured temporary directory."""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
failures, passes = [], []

STUB = r'''
import json, os, sys, time
rec = os.environ["STUB_RECORD"]
data = sys.stdin.buffer.read().decode("utf-8", "replace")
with open(rec, "w", encoding="utf-8") as f:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": data,
               "env": {k: os.environ.get(k) for k in ("HOOK_COMPACTION_AUDITOR", "CLAUDE_CODE_SAFE_MODE", "CLAUDECODE")}}, f)
mode = os.environ.get("STUB_MODE", "ok_true")
if mode == "sleep":
    time.sleep(40)
if mode == "garbage":
    sys.stdout.write("this is not an envelope")
    sys.exit(0)
res = {"ok_true": '{"ok": true, "reason": "red witnessed at line 7"}',
       "ok_false": '{"ok": false, "reason": "no red for tests/a.test.ts: no failing run names it before src/a.ts was written"}',
       "fenced_false": '```json\n{"ok": false, "reason": "fenced: no red for tests/a.test.ts"}\n```',
       "ok_string": '{"ok": "false", "reason": "a string, not a boolean"}',
       "sleep": '{"ok": false, "reason": "slept"}'}[mode]
sys.stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": res}))
'''

def check(name, cond, detail=""):
    print(("  ok   " if cond else "  RED  ") + name + ("" if cond else "  -- " + str(detail)[:400]))
    (passes if cond else failures).append(name)

def git(repo, *args):
    return subprocess.run(["git", "-C", repo, "-c", "user.name=selftest", "-c", "user.email=selftest@example.invalid", "-c", "commit.gpgsign=false"] + list(args),
                          capture_output=True, timeout=30)

def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

class Hook:
    def __init__(self, script, tmp, bindir, transcript):
        self.script, self.tmp, self.bindir, self.transcript = script, tmp, bindir, transcript
        self.rec = os.path.join(tmp, "stub-call.json")
        self.state = os.path.join(tmp, "state")

    def run(self, command, cwd, mode="ok_true", tool="Bash", extra=None, raw=None, transcript=None):
        if os.path.exists(self.rec):
            os.remove(self.rec)
        env = dict(os.environ)
        for k in ("HOOK_COMPACTION_AUDITOR", "HOOK_RED_WITNESS_SKIP", "CLAUDE_CODE_SAFE_MODE", "HOOK_CLAUDE_BIN", "HOOK_RED_WITNESS_TIMEOUT_S"):
            env.pop(k, None)
        env.update({"PATH": self.bindir + os.pathsep + env.get("PATH", ""), "STUB_RECORD": self.rec, "STUB_MODE": mode,
                    "COMMIT_RED_WITNESS_STATE_DIR": self.state, "HOOK_CLAUDE_BIN": os.path.join(self.bindir, "claude_stub.py")})
        env.update(extra or {})
        payload = {"session_id": "selftest-crw", "transcript_path": transcript if transcript is not None else self.transcript, "cwd": cwd,
                   "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}, "tool_use_id": "toolu_x"}
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        t0 = time.time()
        try:
            p = subprocess.run([sys.executable, "-B", self.script], input=data, capture_output=True, env=env, timeout=120)
        except subprocess.TimeoutExpired:
            return 99, "", "TIMEOUT", time.time() - t0, None
        call = None
        if os.path.exists(self.rec):
            try:
                with open(self.rec, encoding="utf-8") as f:
                    call = json.load(f)
            except Exception:
                call = {"unreadable": True}
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"), time.time() - t0, call

    def log(self):
        try:
            with open(os.path.join(self.state, "log.jsonl"), encoding="utf-8") as f:
                return [json.loads(x) for x in f if x.strip()]
        except Exception:
            return []

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default=os.path.join(HERE, "commit-red-witness.py"))
    a = ap.parse_args()
    base_tmp = os.environ.get("HOOK_SELFTEST_TMP") or tempfile.gettempdir()
    os.makedirs(base_tmp, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="commit-red-witness-selftest-", dir=base_tmp)
    print(f"script: {a.script}\ntemp:   {tmp}  (left in place — this test never deletes a tree)")
    bindir = os.path.join(tmp, "bin")
    write(os.path.join(bindir, "claude_stub.py"), STUB)
    write(os.path.join(bindir, "claude.cmd"), f'@"{sys.executable}" -B "%~dp0claude_stub.py" %*\r\n')
    transcript = os.path.join(tmp, "session.jsonl")
    write(transcript, '{"type":"system","subtype":"compact_boundary"}\n{"type":"user","message":{"content":"go"}}\n')
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    git(repo, "init", "-q")
    write(os.path.join(repo, "README.md"), "x\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "init")
    elsewhere = os.path.join(tmp, "elsewhere")
    os.makedirs(elsewhere)
    H = Hook(a.script, tmp, bindir, transcript)

    times = []
    for _ in range(9):
        rc, out, err, dt, call = H.run("git status --short", repo)
        times.append(dt)
    med = statistics.median(times)
    print(f"       (fast path, wall per hook process incl. python start-up: median {med*1000:.0f} ms over 9 runs)")
    check("C1. not a commit (`git status`): exit 0, the model is never called", rc == 0 and call is None, (rc, err[:200]))
    check("C1a. the fast path's wall time (process incl. interpreter start) is under 200 ms (median)", med < 0.2, f"{med*1000:.0f} ms")
    rc, out, err, dt, call = H.run("echo 'commit later' && git log -1", repo)
    check("C1b. 'commit' without `git … commit`: exit 0, no model call", rc == 0 and call is None, (rc, err[:200]))
    rc, out, err, dt, call = H.run("git commit-tree HEAD^{tree} -m x", repo)
    check("C1c. `git commit-tree` is not a commit: exit 0, no model call", rc == 0 and call is None, (rc, err[:200]))

    cases = [
        ("C2.  -m \"… GREEN …\"", 'git commit -m "CASE1 STEP1 GREEN at last"', "GREEN"),
        ("C2b. bash heredoc subject carries ZERO", "git commit -m \"$(cat <<'EOF'\nfold r3: gate ZERO\n\nthe body\nEOF\n)\"", "ZERO"),
        ("C2c. PowerShell here-string subject carries PASS", "git commit -m @'\nPASS: the cap holds\n\nbody\n'@", "PASS"),
        ("C2d. `git -C <repo> commit -am '… NO-GO …'` (GO, word-bounded at the hyphen)", f"git -C \"{repo}\" commit -am 'seat 2 NO-GO banked'", "GO"),
        ("C2e. --message=FIXED…", "git commit --message=FIXED-the-race", "FIXED"),
    ]
    for name, cmd, word in cases:
        rc, out, err, dt, call = H.run(cmd, repo)

        check(name + ": exit 0, the word NOTED on stdout, no model call", rc == 0 and word in out and "note, not a refusal" in out and call is None, (rc, out[:300], err[:200], call is not None))
    for name, cmd in (("C2f. the word only in the BODY (second -m) is not refused by the subject check", 'git commit -m "neutral subject" -m "GREEN in the body"'),
                      ("C2g. lowercase 'green' is not a verdict word (case-sensitive)", 'git commit -m "make the light green"'),
                      ("C2h. GOAL / PASSWORD are not standalone verdict words", 'git commit -m "GOAL: rotate the PASSWORD file"')):
        rc, out, err, dt, call = H.run(cmd, repo)
        check(name + ": exit 0 (nothing src/tests staged)", rc == 0, (rc, err[:300]))

    write(os.path.join(repo, "docs", "n.md"), "n\n")
    git(repo, "add", "docs/n.md")
    rc, out, err, dt, call = H.run('git commit -m "docs: a note"', repo, mode="ok_false")
    check("C9. only docs/ staged: exit 0, the model is never called", rc == 0 and call is None, (rc, err[:200], call is not None))
    git(repo, "commit", "-q", "-m", "docs")

    write(os.path.join(repo, "src", "a.ts"), "export const a = 1;\n")
    write(os.path.join(repo, "tests", "a.test.ts"), "test('a', () => {});\n")
    git(repo, "add", "src/a.ts", "tests/a.test.ts")

    rc, out, err, dt, call = H.run('git commit -m "CASE1: a stale nonce is rejected"', repo, mode="ok_false")
    check("C3. model says ok:false: exit 2 with the model's reason", rc == 2 and "no red for tests/a.test.ts" in err and call is not None, (rc, err[:300]))
    rc, out, err, dt, call = H.run('git commit -m "CASE1: a stale nonce is rejected"', repo, mode="ok_true")
    check("C4. model says ok:true: exit 0", rc == 0 and call is not None, (rc, err[:300]))
    rc, out, err, dt, call = H.run('git commit -m "CASE1: a stale nonce is rejected"', repo, mode="fenced_false")
    check("C6b. a ```json-fenced ok:false still refuses (fence stripped)", rc == 2 and "fenced: no red" in err, (rc, err[:300]))
    rc, out, err, dt, call = H.run('git commit -m "CASE1: a stale nonce is rejected"', repo, mode="ok_string")
    check("C6c. ok that is not a boolean: exit 0 (fail open), logged", rc == 0 and any(e.get("event", "").startswith("fail-open") for e in H.log()[-1:]), (rc, H.log()[-1:]))
    n_before = len(H.log())
    rc, out, err, dt, call = H.run('git commit -m "CASE1: a stale nonce is rejected"', repo, mode="sleep", extra={"HOOK_RED_WITNESS_TIMEOUT_S": "3"})
    check("C5. the model hangs: cut at the timeout, exit 0 (fail open), logged as a timeout", rc == 0 and dt < 20 and any("timeout" in e.get("event", "") for e in H.log()[n_before:]),
          (rc, round(dt, 1), H.log()[n_before:]))
    n_before = len(H.log())
    rc, out, err, dt, call = H.run('git commit -m "CASE1: a stale nonce is rejected"', repo, mode="garbage")
    check("C6. the model answers garbage: exit 0 (fail open), logged", rc == 0 and any(e.get("event", "").startswith("fail-open") for e in H.log()[n_before:]), (rc, H.log()[n_before:]))

    rc, out, err, dt, call = H.run('git commit -m "CASE1: a stale nonce is rejected"', repo, mode="ok_true")
    argv = (call or {}).get("argv", [])

    def flag(nm):
        return argv[argv.index(nm) + 1] if nm in argv and argv.index(nm) + 1 < len(argv) else None
    check("C11. argv: -p --model opus --max-turns 25 --output-format json --no-session-persistence --permission-mode dontAsk",
          "-p" in argv and flag("--model") == "opus" and flag("--max-turns") == "25" and flag("--output-format") == "json"
          and "--no-session-persistence" in argv and flag("--permission-mode") == "dontAsk", argv)
    check("C11b. argv: --allowedTools 'Read,Grep,Glob,Bash(git diff *),Bash(git status *),Bash(python *)' and the write/agent/web tools disallowed",
          flag("--allowedTools") == "Read,Grep,Glob,Bash(git diff *),Bash(git status *),Bash(python *)"
          and flag("--disallowedTools") == "Write,Edit,NotebookEdit,Agent,Workflow,WebFetch,WebSearch", argv)
    envc = (call or {}).get("env", {})
    check("C11c. the model's env: HOOK_COMPACTION_AUDITOR=1, CLAUDE_CODE_SAFE_MODE=1", envc.get("HOOK_COMPACTION_AUDITOR") == "1" and envc.get("CLAUDE_CODE_SAFE_MODE") == "1", envc)
    prompt = (call or {}).get("stdin", "")
    check("C11d. the prompt names the repo, the command, the transcript, `git diff --cached --name-only`, the compact_boundary window, the JSON reply",
          all(s in prompt for s in (repo, 'git commit -m "CASE1: a stale nonce is rejected"', transcript, "git diff --cached --name-only", "compact_boundary", "400 lines", '{"ok":true|false')), prompt[:600])
    check("C11f. the prompt carries the tool note (Bash only when its FIRST word is `python`, `git diff` or `git status`; chains / PowerShell denied)",
          "FIRST word" in prompt and "PowerShell" in prompt, prompt[-500:])
    check("C11e. the model runs in the repo", os.path.normcase(os.path.abspath((call or {}).get("cwd", "?"))) == os.path.normcase(os.path.abspath(repo)), (call or {}).get("cwd"))

    rc, out, err, dt, call = H.run(f'git -C "{repo}" commit -m "CASE1: from elsewhere"', elsewhere, mode="ok_false")
    check("C8. `git -C <repo> commit` from another cwd: detected, judged in the -C repo", rc == 2 and call is not None
          and os.path.normcase(os.path.abspath(call.get("cwd", "?"))) == os.path.normcase(os.path.abspath(repo)), (rc, err[:200], (call or {}).get("cwd")))
    rc, out, err, dt, call = H.run(f'cd "{repo}" && git commit -m "CASE1: after cd"', elsewhere, mode="ok_false")
    check("C8b. `cd <repo> && git commit` from another cwd: judged in the cd target", rc == 2 and call is not None
          and os.path.normcase(os.path.abspath(call.get("cwd", "?"))) == os.path.normcase(os.path.abspath(repo)), (rc, err[:200], (call or {}).get("cwd")))

    for tool in ("mcp__project__shell", "mcp__00000000-0000-4000-8000-000000000001__shell", "PowerShell"):
        rc, out, err, dt, call = H.run('git commit -m "CASE1: via another shell"', repo, mode="ok_false", tool=tool)
        check(f"C12. tool {tool[:40]}: judged the same (ok:false -> exit 2)", rc == 2 and call is not None, (rc, err[:200]))

    rc, out, err, dt, call = H.run('git commit -m "CASE1: no transcript"', repo, mode="ok_false", transcript=os.path.join(tmp, "missing.jsonl"))
    check("C14. the transcript file is missing: exit 0 (fail open), no model call", rc == 0 and call is None, (rc, err[:200]))

    long_cmd = 'git commit -m "CASE1: long" ' + "&& echo " + "y" * 2000
    rc, out, err, dt, call = H.run(long_cmd, repo, mode="ok_true")
    prompt = (call or {}).get("stdin", "")
    check("C15. the command quoted in the prompt is at most 600 chars", bool(prompt) and "y" * 560 in prompt and "y" * 700 not in prompt, len(prompt))

    for k in ("HOOK_RED_WITNESS_SKIP", "HOOK_COMPACTION_AUDITOR"):
        rc, out, err, dt, call = H.run('git commit -m "CASE1 GREEN"', repo, mode="ok_false", extra={k: "1"})
        check(f"C10. {k}=1: exit 0, nothing checked, no model call", rc == 0 and call is None, (rc, err[:200]))

    git(repo, "commit", "-q", "-m", "stage-clear")
    write(os.path.join(repo, "src", "b.ts"), "export const b = 2;\n")
    rc, out, err, dt, call = H.run('git add -A && git commit -m "CASE1: b"', repo, mode="ok_false")
    check("C13. `git add -A && git commit` with src/b.ts unstaged: the model is still asked (exit 2 on ok:false)", rc == 2 and call is not None and "src/b.ts" in (call or {}).get("stdin", ""), (rc, err[:200]))

    for label, raw in (("not JSON with commit in it", b"git commit {{{"), ("a JSON list", b'["git commit"]'), ("tool_input not an object", b'{"tool_input": "git commit -m x"}'), ("empty", b"")):
        rc, out, err, dt, call = H.run("", repo, raw=raw)
        check(f"C7. garbage stdin ({label}): exit 0", rc == 0 and call is None, (rc, err[:200]))

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    if failures:
        print("RED: " + ", ".join(n.split(".")[0] for n in failures))
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
