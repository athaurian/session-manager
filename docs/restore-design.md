# Restore design

This note records the design reasoning behind the restore rework. It comes from
the 2026-11-04 architecture discussion that preceded M0–M5, and it is meant to
outlive the branch-scoped notes in `CONTEXT.md`.

The implementation is authoritative. Read this for the shape and the
invariants, then inspect `src/session_manager/restore.py` for the current
details.

## Why the old loop failed

The problem was not the file layout or the per-app branches. It was the shape
of the restore loop: creation, identity, and arrangement were one sequential
pass.

- `restore` walked the saved tree and called `launch_and_get_id` for each
  window. That call blocked until the app opened a window.
- Placement then depended on the compositor's current focus. A window could map
  after a workspace switch and land in the wrong place.
- Visibility was assumed. A hidden scratchpad window was parked and never
  verified to be off the desktop.
- Timing was load-bearing: Helium's flush sleep, the floating-geometry settle
  delay, title grace periods, and tree-scan intervals existed because the code
  could not observe the condition it cared about.
- Identity was re-derived after the fact. The saved pid is dead after reboot,
  so matching fell back to pid, then app id, then title.
- Adding an app touched several unrelated lists and dispatchers.
- The saved JSON was a rendering rather than a versioned contract.

The consequence: restore was the sum of launch latencies, layout was a side
effect of creation order, and correctness depended on timing.

## The four phases

The alternative was to split restore into four phases with different
concurrency properties:

> Split restore into four phases with different concurrency properties:
>
> - **Materialize** — launch every independent app concurrently, each behind a
>   future keyed by its saved node. No compositor commands here.
> - **Arrange** — once ids resolve, build the tree by explicit criteria/id.
>   This is milliseconds of IPC, so it can stay sequential; only the slow part
>   goes parallel. (`append_layout` is the fully declarative version of this,
>   where supported.)
> - **Decorate** — marks, geometry, fullscreen, scratchpad, all idempotent and
>   addressed by id.
> - **Reconcile** — diff desired vs. live and retry/correct, instead of trusting
>   a timeout. This is what makes restore robust to apps that open late, and it
>   makes `--workspace N` and re-runs natural.

These are phases, not necessarily four top-level functions. In the current
implementation:

- **Materialize** is `_materialize`, with `WindowRegistry` resolving saved nodes
  to live window ids.
- **Arrange** is the `_arrange` walk through `_restore_node`.
- **Decorate** is folded into that walk: `_apply_marks`, `_apply_geometry`,
  `_apply_fullscreen`, `_apply_border`, and `_apply_percent` all address the
  window by id.
- **Reconcile** is `_recover_staged_windows` followed by
  `_reconcile_placements`.

## Invariants

These are the rules the restructure enforces. They matter more than the
function names.

1. **Placement is asserted, never inferred.** Every restored window is moved to
   its saved workspace by `con_id`. No phase may rely on which workspace happens
   to be focused when the window maps.
2. **Visibility is asserted, never inferred.** A hidden scratchpad entry is only
   ever parked with `move scratchpad`, never `scratchpad show`, and the result
   is verified.
3. **Every phase is idempotent.** Re-running arrange, decorate, or reconcile
   converges; it never duplicates or toggles.
4. **Failures are per-window.** One bad node is dropped and reported; it never
   aborts the rest of the session.
5. **No blind sleeps.** Wait on an observable condition with a deadline.
6. **Identity is matched deliberately.** Adoption reuses a live window only
   when its pid, or its app identity plus an exact title, matches the saved
   node. Each live window is claimed once.

## Current flow

1. Adopt saved windows that are already live from an interrupted restore.
2. Switch to a restore-only staging workspace.
3. Materialize every visible window concurrently.
4. Arrange the workspaces in saved order.
5. Restore hidden scratchpad windows just before parking them.
6. Recover windows that mapped after their launch wait.
7. Reconcile workspace placement and scratchpad visibility against the live
   tree.
8. Leave the staging workspace only after it is empty.

## What we deliberately did not do

- Rewrite the codebase. `ipc.py`, `proc_cache.py`, the Neovim subsystem,
  `session_env.py`, and most app modules were independent of the flaw.
- Use `append_layout`. A spike showed that `move to workspace` + `focus` +
  `split` reproduces the nested tree, while `move container to mark` does not.
- Restore workspace-to-output assignment. It is still a gap.
- Inject `SessionPaths` / `Backend` everywhere. `config.py` still freezes paths
  and the backend at import time.

## Open gaps

`CONTEXT.md` tracks the current list while the rework branch is open. The
longer-lived gaps worth remembering are:

- Reconcile repairs workspace placement, not nesting. The container shape is
  still rebuilt by the walk.
- Adoption needs a pid or an exact title match. A relaunched app that renames
  its window is launched again rather than reused.
- Scratchpad groups are flattened, so their internal layout is lost.
- Floating geometry is applied once and not verified against the live rect.
- `background_apps` is written to both the main state file and a sidecar.

## Origin

The discussion started from the prompt "I want to rework this repo from scratch
— I think there's a massive flaw with how it's architected." The answer
reframed that as a rearchitecture of the restore loop rather than a literal
rewrite. The four-phase model above is the original design; M0–M5 implemented
it on `restore-rework`.
