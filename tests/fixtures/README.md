# Live restore fixtures

`heavy_sway_session.json` is a synthetic version-3 Sway session for manual
restore testing. It holds 8 workspaces, 31 visible windows (26 kitty, 4
zathura, 1 thunar), and 4 hidden scratchpad windows. The layout covers nested
splits, tabbed and stacked containers, floats, a fullscreen window, and marks.

Install it as the live state file, then restore:

```sh
cp tests/fixtures/heavy_sway_session.json \
  "${XDG_STATE_HOME:-$HOME/.local/state}/sway_session.json"
session-manager restore
```

Restore launches real applications, so run it on a compositor you are willing
to fill with windows. It does not close windows that are already open.
