"""Behavior tests for `discuss.py new --scratch` (no test framework dependency).
Run: python skills/cc-codex-discussion/scripts/test_discuss_scratch.py
Exits non-zero on first failure.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "discuss.py"


def _run(args, cwd):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(cwd), capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr}"
    return proc.stdout.strip()


def test_scratch_goes_to_system_temp():
    # run from an arbitrary cwd; --scratch must ignore cwd and use system temp
    with tempfile.TemporaryDirectory() as work:
        out = _run(["new", "plan-test", "--topic", "neutral topic", "--scratch"], cwd=work)
        path = Path(out)
        tmp_root = Path(tempfile.gettempdir()).resolve()
        assert tmp_root in path.resolve().parents, f"{path} not under system temp {tmp_root}"
        assert path.parent.name == "cc-codex-discussion-history"
        assert path.exists(), "new should create the file"
        assert Path(work).resolve() not in path.resolve().parents, "must NOT be under cwd"


def test_no_scratch_keeps_cwd_behavior():
    with tempfile.TemporaryDirectory() as work:
        out = _run(["new", "plan-test", "--topic", "neutral topic"], cwd=work)
        path = Path(out)
        assert Path(work).resolve() in path.resolve().parents, f"{path} not under cwd {work}"
        assert path.parent.name == "cc-codex-discussion-history"


if __name__ == "__main__":
    test_scratch_goes_to_system_temp()
    test_no_scratch_keeps_cwd_behavior()
    print("OK: 2 passed")
