# AGENTS.md

## Repository

`session-manager` saves and restores Sway and i3 desktop sessions. It is a
Python 3.11+ package with a command-line entry point.

- `src/session_manager/` is the implementation.
- `tests/` holds unit tests, Docker roundtrips (`tests/integration/`), and the
  restore benchmark (`tests/bench/`).
- `bin/session-manager` runs from a checkout; `pyproject.toml` installs the
  `session-manager` command.
- `Makefile` is the supported development interface.

Treat the implementation as authoritative. Inspect it directly before relying
on this file.

## Architecture

- `save` reads the live compositor tree, captures window state, and writes a
  versioned `Snapshot` to the backend's state file.
- `restore` parses the snapshot, materializes each saved window, places it by
  container id, applies decorations, and reports what did not come back.
- `docs/restore-design.md` records the four-phase model and its invariants;
  read it before changing restore.
- `model.py` owns the saved-session contract and per-node validation.
- `ipc.py` owns the Sway/i3 wire protocol: a shared request/reply connection
  plus dedicated connections for event subscriptions.
- `sway_tree.py` owns the capture-side tree cleanup: `clean_tree` /
  `clean_floating` decide what a save records.
- `registry.py` owns live-window ownership (`WindowRegistry`): which live
  container id belongs to which saved window.
- `apps/` owns per-application capture and launch behavior.
  `apps/handlers.py` maps each app to its handler; `config.py` holds
  app profiles, launch commands, aliases, and shared timeouts.
- `nvim/` owns Neovim session snapshots and RPC.
- `background.py` owns tray-app restore and daemon health checks.
- `session_env.py` repairs `DISPLAY` / `WAYLAND_DISPLAY` for login-time runs.
- `state.py` holds the mutable per-restore context.

## Working rules

- Write commit subjects as Conventional Commits. Use a scope when it helps.
  Keep the subject to one line; do not add a body.
- Make the smallest change that fixes a real problem or satisfies a concrete
  requirement.
- Verify relevant files and consumers before changing shared code, the state
  format, or an interface.
- Preserve existing structure, naming, and tooling unless there is a concrete
  reason to change them.
- Keep sources of truth authoritative. Do not duplicate state or derived data.
- Do not add indirection, templating, or abstraction when a direct
  implementation is sufficient.
- Do not weaken security or reliability safeguards without an explicit
  requirement and verification of the consequences.
- When a change affects runtime behavior, verify the behavior, not just syntax.
- Do not add blind sleeps. Wait on an observable condition with a deadline.
  Existing sleeps are known debt; do not add more.

## Invariants

- The saved payload is versioned. Bump `model.CURRENT_VERSION` when its shape
  changes and teach `Snapshot.from_payload` to read the previous shape; unknown
  future versions are refused.
- Validation repairs per node. One bad window or workspace is dropped with a
  warning; it must not reject the rest of the session.
- Placement is asserted, never inferred. Every restored window is moved to its
  saved workspace by `con_id`. Do not rely on which workspace is focused when
  the window maps.
- Visibility is asserted. A `hidden_scratchpad` entry is only ever parked with
  `move scratchpad`; it is never `scratchpad show`n, and the park is verified.
  A window saved visible on a workspace with a non-`none` `scratchpad_state` is
  deliberately re-shown (`restore.py:1524-1531`); that path has no test of its
  own.
- A window's identity is matched by pid, app id, or title. `claimed_ids`
  prevents one live window from being assigned to two saved windows.
- `operation_lock` serializes save and restore across processes.
- The shared IPC connection is process-wide and reconnects after a failed
  reply. Subscriptions require their own `WindowEventWatcher`.
- Tests must not touch the user's live compositor. Integration and benchmark
  runs use isolated containers.

## Verification

Run the narrowest relevant checks.

```text
make check
make lint
make format
make test
make integration
make bench
```

`make check` runs lint, format check, type check, and unit tests. `make
integration` and `make bench` require Docker and do not run in CI. The CI
workflow tests Python 3.11 and 3.13. The Make targets fetch pinned `ruff` and
`mypy` through `uv`, so no project virtual environment is required.

## Important constraints

- State files are per backend: `sway_session.json` and `i3_session.json` under
  `XDG_STATE_HOME` (default `~/.local/state`). Do not mix them.
- `SESSION_MANAGER_BACKEND` and `SESSION_MANAGER_SOCK` pin the backend and
  socket. Detection otherwise follows the environment and the compositor
  binary.
- `config.py` computes `BACKEND`, `STATE_FILE`, and related paths at import
  time. Treat that as known debt, not a pattern to extend.
- Sway and i3 differ in geometry coordinates, layout names, and scratchpad
  staging. Keep those differences explicit and covered by the integration
  suite.
- The main state file is authoritative for background apps. The sidecar
  `*_session_background_apps.json` is compatibility output.
- Save records Helium's session files as they are; a window opened immediately
  before a save can be missing from `--restore-last-session`.
- The dropdown terminal is saved but does not count as restorable content;
  transient helper terminals are not saved.
- A full Helium restore uses `--restore-last-session` and matches windows by
  title. A running instance is reused when it already has enough windows. A
  scoped `--workspace` restore opens blank windows because the browser does
  not expose its URLs to the compositor.

## Known debt

`notes/CONTEXT.md` tracks the current correctness rework, its remaining phases,
and the known gaps. It sits in `notes/`, which is local-only and never
committed. Update it when the branch state changes, and delete it once the
rework is merged.
