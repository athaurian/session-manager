# Integration tests

These tests check that a saved session restores correctly. They run against a
real compositor inside Docker, either i3 on X11 or Sway on Wayland.

A **roundtrip** is one save followed by one restore. Each compositor runs the
roundtrip twice. The first run has the compositor's environment variables
(`SWAYSOCK` and `WAYLAND_DISPLAY`, or `I3SOCK` and `DISPLAY`) set. The second
removes them, so `restore` has to find the compositor itself. That is the
fallback login scripts and systemd units rely on.

The tests are not part of `make check` because they need Docker and download
Debian packages on the first run.

## Run both

```sh
make integration
```

## Run one compositor

Choose the compositor with the first argument:

```sh
tests/integration/run.sh i3
tests/integration/run.sh sway
```

## Images

`run.sh` builds `session-manager-i3:test` and `session-manager-sway:test` from
the Dockerfiles here when they are missing. To reuse an existing image, set
`IMAGE_I3` or `IMAGE_SWAY`:

```sh
IMAGE_I3=i3-int:test IMAGE_SWAY=sway-int:test tests/integration/run.sh
```

The containers run with `--network none`, and only the image build needs
network.
