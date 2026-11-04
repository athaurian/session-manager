#!/usr/bin/env python3
"""Session restore benchmark against a headless Sway compositor.

The benchmark runs inside the integration container. Fake apps launch foot
windows with controlled delays, so the runner can measure how long restore
takes and whether the final layout matches the saved session. See README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MAGIC = b"i3-ipc"
MSG_GET_TREE = 4
DEFAULT_WORKDIR = Path("/tmp/session-manager-bench")
ASSETS = Path(__file__).resolve().parent


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    """Read exactly size bytes from the compositor socket."""
    chunks: list[bytes] = []
    while size:
        chunk = sock.recv(size)
        if not chunk:
            raise ConnectionError("socket closed")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def ipc_get_tree(sock_path: Path) -> dict[str, Any]:
    """Return the compositor tree over a fresh IPC connection."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(sock_path))
        sock.sendall(MAGIC + struct.pack("=II", 0, MSG_GET_TREE))
        _magic, length, _msg_type = struct.unpack("=6sII", _recv_exact(sock, 14))
        return json.loads(_recv_exact(sock, length))
    finally:
        sock.close()


def iter_workspaces(node: dict[str, Any]):
    """Yield every workspace in a compositor tree."""
    if node.get("type") == "workspace":
        yield node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        yield from iter_workspaces(child)


def _titles(node: dict[str, Any]) -> list[str]:
    """Return the window titles in a subtree, in tree order."""
    titles: list[str] = []
    if node.get("type") in ("con", "floating_con") and node.get("app_id"):
        titles.append(node.get("name") or "")
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        titles.extend(_titles(child))
    return titles


def layout(tree: dict[str, Any]) -> dict[str, list[str]]:
    """Return workspace name -> window titles in tree order."""
    result: dict[str, list[str]] = {}
    for ws in iter_workspaces(tree):
        name = ws.get("name") or ""
        if name.startswith("__"):
            continue
        result[name] = _titles(ws)
    return result


def window(app_id: str, name: str, class_name: str = "") -> dict[str, Any]:
    """Return a saved window node."""
    return {
        "type": "window",
        "app_id": app_id,
        "class": class_name,
        "name": name,
        "pid": 0,
        "fullscreen_mode": 0,
        "rect": {"x": 0, "y": 0, "width": 800, "height": 600},
        "marks": [],
        "scratchpad_state": "none",
    }


