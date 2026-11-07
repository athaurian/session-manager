# Live restore fixtures

`heavy_sway_session.json` is a synthetic version-3 Sway session for manual
restore testing. It covers every application the repo knows how to restore:

| App | Windows |
| --- | --- |
| kitty | 12 (10 visible, 2 hidden) |
| kitty_drop | 1 (hidden) |
| Zathura | 4 |
| Helium | 2 |
| mpv | 1 |
| pavucontrol | 1 |
| blueman-manager | 1 |
| Thunar | 1 |
| Calibre | 1 |
| Calibre e-book viewer | 1 |
| Vesktop | 1 |
| KeePassXC | 1 |

That is 24 visible windows across 9 workspaces plus 3 hidden scratchpad
windows. The layout covers nested splits, tabbed and stacked containers,
floats, a fullscreen window, and marks.

Every path, title, and pid in the fixture is invented. The saved cwd values
are `/home/example`, the saved documents are `book-a.pdf` to `book-e.epub`,
the media file is `/tmp/session-manager-fixture/heavy_media.webm`, and the
saved pid is 0. The fixture therefore describes no real desktop.

`heavy_media.webm` is a 30-minute black clip so mpv stays open instead of
exiting after a short sound file. Regenerate it with:

```sh
ffmpeg -f lavfi -i color=c=black:s=32x18:r=1 -t 1800 \
  -c:v libvpx-vp9 -b:v 1k -an tests/fixtures/heavy_media.webm
```

Copy it to the path the fixture expects before a restore:

```sh
mkdir -p /tmp/session-manager-fixture
cp tests/fixtures/heavy_media.webm /tmp/session-manager-fixture/
```

Install it as the live state file, then restore:

```sh
cp tests/fixtures/heavy_sway_session.json \
  "${XDG_STATE_HOME:-$HOME/.local/state}/sway_session.json"
session-manager restore
```

Restore launches real applications, so run it on a compositor you are willing
to fill with windows. It does not close windows that are already open. The
fixture needs roughly 3-4 GB of RAM; close other heavy apps first if memory is
tight.

KeePassXC is reused when a window titled `Passwords - KeePassXC` is already
live. Every other app is launched fresh unless its saved title matches a live
window.
