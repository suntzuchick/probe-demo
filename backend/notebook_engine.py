"""
Notebook execution engine for Probe.

Each session gets a persistent kernel subprocess (kernel_runner.py) that
keeps heavy imports (pandas, numpy, matplotlib, lifelines) loaded in memory.
The first cell run for a session pays the ~20s startup cost once; subsequent
cells execute in ~1s or less.

Cell execution protocol:
  1. Send JSON line to kernel stdin: {"session_dir": "...", "code": "..."}
  2. Kernel loads any persisted dataframes from disk into its namespace
  3. Kernel executes the cell and captures stdout / matplotlib figures
  4. Kernel persists any changed KNOWN_VARS back to disk
  5. Kernel writes JSON result between sentinel lines on stdout

State is kept both in the kernel's in-memory namespace (fast path) and as
pickle files on disk (so it survives kernel restarts and is accessible to
save_state / load_state helpers called by app.py).

This is real Python execution with no sandbox against malicious code —
appropriate for a local single-tenant demo, not for untrusted multi-tenant
production. That tradeoff is intentional.
"""
import json
import os
import pickle
import queue
import subprocess
import sys
import threading

EXEC_TIMEOUT_SECONDS = 30
KERNEL_STARTUP_TIMEOUT = 60

_RUNNER = os.path.join(os.path.dirname(__file__), "kernel_runner.py")

# session_dir -> {"proc": Popen, "lock": Lock}
_kernels: dict[str, dict] = {}
_kernels_lock = threading.Lock()


def _start_kernel() -> dict:
    proc = subprocess.Popen(
        [sys.executable, _RUNNER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    # Block until kernel signals imports are done
    import time
    t0 = time.monotonic()
    while True:
        line = proc.stdout.readline()
        if not line:
            stderr = proc.stderr.read()
            raise RuntimeError(f"Kernel process exited during startup.\n{stderr}")
        if "___PROBE_READY___" in line:
            break
        if time.monotonic() - t0 > KERNEL_STARTUP_TIMEOUT:
            proc.kill()
            raise RuntimeError("Kernel startup timed out")

    return {"proc": proc, "lock": threading.Lock()}


def _get_kernel(session_dir: str) -> dict:
    with _kernels_lock:
        entry = _kernels.get(session_dir)
        if entry is not None and entry["proc"].poll() is None:
            return entry
        entry = _start_kernel()
        _kernels[session_dir] = entry
        return entry


def _state_path(session_dir, varname):
    return os.path.join(session_dir, f"{varname}.pkl")


def save_state(session_dir, varname, value):
    os.makedirs(session_dir, exist_ok=True)
    with open(_state_path(session_dir, varname), "wb") as f:
        pickle.dump(value, f)


def load_state(session_dir, varname):
    path = _state_path(session_dir, varname)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def available_vars(session_dir):
    if not os.path.isdir(session_dir):
        return []
    return [f[:-4] for f in os.listdir(session_dir) if f.endswith(".pkl")]


def run_cell(session_dir: str, code: str) -> dict:
    os.makedirs(session_dir, exist_ok=True)

    entry = _get_kernel(session_dir)
    proc = entry["proc"]
    cell_lock = entry["lock"]

    request_line = json.dumps({"session_dir": session_dir, "code": code}) + "\n"

    with cell_lock:
        try:
            proc.stdin.write(request_line)
            proc.stdin.flush()
        except BrokenPipeError:
            # Kernel died; get a fresh one and retry
            with _kernels_lock:
                _kernels.pop(session_dir, None)
            entry = _get_kernel(session_dir)
            proc = entry["proc"]
            proc.stdin.write(request_line)
            proc.stdin.flush()

        result_q: queue.Queue = queue.Queue()

        def _read_response():
            collecting = False
            chunks = []
            while True:
                line = proc.stdout.readline()
                if not line:
                    result_q.put(("dead", ""))
                    return
                if "___PROBE_OUTPUT_START___" in line:
                    collecting = True
                    continue
                if "___PROBE_OUTPUT_END___" in line:
                    result_q.put(("ok", "".join(chunks)))
                    return
                if collecting:
                    chunks.append(line)

        reader = threading.Thread(target=_read_response, daemon=True)
        reader.start()

        try:
            kind, data = result_q.get(timeout=EXEC_TIMEOUT_SECONDS)
        except queue.Empty:
            with _kernels_lock:
                _kernels.pop(session_dir, None)
            proc.kill()
            return {"status": "error", "error": f"Execution exceeded {EXEC_TIMEOUT_SECONDS}s timeout."}

    if kind == "dead":
        with _kernels_lock:
            _kernels.pop(session_dir, None)
        return {"status": "error", "error": "Kernel process exited unexpectedly."}

    try:
        output = json.loads(data.strip())
    except Exception:
        return {"status": "error", "error": "Could not parse kernel output."}

    if output.get("error"):
        return {"status": "error", "error": output["error"]}

    return {
        "status": "ok",
        "stdout": output.get("stdout", ""),
        "result_repr": output.get("result_repr"),
        "figures": output.get("figures", []),
    }


if __name__ == "__main__":
    import pandas as pd
    test_dir = "/tmp/probe_test_session"
    os.makedirs(test_dir, exist_ok=True)
    df = pd.DataFrame({"USUBJID": ["1", "2", "3"], "AGE": [55, 60, 65], "ARMCD": ["DARA", "CHEMO", "DARA"]})
    save_state(test_dir, "adsl", df)

    print("Starting kernel (first call will be slow — imports loading)...")
    r = run_cell(test_dir, "print(adsl.shape)\nadsl.groupby('ARMCD')['AGE'].mean()")
    print("TEST 1:", r)

    print("Second cell (should be fast)...")
    r2 = run_cell(test_dir, "import matplotlib.pyplot as plt\nplt.plot([1,2,3],[4,5,6])\nplt.title('test')")
    print("TEST 2 has figure:", len(r2.get("figures", [])) > 0)

    r3 = run_cell(test_dir, "1/0")
    print("TEST 3 (error):", r3["status"], r3["error"][-100:])
