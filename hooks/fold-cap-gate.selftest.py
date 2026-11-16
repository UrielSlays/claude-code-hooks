#!/usr/bin/env python3
"""Offline regression tests. Run with python -B. Scratch files stay under the configured temporary directory."""

import json
import os
import subprocess
import sys
import tempfile
import time

HOOK = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "fold-cap-gate.py")

def sh(cwd, *args):
    r = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"setup failed: {' '.join(args)}: {r.stderr}")
    return r.stdout

def run_gate(payload):
    env = dict(os.environ)
    env["FOLD_CAP_STATE_DIR"] = os.path.join(tempfile.gettempdir(), "claude-fold-cap-selftest")
    env.pop("HOOK_FOLD_CAP_SKIP", None)
    env.pop("HOOK_COMPACTION_AUDITOR", None)
    r = subprocess.run([sys.executable, "-B", HOOK], input=payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8"), capture_output=True, env=env)
    return r.returncode, r.stderr.decode("utf-8", "replace")

def payload(repo, command):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": repo}

def main():
    root = tempfile.mkdtemp(prefix="fold-cap-selftest-")
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "config", "user.email", "selftest@invalid")
    sh(repo, "git", "config", "user.name", "selftest")
    sh(repo, "git", "config", "commit.gpgsign", "false")
    open(os.path.join(repo, "a.txt"), "w").write("a\n")
    sh(repo, "git", "add", "a.txt")
    sh(repo, "git", "commit", "-q", "-m", "chore: base")
    sh(repo, "git", "update-ref", "refs/remotes/origin/main", "HEAD")
    sh(repo, "git", "checkout", "-q", "-b", "feat/lane")
    open(os.path.join(repo, "t.txt"), "w").write("t\n")
    sh(repo, "git", "add", "t.txt")
    sh(repo, "git", "commit", "-q", "-m", "test(lane): the red")
    old_walk = os.path.join(root, "WALK-old.md")
    open(old_walk, "w").write("old walk\n")
    time.sleep(1.1)
    open(os.path.join(repo, "s.txt"), "w").write("s\n")
    sh(repo, "git", "add", "s.txt")
    sh(repo, "git", "commit", "-q", "-m", "fix(lane): the first fold")
    time.sleep(1.1)
    fresh_walk = os.path.join(root, "WALK-every-sentence.md")
    open(fresh_walk, "w").write("fresh walk\n")
    open(os.path.join(repo, "s.txt"), "a").write("more\n")
    sh(repo, "git", "add", "s.txt")

    cases = []
    rc, err = run_gate(payload(repo, f'git -C "{repo}" commit -m "fix(lane): the second fold"'))
    cases.append(("R1 second fix, no Walk line", rc, 2, err))
    rc, err = run_gate(payload(repo, f'git -C "{repo}" commit -m "fix(lane): the second fold\n\nWalk: {old_walk}"'))
    cases.append(("R2 second fix, stale walk cited", rc, 2, err))
    rc, err = run_gate(payload(repo, f'git -C "{repo}" commit -m "fix(lane): the second fold\n\nWalk: {fresh_walk}"'))
    cases.append(("G1 second fix, fresh walk cited", rc, 0, err))
    rc, err = run_gate(payload(repo, f'git -C "{repo}" commit -m "docs(lane): the pair"'))
    cases.append(("G3 docs subject", rc, 0, err))
    msgfile = os.path.join(root, "msg.txt")
    open(msgfile, "w", encoding="utf-8").write(f"fix(lane): the second fold\n\nbody\n\nWalk: {fresh_walk}\n")
    rc, err = run_gate(payload(repo, f'cd "{repo}" && git commit -q -F "{msgfile}"'))
    cases.append(("G4a -F file with a fresh walk", rc, 0, err))
    open(msgfile, "w", encoding="utf-8").write("fix(lane): the second fold\n\nbody with no walk\n")
    rc, err = run_gate(payload(repo, f'cd "{repo}" && git commit -q -F "{msgfile}"'))
    cases.append(("G4b -F file without a walk", rc, 2, err))
    rc, err = run_gate(b"not json at all commit")
    cases.append(("G5 garbage stdin", rc, 0, err))
    
    sh(repo, "git", "stash", "-q")
    sh(repo, "git", "checkout", "-q", "-b", "feat/lane2", "origin/main")
    open(os.path.join(repo, "u.txt"), "w").write("u\n")
    sh(repo, "git", "add", "u.txt")
    rc, err = run_gate(payload(repo, f'git -C "{repo}" commit -m "fix(lane2): the first fold"'))
    cases.append(("G2 first fix on a fresh branch", rc, 0, err))

    bad = 0
    for name, got, want, err in cases:
        ok = got == want
        bad += 0 if ok else 1
        print(f"{'ok ' if ok else 'BAD'} {name}: exit {got} (want {want}){(' — ' + err.strip()[:160]) if err.strip() and want == 2 else ''}")
    print(f"fold-cap-gate selftest: {len(cases) - bad}/{len(cases)} agree; repo {root}")
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
