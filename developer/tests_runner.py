"""Test-runner + distiller.

Runs pytest in a project's workspace and distills the output into a compact
structured digest that Claude (or any other consumer) can read cheaply,
instead of ingesting the raw pytest output on every turn.

**Subprocess exception — documented per ``feedback_prefer_python_modules_over_execs``:**
The project-wide default is to prefer Python SDK / module APIs over shelling
out. This module deliberately uses subprocess because each target project
has its own venv, and Python cannot load two different venvs into one
process. Running pytest in-process would couple developer's interpreter
to every project under test. The subprocess boundary is the isolation we
need here.

Future improvement: swap the stdout-regex parser for JUnit XML output
(``--junitxml=<tmpfile>``) so the data path is structured even though we
still exec. Tracked as a follow-up.

Scope notes:
- pytest only for now. Other frameworks can land as follow-ups; the parser
  is localized here, so swapping test framework means one module.
- No LLM summarization in this pass — that's a later improvement once the
  LLM-planner infrastructure is in place (``fr_developer_6d480f34``).
- No result caching yet. Follow-up when the hit rate justifies it.
- Timeouts default to 5 minutes; callable passes may override via config.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class FailureRecord:
    """A single failing test case with a short trace excerpt."""

    nodeid: str  # e.g. "tests/test_agent.py::test_foo"
    message: str  # one-line error / assertion summary
    excerpt: list[str] = field(default_factory=list)  # 5-10 line trace excerpt


@dataclass
class RunResult:
    """Structured outcome of a pytest run.

    ``parsed`` is False when we couldn't parse the summary line — callers
    should fall back to ``raw_output``.
    """

    command: list[str]
    cwd: str
    returncode: int
    elapsed_s: float
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    warnings: int = 0
    collected: int = 0
    failures: list[FailureRecord] = field(default_factory=list)
    parsed: bool = False
    raw_output: str = ""  # preserved for fallback when parsing fails
    timed_out: bool = False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


DEFAULT_TIMEOUT_SECONDS = 300


def _resolve_python_exe(cwd: Path) -> str:
    """Pick the Python interpreter for a pytest run.

    Prefers the target project's in-tree ``.venv/bin/python`` so tests run
    under the project's own dependencies rather than developer's venv.
    Falls back to ``sys.executable`` if no local venv is found, which is
    still better than bare ``"python"`` from PATH (non-deterministic).

    Callers may override via the ``python_exe`` parameter on
    :func:`run_pytest` when they know better.
    """
    local_venv = cwd / ".venv" / "bin" / "python"
    if local_venv.exists():
        return str(local_venv)
    return sys.executable


async def run_pytest(
    cwd: str | Path,
    target: str = "",
    *,
    timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
    extra_args: list[str] | None = None,
    python_exe: str | None = None,
) -> RunResult:
    """Run pytest in ``cwd`` and return a parsed :class:`RunResult`.

    ``target`` is an optional path or pytest node id (``tests/test_foo.py``
    or ``tests/test_foo.py::test_bar``); empty runs the whole suite.

    ``python_exe`` overrides interpreter selection. When unset, defaults
    to ``<cwd>/.venv/bin/python`` if present, else ``sys.executable``.
    Never uses bare ``"python"`` from PATH — that's non-deterministic
    and undermines per-project venv isolation.
    """
    cwd_path = Path(cwd)
    if python_exe is None:
        python_exe = _resolve_python_exe(cwd_path)

    cmd = [
        python_exe,
        "-m",
        "pytest",
        "--tb=short",
        "--no-header",
        "-q",
    ]
    if extra_args:
        cmd.extend(extra_args)
    if target:
        cmd.append(target)

    loop = asyncio.get_running_loop()
    started = loop.time()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except FileNotFoundError as e:
        return RunResult(
            command=cmd,
            cwd=str(cwd_path),
            returncode=-1,
            elapsed_s=0.0,
            parsed=False,
            raw_output=f"failed to launch pytest: {e}",
        )

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        timed_out = False
    except asyncio.TimeoutError:
        proc.kill()
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            stdout = b""
        timed_out = True

    elapsed = loop.time() - started
    raw = stdout.decode("utf-8", errors="replace") if stdout else ""

    if timed_out:
        return RunResult(
            command=cmd,
            cwd=str(cwd_path),
            returncode=proc.returncode or -1,
            elapsed_s=elapsed,
            parsed=False,
            raw_output=raw,
            timed_out=True,
        )

    parsed = _parse_pytest_output(raw)
    parsed.command = cmd
    parsed.cwd = str(cwd_path)
    parsed.returncode = proc.returncode or 0
    parsed.elapsed_s = elapsed
    parsed.raw_output = raw
    return parsed


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


# pytest summary line examples seen in the wild:
#   "== 12 passed in 0.42s =="                          — default / -v
#   "== 2 failed, 10 passed in 1.02s =="                — default w/ failures
#   "== 1 failed, 1 error, 3 passed, 2 skipped in 0.80s ==" — with errors+skipped
#   "121 passed in 10.50s"                              — -q (bars omitted)
#   "2 failed, 119 passed in 10.50s"                    — -q w/ failures
# The decorative `=+` bars are optional in -q mode. Make them optional on
# both sides so the same regex handles quiet and verbose output.
_SUMMARY_RE = re.compile(
    r"^(?:=+\s+)?(?P<body>.+?)\s+in\s+(?P<elapsed>\d+(?:\.\d+)?)s(?:\s+=+)?\s*$",
    re.MULTILINE,
)
_COUNT_RE = re.compile(r"(\d+)\s+([a-z]+)")

# Failure block header like:
#   "FAILED tests/test_foo.py::test_bar - AssertionError: expected 3 got 2"
_FAILED_LINE_RE = re.compile(
    r"^FAILED\s+(?P<nodeid>\S+)(?:\s+-\s+(?P<message>.+))?$",
    re.MULTILINE,
)

# Collected line for -q mode; sometimes absent
_COLLECTED_RE = re.compile(r"^collected\s+(\d+)\s+items?", re.MULTILINE)


def _parse_pytest_output(text: str) -> RunResult:
    """Best-effort parser for pytest -q output. Never raises."""
    result = RunResult(command=[], cwd="", returncode=0, elapsed_s=0.0)

    if not text:
        return result

    # Summary counts
    match = _SUMMARY_RE.search(text)
    if match:
        body = match.group("body")
        for count_match in _COUNT_RE.finditer(body):
            n = int(count_match.group(1))
            kind = count_match.group(2)
            if kind == "passed":
                result.passed = n
            elif kind == "failed":
                result.failed = n
            elif kind in ("error", "errors"):
                result.errors = n
            elif kind == "skipped":
                result.skipped = n
            elif kind == "xfailed":
                result.xfailed = n
            elif kind == "xpassed":
                result.xpassed = n
            elif kind in ("warning", "warnings"):
                result.warnings = n
        result.parsed = True

    collected_match = _COLLECTED_RE.search(text)
    if collected_match:
        result.collected = int(collected_match.group(1))
    else:
        result.collected = (
            result.passed + result.failed + result.errors
            + result.skipped + result.xfailed + result.xpassed
        )

    # Per-failure entries from the "short test summary info" section
    for f_match in _FAILED_LINE_RE.finditer(text):
        nodeid = f_match.group("nodeid")
        message = (f_match.group("message") or "").strip()
        excerpt = _extract_failure_excerpt(text, nodeid)
        result.failures.append(
            FailureRecord(nodeid=nodeid, message=message, excerpt=excerpt)
        )

    # If we saw failures in the summary but couldn't parse nodeids, still
    # report parsed=True so callers get the counts.
    if result.failed and not result.failures:
        # No structured failures available — leave failures[] empty; counts
        # are still authoritative.
        pass

    return result


def _extract_failure_excerpt(text: str, nodeid: str, context_lines: int = 8) -> list[str]:
    """Pull a short trace excerpt for ``nodeid`` from the failure section.

    pytest --tb=short prints a block per failure headed by ``_____ test_name _____``
    followed by a short traceback and the error line. We find the block whose
    header matches the test name and return up to ``context_lines`` lines
    from it (excluding the header itself).
    """
    # nodeid is path::test[::param]; the block header uses the test function
    # name (last component). Split on :: and take the bit after the file.
    parts = nodeid.split("::")
    if len(parts) < 2:
        return []
    test_fn = parts[-1].split("[")[0]  # strip parametrize brackets

    # pytest short-form blocks look like:
    #   "___________________________ test_fn ____________________________"
    # find that anchor, then collect up to the next block/summary boundary.
    header_re = re.compile(rf"^_+\s+{re.escape(test_fn)}\s+_+\s*$", re.MULTILINE)
    header = header_re.search(text)
    if not header:
        return []

    tail = text[header.end():]
    lines = tail.split("\n")
    excerpt: list[str] = []
    for line in lines:
        if re.match(r"^_+\s+\S.+_+\s*$", line):  # next block header
            break
        if line.startswith("=") and line.endswith("="):  # summary bar
            break
        excerpt.append(line.rstrip())
        if len(excerpt) >= context_lines:
            break

    # Trim trailing blank lines
    while excerpt and not excerpt[-1].strip():
        excerpt.pop()
    return excerpt


# ---------------------------------------------------------------------------
# Formatting for compact | brief | full detail modes
# ---------------------------------------------------------------------------


def format_compact(result: RunResult) -> str:
    """Single pipe-delimited line for agent loops."""
    status = "ok" if result.failed == 0 and result.errors == 0 and result.returncode == 0 else "fail"
    bits = [
        f"status={status}",
        f"passed={result.passed}",
        f"failed={result.failed}",
        f"errors={result.errors}",
        f"skipped={result.skipped}",
        f"elapsed={result.elapsed_s:.2f}s",
    ]
    if result.timed_out:
        bits.append("timed_out=true")
    if not result.parsed:
        bits.append("parsed=false")
    return "|".join(bits)


def format_brief(result: RunResult) -> str:
    """Counts + one line per failure."""
    lines = [format_compact(result)]
    if result.failures:
        lines.append("")
        for f in result.failures:
            msg = f.message if f.message else "(no message)"
            lines.append(f"  FAIL {f.nodeid}: {msg}")
    elif result.failed and not result.failures:
        lines.append("")
        lines.append("  (pytest reported failures but node ids couldn't be parsed — use detail=full)")
    return "\n".join(lines)


def format_full(result: RunResult) -> str:
    """Brief + per-failure trace excerpt. Still smaller than raw pytest output."""
    lines = [format_brief(result)]
    for f in result.failures:
        if not f.excerpt:
            continue
        lines.append("")
        lines.append(f"  --- {f.nodeid}")
        for line in f.excerpt:
            lines.append(f"    {line}")
    if not result.parsed:
        # Fall back to raw output so callers get the actual error context.
        lines.append("")
        lines.append("  --- raw pytest output (parser couldn't extract structure) ---")
        lines.append(result.raw_output)
    return "\n".join(lines)


def format_response(result: RunResult, detail: str = "brief") -> str:
    if detail == "compact":
        return format_compact(result)
    if detail == "full":
        return format_full(result)
    return format_brief(result)
