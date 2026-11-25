"""Offline evaluation through the public MCP and CLI surfaces.

Cases and output may contain private data: keep them outside this repository.
Run with the repository's dependency environment; --repo selects an exact archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import subprocess
import sys
import time


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def summarize(rows: list[dict]) -> dict:
    positives = [r for r in rows if r["acceptable_skills"] and r.get("group") != "close"]
    negatives = [r for r in rows if not r["acceptable_skills"] and r.get("group") != "close"]
    close = [r for r in rows if r.get("group") == "close"]
    return {
        "answerable": len(positives),
        "top1_hits": sum(r["top1"] for r in positives),
        "top4_hits": sum(r["top4"] for r in positives),
        "no_answer_cases": len(negatives),
        "correct_abstentions": sum(not r["names"] for r in negatives),
        "wrong_refusals": sum(not r["names"] for r in positives),
        "close_cases": len(close),
        "close_top1": sum(r["top1"] for r in close),
        "close_top4": sum(r["top4"] for r in close),
        "semantic_dropped": sum("semantic" not in r["method"] for r in rows),
        "calls": len(rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--cold-calls", type=int, default=3)
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    args.database = args.database.resolve()
    args.output = args.output.resolve()
    if args.output.exists():
        parser.error("output already exists; preserve earlier measurements")
    # The scope bounds the entire process tree. Child address spaces get the
    # same additional bound, with core dumps disabled.
    resource.setrlimit(resource.RLIMIT_AS, (8 * 1024**3, 8 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    for name in ("CAPALITH_RANKING", "CAPALITH_RANKING_CONFIG", "TYPESAFE_API_KEY"):
        os.environ.pop(name, None)
    os.environ.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", TOKENIZERS_PARALLELISM="false")
    os.environ["PYTHONPATH"] = str(args.repo)
    sys.path.insert(0, str(args.repo))
    import anyio
    from mcp import ClientSession, StdioServerParameters, stdio_client

    document = json.loads(args.cases.read_text())
    cases = document["cases"]
    result = {
        "repo": str(args.repo), "database_sha256": hashlib.sha256(args.database.read_bytes()).hexdigest(),
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "started_at": time.time(), "load_before": os.getloadavg(),
        "cpu_count": os.cpu_count(), "scope_memory_max": Path("/proc/self/cgroup").read_text(),
        "memory_limit_bytes": 8 * 1024**3, "ranking": "off", "rows": [],
        "cold_cli_ms": [], "state": "running",
    }
    def save() -> None:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    save()
    memory_file = args.output.with_suffix(".mcp-memory.json")
    wrapper = (
        "import atexit,json,resource,runpy,sys; from pathlib import Path; "
        f"atexit.register(lambda: Path({str(memory_file)!r}).write_text(json.dumps({{'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}}))); "
        "sys.argv=['capalith.mcp_server',*sys.argv[1:]]; runpy.run_module('capalith.mcp_server',run_name='__main__')"
    )
    async def exercise() -> None:
        params = StdioServerParameters(command=sys.executable, args=["-c", wrapper, "--db", str(args.database)], cwd=args.repo, env=dict(os.environ))
        with args.output.with_suffix(".server.log").open("w") as errlog:
            async with stdio_client(params, errlog=errlog) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    for repetition in range(args.repeats + 1):
                        for case in cases:
                            started = time.perf_counter()
                            response = await session.call_tool("discover", {"query":case["query"], "limit":4})
                            elapsed = 1000 * (time.perf_counter() - started)
                            if response.is_error:
                                raise RuntimeError(response.content)
                            body = response.structured_content
                            if body is None:
                                body = json.loads(response.content[0].text)
                            names = [r["name"] for r in body["candidates"]]
                            accepted = set(case["acceptable_skills"])
                            result["rows"].append({"id":case["id"], "repetition":repetition, "ms":elapsed,
                                "acceptable_skills":case["acceptable_skills"], "group":case.get("group","strong"), "names":names,
                                "top1":bool(names and names[0] in accepted), "top4":bool(accepted.intersection(names[:4])),
                                "method":body["retrieval"]["method"], "retrieval":body["retrieval"], "load":os.getloadavg()})
                        save()
    anyio.run(exercise)
    for index in range(args.cold_calls):
        started = time.perf_counter()
        child = subprocess.run([sys.executable,"-m","capalith","--db",str(args.database),"--limit","4","discover",cases[index % len(cases)]["query"]], cwd=args.repo, env=os.environ, capture_output=True, text=True, timeout=120)
        if child.returncode:
            raise RuntimeError(child.stderr)
        result["cold_cli_ms"].append(1000 * (time.perf_counter() - started))
        save()
    warm = [r["ms"] for r in result["rows"] if r["repetition"] > 0]
    result.update(quality=summarize([r for r in result["rows"] if r["repetition"] == 0]),
        warm_mcp_p50_ms=percentile(warm,.5), warm_mcp_p95_ms=percentile(warm,.95),
        first_mcp_ms=result["rows"][0]["ms"], load_after=os.getloadavg(),
        child_peak_rss_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        mcp_memory=json.loads(memory_file.read_text()) if memory_file.exists() else None,
        finished_at=time.time(), state="complete")
    save()
    print(json.dumps({k:v for k,v in result.items() if k not in {"rows","repo","database_sha256","cases_sha256"}}, indent=2))


if __name__ == "__main__":
    main()