def workspace(name: str, nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a saved workspace node."""
    return {
        "type": "workspace",
        "name": name,
        "layout": "splith",
        "nodes": nodes,
        "floating_nodes": [],
    }


def payload(scenario: str) -> dict[str, Any]:
    """Return a saved session shaped like the benchmark scenario."""
    if scenario == "mixed":
        workspaces = [
            workspace(
                "1",
                [
                    window("helium", "h0"),
                    window("fastapp", "fast app", "fastapp"),
                    window("helium", "h1"),
                ],
            ),
            workspace(
                "2",
                [
                    window("benchapp", "bench app", "benchapp"),
                    window("fastapp", "fast app", "fastapp"),
                ],
            ),
        ]
    else:
        workspaces = [
            workspace(
                "1",
                [
                    window("helium", "h0"),
                    window("helium", "h1"),
                    window("helium", "h2"),
                ],
            ),
            workspace(
                "2",
                [
                    window("benchapp", "bench app", "benchapp"),
                    window("fastapp", "fast app", "fastapp"),
                    window("fastapp", "fast app", "fastapp"),
                ],
            ),
        ]
    return {
        "workspaces": workspaces,
        "hidden_scratchpad": [],
        "background_apps": [],
        "focused_workspace": "2",
    }


def expected_layout(scenario: str) -> dict[str, list[str]]:
    """Return the layout the restored session should produce."""
    if scenario == "mixed":
        return {"1": ["h0", "fast app", "h1"], "2": ["bench app", "fast app"]}
    return {"1": ["h0", "h1", "h2"], "2": ["bench app", "fast app", "fast app"]}


def write_desktop_entries(home: Path, fakebin: Path) -> None:
    """Install desktop entries so gtk-launch can start the fake apps."""
    apps = home / ".local" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    for name in ("benchapp", "fastapp"):
        (apps / f"{name}.desktop").write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={name}\n"
            f"Exec={fakebin / name}\n"
            "Terminal=false\n",
            encoding="utf-8",
        )


def start_sway(
    workdir: Path, env: dict[str, str]
) -> tuple[subprocess.Popen[bytes], Path, Any]:
    """Start a headless Sway and wait for its IPC socket."""
    runtime = Path(env["XDG_RUNTIME_DIR"])
    config = workdir / "sway.conf"
    config.write_text("font pango:monospace 8\n", encoding="utf-8")
    log_path = workdir / "sway.log"
    log = log_path.open("wb")
    proc = subprocess.Popen(
        ["sway", "--unsupported-gpu", "-c", str(config)],
        env=env,
        stdout=log,
        stderr=log,
    )
    socket_path = None
    for _ in range(300):
        if proc.poll() is not None:
            log.close()
            raise RuntimeError(
                f"sway exited during startup:\n{log_path.read_text(errors='replace')}"
            )
        matches = sorted(runtime.glob("sway-ipc.*.sock"))
        if matches:
            socket_path = matches[0]
            break
        time.sleep(0.02)
    if socket_path is None:
        proc.kill()
        log.close()
        raise RuntimeError(
            "sway socket never appeared:\n"
            f"{log_path.read_text(errors='replace')}\n"
            f"runtime: {sorted(p.name for p in runtime.iterdir())}"
        )
    displays = sorted(
        p.name for p in runtime.iterdir() if p.name.startswith("wayland-")
    )
    if displays:
        env["WAYLAND_DISPLAY"] = displays[0]
    env["SWAYSOCK"] = str(socket_path)
    return proc, socket_path, log


def stop_sway(
    proc: subprocess.Popen[bytes],
    socket_path: Path,
    workdir: Path,
    log: Any,
) -> None:
    """Stop Sway and the fake browser, then reap the process."""
    pidfile = workdir / "helium.pid"
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text().strip()), 9)
        except (OSError, ValueError):
            pass
    if proc.poll() is None:
        subprocess.run(
            ["swaymsg", "--socket", str(socket_path), "exit"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    log.close()


def wait_for_layout(
    socket_path: Path,
    deadline: float,
    child: subprocess.Popen[str],
    expected: dict[str, list[str]],
) -> float | None:
    """Return the time the expected layout appeared, or None."""
    while time.monotonic() < deadline and child.poll() is None:
        try:
            if layout(ipc_get_tree(socket_path)) == expected:
                return time.monotonic()
        except (OSError, ConnectionError, ValueError):
            pass
        time.sleep(0.01)
    return None


def run_once(
    repo: Path,
    scenario: str,
    helium_delay: float,
    app_delay: float,
    workdir: Path,
) -> dict[str, Any]:
    """Run one restore and return its timings."""
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    runtime = workdir / "run"
    runtime.mkdir()
    runtime.chmod(0o700)
    home = workdir / "home"
    state = workdir / "state"
    state.mkdir()
    fakebin = ASSETS / "fakebin"
    write_desktop_entries(home, fakebin)

    (state / "sway_session.json").write_text(
        json.dumps(payload(scenario)), encoding="utf-8"
    )
    notify_log = workdir / "notify.log"

    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_STATE_HOME": str(state),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CURRENT_DESKTOP": "sway",
            "XDG_SESSION_TYPE": "wayland",
            "PATH": f"{fakebin}:{env['PATH']}",
            "PYTHONPATH": str(repo / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "WLR_BACKENDS": "headless",
            "WLR_LIBINPUT_NO_DEVICES": "1",
            "WLR_RENDERER": "pixman",
            "SESSION_MANAGER_BACKEND": "sway",
            "SWAY_SESSION_SKIP_DAEMON_CHECKS": "1",
            "HELIUM_FAKE_DELAY": str(helium_delay),
            "HELIUM_FAKE_TITLES": "h0,h1" if scenario == "mixed" else "h0,h1,h2",
            "HELIUM_FAKE_PIDFILE": str(workdir / "helium.pid"),
            "BENCH_APP_DELAY": str(app_delay),
            "NOTIFY_LOG": str(notify_log),
        }
    )
    for name in ("WAYLAND_DISPLAY", "SWAYSOCK", "I3SOCK", "DISPLAY"):
        env.pop(name, None)

    proc, socket_path, log = start_sway(workdir, env)
    expected = expected_layout(scenario)
    try:
        wall_start = time.time()
        started = time.monotonic()
        child = subprocess.Popen(
            [sys.executable, "-m", "session_manager", "restore"],
            env=env,
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        t_layout = wait_for_layout(socket_path, started + 60, child, expected)
        output, _ = child.communicate(timeout=60)
        t_exit = time.monotonic()
        final = layout(ipc_get_tree(socket_path))
        if t_layout is None and final == expected:
            t_layout = t_exit

        t_notify = None
        if notify_log.exists():
            for line in notify_log.read_text(errors="replace").splitlines():
                stamp, _, message = line.partition(" ")
                if "session restored successfully" in message:
                    t_notify = int(stamp) / 1e9 - wall_start
                    break

        return {
            "total_s": round(t_exit - started, 3),
            "layout_s": round(t_layout - started, 3) if t_layout else None,
            "notify_s": round(t_notify, 3) if t_notify is not None else None,
            "layout_ok": final == expected,
            "exit_code": child.returncode,
            "output": output.strip().splitlines()[-20:],
        }
    finally:
        stop_sway(proc, socket_path, workdir, log)


def seconds(value: float | None) -> str:
    """Format a duration, or a dash when it was not observed."""
    return f"{value:.3f}s" if value is not None else "-"


def summarize(label: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    """Return median timings and layout correctness for one label."""
    totals = [result["total_s"] for result in results]
    layouts = [
        result["layout_s"] for result in results if result["layout_s"] is not None
    ]
    notifies = [
        result["notify_s"] for result in results if result["notify_s"] is not None
    ]
    return {
        "label": label,
        "runs": len(results),
        "total_median": statistics.median(totals),
        "layout_median": statistics.median(layouts) if layouts else None,
        "notify_median": statistics.median(notifies) if notifies else None,
        "layouts_ok": sum(1 for result in results if result["layout_ok"]),
    }


def print_summary(summary: dict[str, Any]) -> None:
    """Print one label's medians."""
    print(
        f"{summary['label']}: total {seconds(summary['total_median'])} "
        f"layout {seconds(summary['layout_median'])} "
        f"notify {seconds(summary['notify_median'])} "
        f"layouts {summary['layouts_ok']}/{summary['runs']}"
    )


def print_delta(baseline: dict[str, Any], current: dict[str, Any]) -> None:
    """Print the change between two labels."""
    for key in ("total_median", "layout_median", "notify_median"):
        before = baseline[key]
        after = current[key]
        if before is None or after is None:
            continue
        difference = after - before
        percent = difference / before * 100 if before else 0.0
        print(f"{key.removesuffix('_median')}: {difference:+.3f}s ({percent:+.1f}%)")


def main() -> int:
    """Run the benchmark and print the results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--helium-delay", type=float, default=2.0)
    parser.add_argument("--app-delay", type=float, default=1.0)
    parser.add_argument("--scenario", choices=("split", "mixed"), default="split")
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    labels = [("current", args.repo)]
    if args.baseline is not None:
        labels.insert(0, ("baseline", args.baseline))

    results: dict[str, list[dict[str, Any]]] = {label: [] for label, _ in labels}
    for run in range(1, args.runs + 1):
        for label, repo in labels:
            result = run_once(
                repo,
                args.scenario,
                args.helium_delay,
                args.app_delay,
                args.workdir,
            )
            results[label].append(result)
            print(
                f"run {run} {label}: total={seconds(result['total_s'])} "
                f"layout={seconds(result['layout_s'])} "
                f"notify={seconds(result['notify_s'])} "
                f"ok={result['layout_ok']} exit={result['exit_code']}",
                flush=True,
            )
            if not result["layout_ok"]:
                print(f"  output: {result['output']}", flush=True)

    summaries = [summarize(label, results[label]) for label, _ in labels]
    print()
    for summary in summaries:
        print_summary(summary)
    if len(summaries) == 2:
        print_delta(summaries[0], summaries[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
