#!/usr/bin/env python3
"""Require two low-progress CPU samples before a literal PID kill.

Name and filter kills are refused. Command text is inspected, never executed.
Unknown syntax and corrupt witness files fail open."""

import os
import tempfile
import sys

def fast_path():
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        return None
    low = raw.lower()
    return raw if (b"kill" in low or b"stop-process" in low or b"spps" in low) else None

if __name__ == "__main__":
    _RAW = fast_path()
    if _RAW is None:
        sys.exit(0)

import json  
import re  
import time  
from datetime import datetime  

WITNESS_DIR = os.environ.get("KILL_WITNESS_DIR") or os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir(), "claude-kill-witness")
MIN_GAP_S = 60.0
MAX_CPU_DELTA_S = 0.5
VERB = re.compile(r"(?i)(?<![\w.$-])(taskkill(?:\.exe)?|stop-process|spps|kill|pkill|killall)(?![\w-])")
SEP = re.compile(r"\|\||&&|;|\r?\n|\|")
SAMPLE_HELPER = os.environ.get("KILL_SAMPLE_HELPER") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "stall-sample.ps1")
HELPER = 'pwsh -NoProfile -File "' + SAMPLE_HELPER.replace("{", "{{").replace("}", "}}") + '" -Pid {pid}'

def log(event: str, **kw) -> None:
    try:
        os.makedirs(WITNESS_DIR, exist_ok=True)
        with open(os.path.join(WITNESS_DIR, "gate-log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": int(time.time()), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **kw}, ensure_ascii=False) + "\n")
    except Exception:
        pass

def toks(s: str) -> list:
    return [t.strip("\"'()") for t in re.findall(r"\"[^\"]*\"|'[^']*'|[^\s]+", s)]

def segments(cmd: str) -> list:
    out, pos, piped = [], 0, False
    for m in SEP.finditer(cmd):
        out.append((cmd[pos:m.start()], piped))
        piped = m.group(0) == "|"
        pos = m.end()
    out.append((cmd[pos:], piped))
    return out

def ids_from(vals: list, pids: list, nonlit: list) -> None:
    for v in vals:
        for x in v.split(","):
            x = x.strip()
            if not x:
                continue
            if re.fullmatch(r"\d+", x):
                pids.append(int(x))
            else:
                nonlit.append(x)

def judge_ps(args: list, prev: str, piped: bool, by_name: list, pids: list, nonlit: list, verb: str) -> None:
    i, found = 0, False
    while i < len(args):
        a = args[i]
        low = a.lower()
        m = re.fullmatch(r"-id(?::(.*))?", low)
        if m:
            found = True
            vals = [m.group(1)] if m.group(1) else []
            j = i + 1
            while not vals or (vals[-1].endswith(",") and j < len(args)):
                if j >= len(args):
                    break
                vals.append(args[j])
                j += 1
            ids_from(["".join(vals)], pids, nonlit)
            i = j
            continue
        if re.fullmatch(r"-(n|na|nam|name|processname|inputobject|i|in|inp|inpu|input|inputo|inputob|inputobj|inputobje|inputobjec)(:.*)?", low):
            by_name.append(f"{verb} {a}")
            found = True
            i += 2
            continue
        if verb == "kill" and (re.fullmatch(r"-\d+|-[a-z]+\d*", low) and low not in ("-id",)):
            
            if low in ("-s", "-n"):
                i += 2
            else:
                if low == "-l":
                    return
                i += 1
            continue
        if low.startswith("-"):
            i += 1
            continue
        if low.startswith("%"):
            i += 1
            found = True
            continue
        if re.fullmatch(r"[\d,]+", a):
            ids_from([a], pids, nonlit)
            found = True
        elif a.startswith("$"):
            nonlit.append(a)
            found = True
        else:

            break
        i += 1
    if not found and piped and verb.lower() in ("stop-process", "spps", "kill"):
        m = re.search(r"(?i)(?:get-process|gps|ps)\s+.*?-id[:\s]\s*([\d,\s]+)", prev)
        if m:
            ids_from([m.group(1).replace(" ", "")], pids, nonlit)
        else:
            by_name.append(f"{prev.strip()[:80]} | {verb}")

QUOTED = re.compile(r'@"(.*?)"@|@\'(.*?)\'@|"((?:[^"\\]|\\.)*)"|\'([^\']*)\'', re.S)
LEAD = re.compile(r"(?i)^(?:sudo|nohup|time|exec|start|call|&)\s+|^\w+=\S*\s+")
VERB_TOKEN = re.compile(r"(?i)^(?:taskkill(?:\.exe)?|stop-process|spps|kill|pkill|killall)$")

def commands(text: str, depth: int = 0) -> list:
    bodies = []

    def mask(m):
        body = next(g for g in m.groups() if g is not None)
        if re.search(r"\s", body):
            bodies.append(body)
            return " "
        return body

    out_ = list(segments(QUOTED.sub(mask, text)))
    if depth < 3:
        for b in bodies:
            out_.extend(commands(b, depth + 1))
    return out_

def first_verb(seg: str):
    s = seg.strip()
    while True:
        m = LEAD.match(s)
        if not m:
            break
        s = s[m.end():]
    m = re.match(r"([^\s]+)", s)
    if not m:
        return None
    tok = m.group(1).strip("\"'()")
    if not VERB_TOKEN.match(tok):
        return None
    return tok.lower(), s[m.end():]

