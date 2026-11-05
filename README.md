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
| `diff`        | Prints a plain-English comparison of the live windows and the saved windows, workspace by workspace.                                     |

| Option          | Applies to        | Effect                                                                                                         |
| --------------- | ----------------- | -------------------------------------------------------------------------------------------------------------- |
| `--quiet`       | `save`, `restore` | Suppresses desktop notifications.                                                                              |
| `--workspace N` | `restore`         | Restores only the workspace whose name matches `N` exactly. The scratchpad and background apps are left alone. |

A filtered restore does not replay the browser's own session, because that
session holds the windows saved on every other workspace. The browser windows
of the selected workspace come back blank instead, because the compositor does
not expose their URLs.

### State files

- Sway: `~/.local/state/sway_session.json`
- i3: `~/.local/state/i3_session.json`
- Neovim snapshots: `~/.local/state/nvim/`

Set `XDG_STATE_HOME` to keep the state somewhere else. The three previous
saves are kept next to the state file as `.1`, `.2`, and `.3`, where `.1` is
the most recent.

## Supported window managers

The tool looks for `SWAYSOCK` or `I3SOCK` first, then for the compositor name
in `XDG_CURRENT_DESKTOP`, `DESKTOP_SESSION`, and `XDG_SESSION_DESKTOP`, then
for `WAYLAND_DISPLAY` or `DISPLAY`.

Set `SESSION_MANAGER_BACKEND` to `sway` or `i3` to force a backend, or
`SESSION_MANAGER_SOCK` to pin the IPC socket. An explicit backend value wins
over the detected signals. A compositor binary that supports
`--get-socketpath` can also provide the socket path.

## Running restore at login

The `restore` command works from a login script or a systemd unit when the
environment identifies the compositor. A systemd session unit does this through
`XDG_CURRENT_DESKTOP`.

The tool repairs the display variables the restored applications need:

- `DISPLAY` on i3.
- `WAYLAND_DISPLAY` on Sway.

It takes these from the running compositor when the caller does not have them.
A caller with no environment at all, such as plain cron, cannot tell Sway from
i3 and is not supported.

## Restorables

- Workspaces, layouts, floating windows, the scratchpad, marks, geometry,
  fullscreen state, and the floating border style.
- Terminals with their working directories, including Neovim sessions inside
  them.
- Open documents, down to the page of the PDF you were reading in zathura.
- The browser, relaunched with Helium's `--restore-last-session` flag. A
  running instance is reused, missing windows are added, and the windows are
  matched back to the saved ones.
- Background apps that live in the tray with no visible window, such as a
  minimized Vesktop.

### Non-restorables

- The dropdown terminal (a hidden terminal summoned by a keybinding) is not
  counted as saved content, because that keybinding spawns it whenever it is
  missing.
- Transient helper terminals (short-lived terminals opened by scripts) are
  intentionally not saved.

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
