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
PID = 4242

def check(name, cond, detail=""):
    print(("  ok   " if cond else "  RED  ") + name + ("" if cond else "  -- " + str(detail)[:400]))
    (passes if cond else failures).append(name)

def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"

class Gate:
    def __init__(self, script, wdir):
        self.script, self.wdir = script, wdir

    def run(self, command=None, tool="Bash", pid=None, raw=None):
        env = dict(os.environ, KILL_WITNESS_DIR=self.wdir)
        ti = {"pid": pid} if pid is not None else {"command": command}
        payload = {"session_id": "selftest-kwg", "cwd": r"C:\Users\example\source\sample-app", "hook_event_name": "PreToolUse",
                   "tool_name": tool, "tool_input": ti, "tool_use_id": "toolu_k"}
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        t0 = time.time()
        try:
            p = subprocess.run([sys.executable, "-B", self.script], input=data, capture_output=True, env=env, timeout=60)
        except subprocess.TimeoutExpired:
            return 99, "", "TIMEOUT", time.time() - t0
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"), time.time() - t0

    def witness(self, pid, samples, raw=None):
        os.makedirs(self.wdir, exist_ok=True)
        path = os.path.join(self.wdir, f"{pid}.jsonl")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            if raw is not None:
                f.write(raw)
            else:
                for at, cpu in samples:
                    f.write(json.dumps({"at": at, "pid": pid, "tree_size": 3, "cpu_total_s": cpu, "shell_children": 1}) + "\n")
        return path

    def clear(self, pid):
        path = os.path.join(self.wdir, f"{pid}.jsonl")
        if os.path.isfile(path):
            os.remove(path)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default=os.path.join(HERE, "kill-witness-gate.py"))
    a = ap.parse_args()
    base_tmp = os.environ.get("HOOK_SELFTEST_TMP") or tempfile.gettempdir()
    os.makedirs(base_tmp, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="kill-witness-gate-selftest-", dir=base_tmp)
    print(f"script: {a.script}\ntemp:   {tmp}  (left in place — this test never deletes a tree)")
    G = Gate(a.script, os.path.join(tmp, "witness"))
    now = time.time()

    times = []
    for _ in range(9):
        rc, out, err, dt = G.run("git status --short")
        times.append(dt)
    med = statistics.median(times)
    print(f"       (fast path, wall per hook process incl. python start-up: median {med*1000:.0f} ms over 9 runs)")
    check("K1. not a kill (`git status`): exit 0", rc == 0, (rc, err[:200]))
    check("K1a. the fast path's wall time is under 200 ms (median)", med < 0.2, f"{med*1000:.0f} ms")
    for cmd in ("Get-Process node | Select-Object Id, CPU", "echo skill && grep -n killer notes.md", "Get-Help Stop-Process", "git commit -m 'kill-witness gate: the helper'"):
        rc, out, err, dt = G.run(cmd)
        check(f"K1b. not a kill: `{cmd[:50]}` -> exit 0", rc == 0, (rc, err[:200]))

    rc, out, err, dt = G.run(f"echo skill {PID} && python backkill.py")
    check(f"K1c. `kill` inside a longer word (`skill {PID}`, `backkill.py`) is not a kill: exit 0", rc == 0, (rc, err[:200]))

    for cmd in (f'git commit -q -m "the process table documents the kill witness gate, row 21 is ready, row 31 is partial" -- a.md',
                f"Set-Location C:/x; git commit -m 'stop-process gate: row {PID} reads PARTIAL' -- b.md",
                f'echo "we never kill on one sample; see row {PID}"',
                f"Write-Output 'Stop-Process is refused by name (target policy)'; git log -1"):
        rc, out, err, dt = G.run(cmd, tool="PowerShell")
        check(f"K1d. the verb inside an argument is not a kill: `{cmd[:60]}` -> exit 0", rc == 0, (rc, err[:200]))
    
    for tool, cmd in (("Bash", f'pwsh -NoProfile -Command "Stop-Process -Id {PID} -Force"'), ("PowerShell", f"cmd /c 'taskkill /F /PID {PID}'")):
        rc, out, err, dt = G.run(cmd, tool=tool)
        check(f"K1e. a quoted sub-command is still a kill: `{cmd[:50]}` -> refused", rc == 2 and f"-Pid {PID}" in err, (rc, err[:200]))

    for cmd in ("taskkill /IM node.exe /F", 'taskkill /F /FI "IMAGENAME eq codex.exe"', "Stop-Process -Name node -Force", "Stop-Process -ProcessName vitest",
                "pkill -f vitest", "killall node", "Get-Process codex | Stop-Process -Force", "kill -Name pwsh"):
        rc, out, err, dt = G.run(cmd, tool="PowerShell")
        check(f"K2. by name refused: `{cmd}`", rc == 2 and "PID" in err and "Target policy" in err, (rc, err[:200]))

    forms = [("Bash", f"taskkill /F /PID {PID}"), ("PowerShell", f"Stop-Process -Id {PID} -Force"), ("Bash", f"kill -9 {PID}"), ("PowerShell", f"kill {PID}"),
             ("PowerShell", f"spps -Id {PID}"), ("PowerShell", f"Stop-Process {PID}"), ("PowerShell", f"Get-Process -Id {PID} | Stop-Process -Force"),
             ("mcp__00000000-0000-4000-8000-000000000001__shell", f"taskkill /T /F /PID {PID}"), ("Bash", f"kill -s KILL {PID}"), ("PowerShell", f"Stop-Process -Id:{PID}")]
    for tool, cmd in forms:
        rc, out, err, dt = G.run(cmd, tool=tool)
        check(f"K3. by PID, no witness, refused: [{tool[:12]}] `{cmd}`", rc == 2 and f"stall-sample.ps1\" -Pid {PID}" in err, (rc, err[:240]))
    rc, out, err, dt = G.run(tool="mcp__project__process_kill", pid=PID)
    check("K3b. process_kill {pid} with no witness: refused", rc == 2 and f"-Pid {PID}" in err, (rc, err[:200]))

    cmd = f"taskkill /F /PID {PID}"
    
    G.witness(PID, [(iso(now - 10), 5.0)])
    rc, out, err, dt = G.run(cmd)
    check("K4. one sample: refused", rc == 2 and "1 usable sample" in err, (rc, err[:200]))
    
    G.witness(PID, [(iso(now - 40), 5.0), (iso(now - 10), 5.0)])
    rc, out, err, dt = G.run(cmd)
    check("K5. two samples 30 s apart: refused (need >= 60 s)", rc == 2 and "60" in err, (rc, err[:200]))
    
    G.witness(PID, [(iso(now - 71), 5.0), (iso(now - 10), 7.0)])
    rc, out, err, dt = G.run(cmd)
    check("K6. two samples 61 s apart, CPU +2.0 s: refused (progress, not a stall)", rc == 2 and "progress" in err, (rc, err[:200]))
    
    G.witness(PID, [(iso(now - 71), 5.0), (iso(now - 10), 5.1)])
    rc, out, err, dt = G.run(cmd)
    check("K7. two samples 61 s apart, CPU +0.1 s: allowed", rc == 0, (rc, err[:200]))
    rc, out, err, dt = G.run(tool="mcp__claude_ai_project__process_kill", pid=str(PID))
    check("K7b. process_kill with the pid as a string, witnessed: allowed", rc == 0, (rc, err[:200]))
    G.witness(PID, [(iso(now - 130), 5.0), (iso(now - 70), 9.0), (iso(now - 10), 9.2)])
    rc, out, err, dt = G.run(cmd)
    check("K7c. three samples, the last two a stall >= 60 s apart: allowed (any qualifying pair)", rc == 0, (rc, err[:200]))
    
    with open(os.path.join(G.wdir, f"{PID}.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"at": iso(now - 71), "pid": PID, "cpu_total_s": None}) + "\n" + json.dumps({"at": iso(now - 10), "pid": PID, "cpu_total_s": 5.0}) + "\n")
    rc, out, err, dt = G.run(cmd)
    check("K16. a sample with cpu_total_s null does not count: refused (1 usable sample)", rc == 2 and "1 usable sample" in err, (rc, err[:200]))
    
    G.witness(PID, None, raw="{not json\n")
    rc, out, err, dt = G.run(cmd)
    check("K8. a corrupt witness file: allowed (fail open)", rc == 0, (rc, err[:200]))
    
    with open(os.path.join(G.wdir, f"{PID}.jsonl"), "w", encoding="utf-8-sig", newline="\n") as f:
        f.write(json.dumps({"at": iso(now - 71), "cpu_total_s": 5.0}) + "\n" + json.dumps({"at": iso(now - 10), "cpu_total_s": 7.5}) + "\n")
    rc, out, err, dt = G.run(cmd)
    check("K17. a BOM-prefixed witness is read (not mistaken for corrupt): progress still refused", rc == 2 and "progress" in err, (rc, err[:200]))
    G.clear(PID)

    G.witness(PID, [(iso(now - 71), 5.0), (iso(now - 10), 5.1)])
    rc, out, err, dt = G.run(f"taskkill /F /PID {PID} /PID 4343")
    check("K9. two PIDs, only one witnessed: refused naming the other", rc == 2 and "4343" in err and f"PID {PID} " not in err.split("refused")[0], (rc, err[:300]))
    rc, out, err, dt = G.run(f"Stop-Process -Id {PID},4343")
    check("K9b. `-Id a,b`, only one witnessed: refused naming the other", rc == 2 and "4343" in err, (rc, err[:300]))
    G.clear(PID)

    rc, out, err, dt = G.run("$p = Start-Process node -PassThru; Stop-Process -Id $p.Id")
    check("K11. a non-literal PID (`-Id $p.Id`): allowed (fail open, logged) — a known limit", rc == 0, (rc, err[:200]))
    rc, out, err, dt = G.run("kill %1")
    check("K12. `kill %1` (a shell job, not a PID): allowed", rc == 0, (rc, err[:200]))

    for label, raw in (("not JSON with kill in it", b"taskkill /PID 1 {{{"), ("a JSON list", b'["kill -9 1"]'), ("tool_input not an object", b'{"tool_name":"Bash","tool_input":"kill -9 1"}'), ("empty", b"")):
        rc, out, err, dt = G.run(raw=raw)
        check(f"K10. garbage stdin ({label}): exit 0", rc == 0, (rc, err[:200]))

    print(f"\n{len(passes)} passed, {len(failures)} failed")
    if failures:
        print("RED: " + ", ".join(n.split(".")[0] for n in failures))
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
