import subprocess
import sys
from pathlib import Path

_PROJECT_CONFIG = Path(__file__).parent.parent / "pyproject.toml"

# The worker deadlocks on a lock nobody releases, and the test leaves the pool
# waiting for it: interrupting the main thread alone does not end this test.
_HANGING = """
import threading
from concurrent.futures import ThreadPoolExecutor


def test_waits_on_a_deadlocked_worker():
    lock = threading.Lock()
    lock.acquire()
    with ThreadPoolExecutor(1) as pool:
        pool.submit(lock.acquire).result()
"""


# The project's own pytest configuration, with only its limits shortened: a
# hanging test ends the run with a failure and the stacks of its threads.
def test_hanging_test_ends_the_run_with_its_stacks(tmp_path: Path):
    (tmp_path / "test_hanging.py").write_text(_HANGING)

    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(_PROJECT_CONFIG),
            "-o",
            "timeout=2",
            "-o",
            "faulthandler_timeout=1",
            "-p",
            "no:cacheprovider",
            str(tmp_path / "test_hanging.py"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=20,
        check=False,
    )

    assert run.returncode != 0
    # pytest-timeout's report: the stack of every thread, the worker's too.
    assert "+ Timeout +" in run.stdout
    assert "Stack of ThreadPoolExecutor" in run.stdout
    assert "in test_waits_on_a_deadlocked_worker" in run.stdout
    # faulthandler's dump, written before it.
    assert run.stdout.index("Timeout (0:00:01)!") < run.stdout.index("+ Timeout +")
