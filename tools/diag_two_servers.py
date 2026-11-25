"""Diagnostic only (branch capalith-mac-diag, never main): two servers' first start at the same moment, many times, and the real cause.

usage: python tools/diag_two_servers.py REPS PARALLEL [JITTER_MS]

tests/test_onboarding.py::test_simultaneous_connections_share_registration_and_priority starts two `capalith-mcp` servers on an empty
data folder and, on macOS CI, one of them sometimes exits 1 with the catalog_setup_failed text, which hides the cause (the server
turns every setup error into that one sentence). This script repeats that test's start REPS times, PARALLEL pairs at a time, and
for every failing start prints what the server hid: the exception type, errno, the message, the chain of causes and the last six
frames (file, line, function). It runs the real `mcp_server.main` with the same arguments and environment as the test and only
wraps `prepare_catalog` to print the exception before main swallows it; no product code is changed. With JITTER_MS above 0, a
child sleeps up to that long, with probability 0.3, before each mkdir, open, flock and sqlite3.connect, so the two starts walk
the steps in more orders. Paths shown are the script's own temporary folders. No network, no paid call.
"""
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

CHILD = r'''
import fcntl, json, os, random, sqlite3, sys, time, traceback
jitter_ms, seed, start_at = float(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
first, second = sys.argv[4], sys.argv[5]
random.seed(seed)
if jitter_ms > 0:
    def jitter(fn):
        def wrapped(*a, **k):
            if random.random() < 0.3:
                time.sleep(random.random() * jitter_ms / 1000)
            return fn(*a, **k)
        return wrapped
    os.mkdir, os.open, fcntl.flock, sqlite3.connect = jitter(os.mkdir), jitter(os.open), jitter(fcntl.flock), jitter(sqlite3.connect)
from capalith import mcp_server
real = mcp_server.prepare_catalog
def wrapped(*args, **kwargs):
    try:
        return real(*args, **kwargs)
    except BaseException as error:
        chain, cause = [], error
        while cause is not None and len(chain) < 6:
            chain.append(f"{type(cause).__name__} errno={getattr(cause, 'errno', None)}: {str(cause)[:160]}")
            cause = cause.__cause__ or cause.__context__
        frames = traceback.extract_tb(error.__traceback__)[-6:]
        print("DIAG " + json.dumps({"chain": chain, "where": [f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}:{f.name}" for f in frames]}),
              file=sys.stderr, flush=True)
        raise
mcp_server.prepare_catalog = wrapped
while time.time() < start_at:
    pass
sys.exit(mcp_server.main(["--source", first, "--source", second, "--source", first]))
'''


def make_skill(root: Path, name: str, text: str) -> None:
    folder = root / name
    (folder / "references").mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Test skill.\n---\n\n# Test\n", encoding="utf-8")
    (folder / "references" / "data.bin").write_bytes(text.encode())


def one_pair(index: int, jitter_ms: float, work: Path, results: list, lock: threading.Lock) -> None:
    root = Path(tempfile.mkdtemp(dir=work)).resolve()
    first, second = root / "first", root / "second"
    make_skill(first, "shared-name", "first")
    make_skill(second, "shared-name", "second")
    env = {**os.environ, "XDG_DATA_HOME": str(root / "data")}
    start_at = time.time() + 3.0
    children = [subprocess.Popen([sys.executable, "-c", CHILD, str(jitter_ms), str(index * 2 + k), str(start_at), str(first), str(second)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
                for k in range(2)]
    for number, child in enumerate(children):
        try:
            out, err = child.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            child.kill()
            out, err = child.communicate()
            err += "\nTIMEOUT after 120 s"
        with lock:
            results.append({"pair": index, "child": number, "code": child.returncode, "stdout": out[:200], "stderr": err[-1500:]})


def run(reps: int, parallel: int, jitter_ms: float) -> None:
    work = Path(tempfile.mkdtemp(prefix="capalith-diag-")).resolve()
    results: list = []
    lock = threading.Lock()
    queue = list(range(reps))

    def worker() -> None:
        while True:
            with lock:
                if not queue:
                    return
                index = queue.pop(0)
            one_pair(index, jitter_ms, work, results, lock)

    started = time.time()
    threads = [threading.Thread(target=worker) for _ in range(parallel)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    failed = [r for r in results if r["code"] != 0 or r["stdout"]]
    print(f"== reps={reps} parallel={parallel} jitter_ms={jitter_ms}: starts={len(results)} ok={len(results) - len(failed)} "
          f"failed={len(failed)} seconds={time.time() - started:.0f}", flush=True)
    kinds = Counter()
    for row in failed:
        diag = [line for line in row["stderr"].splitlines() if line.startswith("DIAG ")]
        kinds[diag[0][5:] if diag else "no DIAG line: " + row["stderr"][-120:].replace("\n", " | ")] += 1
    for kind, count in kinds.most_common(8):
        print(f"  {count} x {kind}", flush=True)
    for row in failed[:3]:
        print("  sample", json.dumps({k: row[k] for k in ("pair", "child", "code")}), row["stderr"][-600:].replace("\n", " | "), flush=True)


if __name__ == "__main__":
    reps, parallel = int(sys.argv[1]), int(sys.argv[2])
    jitter_ms = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    print(f"platform={platform.platform()} python={sys.version.split()[0]} sqlite={sqlite3.sqlite_version} cpus={os.cpu_count()} "
          f"load={os.getloadavg() if hasattr(os, 'getloadavg') else None}", flush=True)
    run(reps, parallel, 0.0)
    if jitter_ms:
        run(reps, parallel, jitter_ms)
