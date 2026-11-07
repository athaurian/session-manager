# Restore benchmark

This harness measures how long a session restore takes against a real, headless
Sway compositor. It is separate from `tests/integration`: those tests check that
a roundtrip restores the session correctly, while this benchmark reports
latency. The latency itself is too noisy to gate CI on, so the CI job runs a
single run and gates only on the restore completing cleanly (`ok=True`,
`exit=0`, every layout check green).

The benchmark runs inside the same Docker image as the Sway integration tests.
Fake apps launch `foot` windows with controlled delays, so a run models a slow
browser and a slow generic application without depending on the host's desktop.

## Run

```sh
make bench
```

Pass runner options through `ARGS`:

```sh
make bench ARGS="--runs 10"
make bench ARGS="--scenario mixed --helium-delay 3"
```

The first run builds `session-manager-sway:test` from
`tests/integration/Dockerfile.sway` when the image is missing. Set `IMAGE_SWAY`
to reuse another image.

## Compare two trees

Create a worktree at the older revision, then pass it as the baseline:

```sh
git worktree add --detach /tmp/session-manager-baseline <ref>
make bench ARGS="--baseline /tmp/session-manager-baseline"
```

The runner alternates baseline and current runs, then prints medians for total
restore, time until the final layout appears, and time until the success
notification. It also prints the delta between the two trees.

## What a run models

| Part | Behavior |
| --- | --- |
| Helium | Waits `--helium-delay` seconds, then opens three windows with the saved titles. |
| Slow app | Waits `--app-delay` seconds before opening one window. |
| Fast app | Opens a window immediately. |
| Notification | A fake `notify-send` records the success message timestamp. |

`--scenario split` puts the Helium group on its own workspace and the other apps
on a second workspace. `--scenario mixed` interleaves Helium and fast windows in
one workspace, which checks that the restore order inside a workspace is kept.

## Caveats

- The numbers are relative, not absolute. The fake browser models startup with a
  sleep; it does not measure a real Helium launch.
- Daemon health checks are skipped, so the benchmark measures window restore and
  notification timing rather than `systemctl` probes.
- Container timing still varies with the host. Compare runs from the same
  machine and session.
- Every run checks the final workspace layout, so a timing win that breaks the
  layout is reported as a failure.