def targets(tool: str, ti: dict):
    by_name, pids, nonlit = [], [], []
    if re.search(r"__process_kill$", tool or ""):
        v = ti.get("pid")
        ids_from([str(v)] if v is not None else [], pids, nonlit)
        return by_name, pids, nonlit
    cmd = ti.get("command")
    if not isinstance(cmd, str):
        return by_name, pids, nonlit
    segs = commands(cmd)
    for k, (seg, piped) in enumerate(segs):
        prev = segs[k - 1][0] if k else ""
        fv = first_verb(seg)
        if fv is None:
            continue
        verb, rest = fv
        args = toks(rest)
        if verb.startswith("taskkill"):
            j = 0
            while j < len(args):
                a = args[j]
                al = a.lower()
                if al in ("/pid", "-pid") and j + 1 < len(args):
                    ids_from([args[j + 1]], pids, nonlit)
                    j += 2
                elif al in ("/im", "-im", "/fi", "-fi"):
                    by_name.append(f"taskkill {a} {args[j + 1] if j + 1 < len(args) else ''}".strip())
                    j += 2
                elif al.startswith("/") or al.startswith("-"):
                    j += 1  
                    if al in ("/s", "/u", "/p", "-s", "-u", "-p") and j < len(args):
                        j += 1
                else:
                    break  
        elif verb in ("pkill", "killall"):
            by_name.append(f"{verb} {' '.join(args)[:60]}".strip())
        else:
            judge_ps(args, prev, piped, by_name, pids, nonlit, verb)
    return by_name, pids, nonlit

def parse_at(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

def witness(pid: int):
    path = os.path.join(WITNESS_DIR, f"{pid}.jsonl")
    if not os.path.isfile(path):
        return False, f"no witness file ({path})", path
    samples = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            if not isinstance(d, dict):
                raise ValueError("a witness line is not an object")
            at, cpu = parse_at(d.get("at")), d.get("cpu_total_s")
            if at is None or isinstance(cpu, bool) or not isinstance(cpu, (int, float)):
                continue
            samples.append((at, float(cpu)))
    if len(samples) < 2:
        return False, f"{len(samples)} usable sample(s) — need two", path
    best_gap, progress = 0.0, None
    for i in range(len(samples)):
        for j in range(i + 1, len(samples)):
            gap = abs(samples[j][0] - samples[i][0])
            dcpu = abs(samples[j][1] - samples[i][1])
            if gap >= MIN_GAP_S and dcpu < MAX_CPU_DELTA_S:
                return True, f"stall witnessed: two samples {gap:.0f} s apart, CPU moved {dcpu:.2f} s", path
            best_gap = max(best_gap, gap)
            if gap >= MIN_GAP_S and (progress is None or dcpu < progress[1]):
                progress = (gap, dcpu)
    if progress:
        return False, f"the tree's CPU moved {progress[1]:.2f} s over {progress[0]:.0f} s — progress, not a stall (need < {MAX_CPU_DELTA_S} s)", path
    return False, f"the samples are at most {best_gap:.0f} s apart (need >= {MIN_GAP_S:.0f} s)", path

def refuse(msg: str) -> int:
    sys.stderr.buffer.write((msg + "\n").encode("utf-8"))
    sys.stderr.flush()
    return 2

def gate(raw: bytes) -> int:
    try:
        p = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return 0
    if not isinstance(p, dict) or not isinstance(p.get("tool_input"), dict):
        return 0
    tool = str(p.get("tool_name") or "")
    by_name, pids, nonlit = targets(tool, p["tool_input"])
    if by_name:
        log("refused-by-name", tool=tool, what=by_name[:5])
        return refuse(
            f"kill refused: {'; '.join(by_name[:3])} — a kill BY NAME or FILTER. Target policy: name each target by its full path or PID. "
            "Find the exact PID (Get-Process / tasklist / process_list), witness its stall twice with "
            f"`{HELPER.format(pid='N')}` (again >= 60 s later), then kill by `/PID N` or `-Id N`.")
    if nonlit:
        log("allow-nonliteral-pid", tool=tool, what=nonlit[:5])
    if not pids:
        return 0
    bad = []
    for pid in sorted(set(pids)):
        try:
            ok, why, path = witness(pid)
        except Exception as e:  
            log("allow-corrupt-witness", pid=pid, error=str(e)[:200])
            continue
        log("allow-witnessed" if ok else "refused-unwitnessed", pid=pid, why=why)
        if not ok:
            bad.append((pid, why, path))
    if not bad:
        return 0
    lines = [f"kill of PID {pid} refused — no stall witness: {why}." for pid, why, _ in bad]
    pid0 = bad[0][0]
    return refuse(
        " ".join(lines) + " A kill needs a stall witnessed TWICE (by exact PID): run "
        f"`{HELPER.format(pid=pid0)}` now and again >= 60 s later (a bounded wait); the kill passes when two samples >= 60 s apart show the "
        f"process tree's CPU moving < {MAX_CPU_DELTA_S} s. Witness file(s): " + ", ".join(path for _, _, path in bad) + ". PIDs this session launched are not exempt.")

def main(raw: bytes) -> int:
    try:
        return gate(raw)
    except Exception as e:  
        log("fail-open-error", error=repr(e)[:300])
        return 0

if __name__ == "__main__":
    sys.exit(main(_RAW))
