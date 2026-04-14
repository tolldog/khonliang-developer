"""Tests for the test-runner parser + formatter.

These are unit tests over the parser using canned pytest output fragments.
End-to-end execution (actually running pytest in a temp project) is left
to integration-style tests; here we cover the parsing surface that the
distiller depends on.
"""

from __future__ import annotations

from developer.tests_runner import (
    FailureRecord,
    RunResult,
    _parse_pytest_output,
    format_compact,
    format_brief,
    format_full,
    format_response,
)


# ---------------------------------------------------------------------------
# Canned pytest output samples
# ---------------------------------------------------------------------------


ALL_PASSED = """\
collected 12 items

............                                                            [100%]
============================== 12 passed in 0.42s ==============================
"""


SOME_FAILED = """\
collected 14 items

..FF..........                                                           [100%]
=================================== FAILURES ===================================
__________________________ test_first_failure __________________________________

    def test_first_failure():
>       assert 1 == 2
E       assert 1 == 2

tests/test_a.py:5: AssertionError
__________________________ test_second_failure _________________________________

    def test_second_failure():
>       assert "foo" in "bar"
E       AssertionError: assert 'foo' in 'bar'

tests/test_a.py:10: AssertionError
=========================== short test summary info ============================
FAILED tests/test_a.py::test_first_failure - assert 1 == 2
FAILED tests/test_a.py::test_second_failure - AssertionError: assert 'foo' in 'bar'
==================== 2 failed, 12 passed in 1.02s ============================
"""


ERRORED_AND_SKIPPED = """\
collected 5 items

.s.E.                                                                   [100%]
=========================== short test summary info ============================
ERROR tests/test_b.py::test_setup_fails - fixture 'x' not found
==================== 1 error, 3 passed, 1 skipped in 0.30s ===================
"""


UNPARSEABLE = """\
Traceback (most recent call last):
  File "/usr/bin/pytest", line 5, in <module>
    from pytest import console_main
ModuleNotFoundError: No module named 'pytest'
"""


# pytest -q --no-header output — no decorative bars around the summary line.
# This is what `run_pytest()` actually produces in production; regression
# coverage for the bar-optional summary regex.
Q_MODE_ALL_PASSED = """\
........................................................................ [ 59%]
.................................................                        [100%]
121 passed in 10.50s
"""


Q_MODE_MIXED = """\
..F..E.s.                                                              [100%]
=========================== short test summary info ============================
FAILED tests/test_x.py::test_bar - assert 1 == 2
ERROR tests/test_x.py::test_setup_fails - fixture 'y' missing
1 failed, 1 error, 6 passed, 1 skipped in 2.05s
"""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_all_passed():
    r = _parse_pytest_output(ALL_PASSED)
    assert r.parsed is True
    assert r.passed == 12
    assert r.failed == 0
    assert r.errors == 0
    assert r.skipped == 0
    assert r.collected == 12
    assert r.failures == []


def test_parse_some_failed_extracts_failures_with_messages():
    r = _parse_pytest_output(SOME_FAILED)
    assert r.parsed is True
    assert r.passed == 12
    assert r.failed == 2
    assert r.collected == 14
    nodeids = {f.nodeid for f in r.failures}
    assert nodeids == {
        "tests/test_a.py::test_first_failure",
        "tests/test_a.py::test_second_failure",
    }
    first = next(f for f in r.failures if f.nodeid.endswith("test_first_failure"))
    assert "1 == 2" in first.message
    # Excerpt should contain the assert line and surrounding context
    assert any("assert 1 == 2" in line for line in first.excerpt)


def test_parse_errors_and_skips():
    r = _parse_pytest_output(ERRORED_AND_SKIPPED)
    assert r.parsed is True
    assert r.passed == 3
    assert r.errors == 1
    assert r.skipped == 1
    assert r.failed == 0


def test_parse_unparseable_returns_parsed_false():
    r = _parse_pytest_output(UNPARSEABLE)
    assert r.parsed is False
    assert r.passed == 0
    assert r.failed == 0


def test_parse_empty_returns_empty_result():
    r = _parse_pytest_output("")
    assert r.parsed is False


def test_parse_q_mode_all_passed_without_bars():
    """Regression: -q --no-header emits the summary line without `=+ =+` bars."""
    r = _parse_pytest_output(Q_MODE_ALL_PASSED)
    assert r.parsed is True
    assert r.passed == 121
    assert r.failed == 0


def test_parse_q_mode_mixed_without_bars():
    r = _parse_pytest_output(Q_MODE_MIXED)
    assert r.parsed is True
    assert r.passed == 6
    assert r.failed == 1
    assert r.errors == 1
    assert r.skipped == 1
    # short test summary block gives us one failure nodeid
    assert any(f.nodeid == "tests/test_x.py::test_bar" for f in r.failures)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _result_with(**counts) -> RunResult:
    r = RunResult(command=["python", "-m", "pytest"], cwd="/p", returncode=0, elapsed_s=0.5)
    for k, v in counts.items():
        setattr(r, k, v)
    r.parsed = True
    return r


def test_compact_all_passed_is_status_ok():
    r = _result_with(passed=12)
    line = format_compact(r)
    assert line.startswith("status=ok")
    assert "passed=12" in line
    assert "failed=0" in line


def test_compact_with_failures_is_status_fail():
    r = _result_with(passed=10, failed=2)
    r.returncode = 1
    line = format_compact(r)
    assert line.startswith("status=fail")


def test_compact_reports_timeout_and_parse_state():
    r = _result_with(passed=0)
    r.timed_out = True
    r.parsed = False
    line = format_compact(r)
    assert "timed_out=true" in line
    assert "parsed=false" in line


def test_brief_includes_failure_lines():
    r = RunResult(command=[], cwd="/p", returncode=1, elapsed_s=1.0,
                     passed=10, failed=2, parsed=True,
                     failures=[
                         FailureRecord(nodeid="tests/test_a.py::test_x", message="assert 1 == 2"),
                         FailureRecord(nodeid="tests/test_a.py::test_y", message="KeyError: 'foo'"),
                     ])
    out = format_brief(r)
    assert "FAIL tests/test_a.py::test_x" in out
    assert "assert 1 == 2" in out
    assert "FAIL tests/test_a.py::test_y" in out


def test_full_includes_trace_excerpts():
    r = RunResult(command=[], cwd="/p", returncode=1, elapsed_s=1.0,
                     passed=0, failed=1, parsed=True,
                     failures=[
                         FailureRecord(
                             nodeid="tests/test_a.py::test_x",
                             message="assert 1 == 2",
                             excerpt=["    def test_x():", ">       assert 1 == 2", "E       assert 1 == 2"],
                         ),
                     ])
    out = format_full(r)
    assert "assert 1 == 2" in out
    assert "--- tests/test_a.py::test_x" in out


def test_full_falls_back_to_raw_when_unparseable():
    r = RunResult(command=[], cwd="/p", returncode=1, elapsed_s=0.0, parsed=False,
                     raw_output="some garbage pytest couldn't give us")
    out = format_full(r)
    assert "raw pytest output" in out
    assert "some garbage" in out


def test_format_response_dispatches_by_detail():
    r = _result_with(passed=1)
    assert format_response(r, "compact").startswith("status=ok")
    assert "passed=1" in format_response(r, "brief")
    assert "passed=1" in format_response(r, "full")
    # Unknown detail falls back to brief
    assert format_response(r, "weird") == format_brief(r)
