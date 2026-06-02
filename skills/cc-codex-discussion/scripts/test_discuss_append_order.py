"""Fail-closed ordering tests for `discuss.py append` (no test framework dependency).
Run: python skills/cc-codex-discussion/scripts/test_discuss_append_order.py
Exits non-zero on first failure.

append must reject any block that does not continue the cc/codex alternation with
increasing rounds (cc r1, codex r1, cc r2, codex r2, ...), and must leave the file
unchanged on rejection. This blocks the "skipped a turn" mistake at write time instead
of letting `check` discover it after the fact.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "discuss.py"


def _run(args, stdin="", cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin, cwd=cwd, capture_output=True, text=True,
    )


def _new(work):
    p = _run(["new", "t", "--topic", "t"], cwd=work)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def _append(F, role, rnd, body="x"):
    return _run(["append", F, "--role", role, "--round", str(rnd)], stdin=body + "\n")


def test_in_order_succeeds():
    with tempfile.TemporaryDirectory() as w:
        F = _new(w)
        for role, rnd in [("cc", 1), ("codex", 1), ("cc", 2), ("codex", 2), ("cc", 3)]:
            p = _append(F, role, rnd)
            assert p.returncode == 0, f"{role} r{rnd} should succeed: {p.stderr}"
        chk = _run(["check", F])
        assert chk.returncode == 0 and '"blocks": 5' in chk.stdout, chk.stdout + chk.stderr


def test_skip_other_side_is_rejected():
    # the real bug: cc1, codex1, cc2, then cc3 (skipped codex2)
    with tempfile.TemporaryDirectory() as w:
        F = _new(w)
        for role, rnd in [("cc", 1), ("codex", 1), ("cc", 2)]:
            assert _append(F, role, rnd).returncode == 0
        before = Path(F).read_text(encoding="utf-8")
        p = _append(F, "cc", 3)  # should be codex r2
        assert p.returncode != 0, "appending cc r3 after cc r2 must be rejected"
        assert "expected codex r2" in p.stderr, p.stderr
        assert Path(F).read_text(encoding="utf-8") == before, "file must be unchanged on rejection"


def test_wrong_first_block_rejected():
    with tempfile.TemporaryDirectory() as w:
        F = _new(w)
        p = _append(F, "codex", 1)  # first block must be cc r1
        assert p.returncode != 0 and "expected cc r1" in p.stderr, p.stderr


def test_duplicate_rejected():
    with tempfile.TemporaryDirectory() as w:
        F = _new(w)
        assert _append(F, "cc", 1).returncode == 0
        p = _append(F, "cc", 1)  # expected codex r1 now
        assert p.returncode != 0, "re-appending cc r1 must be rejected"


if __name__ == "__main__":
    test_in_order_succeeds()
    test_skip_other_side_is_rejected()
    test_wrong_first_block_rejected()
    test_duplicate_rejected()
    print("OK: 4 passed")
