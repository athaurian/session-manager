# session-manager

Hi. This is a tool to save and restore desktop sessions for Sway and i3 window managers.

- `save` writes a snapshot of the current workspaces, windows, and supported
  application state to a JSON file.
- `restore` reads that file, recreates the layout, and asks the applications
  to reopen what they had.

`restore` returns each window to its workspace and position, and reopens
supported applications on the documents and directories they had open.

Personally, I use this in [my setup](https://github.com/athaurian/dotfiles)
where powering off saves the current session, so the next login can bring it
right back.

## Terms

Here are some terms that will be used throughout this page:

| Term            | Meaning                                                                                                                    |
| --------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Login session   | The running Sway or i3 instance that owns the current windows and workspaces.                                              |
| Saved session   | The JSON snapshot written by `save` and read by `restore`. Also called the state file.                                     |
| Compositor      | The program that controls windows and workspaces. Here it means Sway (a Wayland compositor) or i3 (an X11 window manager). |
| Backend         | The supported window manager the tool talks to: `sway` or `i3`.                                                            |
| IPC socket      | The Unix socket a compositor exposes for commands and window-tree queries.                                                 |
| Workspace       | A named group of windows, usually shown on one monitor at a time.                                                          |
| Layout          | How tiled windows are split: horizontal, vertical, tabbed, or stacked.                                                     |
| Floating window | A window that sits above the tiled layout and keeps its own position and size.                                             |
| Scratchpad      | A hidden workspace for parking windows out of sight.                                                                       |
| Mark            | A text label attached to a window, used to jump back to it.                                                                |
| Geometry        | A window's position and size.                                                                                              |
| Background app  | An application that keeps running with no visible window, such as a tray client.                                           |

Below, "session" means a saved session unless the text names a login session,
a systemd session unit, or a Neovim session.

## Requirements

- Python 3.11 or newer.
- A running Sway or i3 login session (`SWAYSOCK` or `I3SOCK`, or a binary that
  supports `--get-socketpath`).
- `notify-send` for desktop notifications.
- `kitty` for terminal restore.
- `xdotool` or `xprop` on i3, because i3's tree omits PIDs while Sway reports
  them.
- Optional application handlers: `nvim`, `zellij`, `zathura`, `thunar`, `mpv`,
  `calibre`, `helium-browser`, and `vesktop`.

## Install

Install the package to get a `session-manager` command on `PATH`:

```sh
uv tool install git+https://github.com/athaurian/session-manager
```

Or install a checkout with `pip install .`, or run the bundled entry point:

```sh
./bin/session-manager save
# ... log out, reboot, or close the windows ...
./bin/session-manager restore
```

`python -m session_manager` also works from a checkout when `src` is
importable.

To check whether there is anything to restore without changing the desktop:

```sh
./bin/session-manager has-session && echo "restorable state found"
```

## Usage

| Command       | Action                                                                                                                                   |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `save`        | Writes the current desktop to the saved session.                                                                                         |
| `restore`     | Recreates the saved session on the running desktop.                                                                                      |
| `has-session` | Exits 0 when the saved session contains anything worth restoring, and 1 otherwise. A login prompt can use this to decide whether to ask. |
| `diff`        | Prints the window counts of the live windows and the saved windows, workspace by workspace.                                              |

| Option          | Applies to        | Effect                                                                                                         |
| --------------- | ----------------- | -------------------------------------------------------------------------------------------------------------- |
| `--quiet`       | `save`, `restore` | Suppresses desktop notifications.                                                                              |
| `--workspace N` | `restore`         | Restores only the workspace whose name matches `N` exactly. The scratchpad and background apps are left alone. |

A filtered restore does not replay the browser's own session, because that
session holds the windows saved on every other workspace. The browser windows
of the selected workspace come back blank instead, because the compositor does
not expose their URLs.

### Exit codes

| Command     | Exit 0                                        | Exit 1                                                                                                                |
| ----------- | --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `save`      | The snapshot was written.                     | The compositor could not be reached, or the state file could not be written.                                          |
| `restore`   | The command ran.                              | Never.                                                                                                                |
| `has-session` | The saved session holds restorable content. | There is nothing to restore, or the state file is missing, unreadable, non-UTF-8, or a version this build cannot read. |
| `diff`      | Both sides were read.                         | The saved session or the live tree could not be read.                                                                 |

`restore` reports what it could not bring back in the log and in a desktop
notification, so it exits 0 after a partial restore and after a refused one.
Run `has-session` first when a script needs a non-zero signal.

### Environment variables

| Variable                        | Effect                                                                                             |
| ------------------------------- | -------------------------------------------------------------------------------------------------- |
| `SESSION_MANAGER_BACKEND`       | Forces the backend to `sway` or `i3`. Wins over every detected signal.                             |
| `SESSION_MANAGER_SOCK`          | Pins the IPC socket path.                                                                          |
| `SWAY_SESSION_IPC_REQUEST_TIMEOUT` | Seconds one IPC request waits for a reply. Default `1.0`. Invalid values fall back to the default. |
| `XDG_STATE_HOME`                | Where the state file lives. Default `~/.local/state`. Read at start-up.                            |
| `SWAY_SESSION_SKIP_DAEMON_CHECKS` | Set to `1` to skip the background daemon health checks. Used by the test suite and isolated runs. |

### State files

- Sway: `~/.local/state/sway_session.json`
- i3: `~/.local/state/i3_session.json`
- Neovim snapshots: `~/.local/state/nvim/`

Set `XDG_STATE_HOME` to keep the state somewhere else. The three previous
saves are kept next to the state file as `.1`, `.2`, and `.3`, where `.1` is
the most recent.

- Background app compatibility output: `sway_session_background_apps.json`,
  `i3_session_background_apps.json`. The main state file is authoritative and
  `restore` reads the background apps from it. The sidecar is written for
  callers that ask for the background list without naming a file.

## Supported window managers

The tool looks for `SWAYSOCK` or `I3SOCK` first, then for the compositor name
in `XDG_CURRENT_DESKTOP`, `DESKTOP_SESSION`, and `XDG_SESSION_DESKTOP`, then
for `WAYLAND_DISPLAY` or `DISPLAY`.

Set `SESSION_MANAGER_BACKEND` to `sway` or `i3` to force a backend, or
`SESSION_MANAGER_SOCK` to pin the IPC socket. An explicit backend value wins
over the detected signals. A compositor binary that supports
`--get-socketpath` can also provide the socket path.

Set `SWAY_SESSION_IPC_REQUEST_TIMEOUT` to change how long a single IPC
request waits for a reply, in seconds (default `1.0`). Non-numeric,
negative, and non-finite values fall back to the default.

## Running restore at login

The `restore` command works from a login script or a systemd unit when the
environment identifies the compositor. A systemd session unit does this through
`XDG_CURRENT_DESKTOP`.

The tool repairs the display variables the restored applications need:

- `DISPLAY`, `XAUTHORITY`, and `I3SOCK` on i3.
- `WAYLAND_DISPLAY`, `XAUTHORITY`, `SWAYSOCK`, and `XDG_RUNTIME_DIR` on Sway.

It takes these from the running compositor when the caller does not have them.
A caller with no environment at all, such as plain cron, is supported: the
compositor is found by process name and its sockets are read from the system.
A value the caller already has is kept, and a `SWAYSOCK` that disagrees with
the compositor is reported. On Sway a stale `WAYLAND_DISPLAY` is kept without
a warning, because that variable is only filled when it is missing.

## Restorables

- Workspaces, layouts, floating windows, the scratchpad, marks, geometry,
  fullscreen state, and the floating border style.
- Terminals with their working directories, including Neovim sessions inside
  them.
- Open documents, down to the page of the PDF you were reading in zathura.
- The browser. A full restore relaunches Helium with `--restore-last-session`,
  reuses a running instance when it has enough windows, and matches windows
  back to the saved ones by title. A scoped `--workspace` restore opens blank
  windows instead, because the browser does not expose its URLs.
- Background apps that live in the tray with no visible window, such as a
  minimized Vesktop.

### Non-restorables

- The dropdown terminal (a hidden terminal summoned by a keybinding) is saved,
  but does not count as restorable content, because that keybinding spawns it
  whenever it is missing.
- Transient helper terminals (short-lived terminals opened by scripts) are
  intentionally not saved.

## Known limitations

- An application that replaces its own window later than one settle time
  after the restore bound it is reported as missing. A later restore
  converges.
- An application that moves its own window after the restore is put back by
  the next restore. Nothing watches for the move in the background.
- Helium flushes its own session when its window closes, so a window opened
  right before a save can be missing from `--restore-last-session`.
- A scoped `--workspace` restore opens blank Helium windows, because the
  browser does not expose its URLs to the compositor.
- A scratchpad group is saved as separate windows. The group's layout is
  lost, but no window is dropped.
- i3 geometry is covered only by the container roundtrip.
- A terminal whose Neovim has no IPC socket saves `nvim_type: unknown`
  instead of a session, and the terminal still comes back.
- The state paths are resolved at start-up, so `XDG_STATE_HOME` must be set
  before the process starts.
- On Sway a stale `WAYLAND_DISPLAY` is kept without a warning. Only a
  `SWAYSOCK` disagreement is reported.
- `restore` exits 0 even when it restores nothing.

## Troubleshooting

If the log says `Cannot reach the window manager`, no IPC socket was
found. Run the command inside a Sway or i3 login session, or set
`SESSION_MANAGER_SOCK`.

If the log says `Invalid session file`, the state file is unreadable
or a later version. The log names the reason, and `has-session`
exits 1 with a `not restorable` warning.

If a window did not come back, the log lists it under `Windows not
restored`. The usual causes are a missing application or a window
that carried no launch command. One damaged window never stops the
rest of the session. The log shows the repair as
`Session payload issue: ...`.

If two saves or restores run at once, one waits for the other. The
lock is a file lock on an open descriptor, so a process that was
killed leaves no stale lock behind.

If nothing is restorable, check `has-session`. A saved workspace
with no windows counts as content, but a state file that holds only
a `_session_restore` staging workspace or only the dropdown terminal
does not.

## Development

The restore design and its invariants are documented in
[`docs/restore-design.md`](docs/restore-design.md).



The `Makefile` provides these targets:

| Target             | Action                                                                                    |
| ------------------ | ----------------------------------------------------------------------------------------- |
| `make check`       | Runs the linter, formatter check, type check, and unit tests. Stops at the first problem. |
| `make lint`        | Runs the linter, formatter check, and type check without modifying files.                 |
| `make format`      | Applies ruff fixes and formatting.                                                        |
| `make test`        | Runs the unit tests.                                                                      |
| `make integration` | Runs save and restore roundtrips in i3 and Sway containers. Requires Docker.              |
| `make bench`       | Benchmarks restore latency in a headless Sway container. Requires Docker.                 |

`make check`, `make lint`, `make format`, and `make test` use `uv` to pull
pinned versions of `ruff` and `mypy` on demand, so they work without a local
virtual environment.

To run the unit tests without `make`:

```sh
PYTHONPATH=src:. python3 -m unittest discover -s tests -t .
```

`make integration` builds Debian containers and runs save and restore
roundtrip tests in both window managers:

- i3 under Xvfb with xterm.
- Headless Sway with foot.

See [`tests/integration/README.md`](tests/integration/README.md) for what each
roundtrip checks.

`make bench` builds or reuses the Sway integration image and reports restore
latency. See [`tests/bench/README.md`](tests/bench/README.md) for the scenarios
and how to compare two trees.

## License

Released under the MIT license. See [LICENSE](LICENSE) for the full text.
