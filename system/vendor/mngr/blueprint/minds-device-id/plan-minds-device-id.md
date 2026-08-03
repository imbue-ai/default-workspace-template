# Plan: minds-owned device id

## Refined prompt

> I would like to change the "device id" that we use within minds
>
> The core problem is that right now in Minds we're using the device id from the *local provider* in mngr, which makes no sense (because minds doesn't use the local provider for literally anything else, so we really would like to *disable* that provider, and this is the only thing that prevents that I think?)
>
> We *should* be able to do this by simply having a different piece of data, at the level of minds, that we use for the device id
> Backwards compatibility should be fairly straightforward (use the existing one if it exists, otherwise make one)
>
> * Store the new device id at `<minds data_dir>/device_id`, next to `anonymous_user_id`, with a minds-local atomic read-or-create function (leave the `imbue_common` anonymous-user-id helper untouched)
> * Migration: when the minds-level file is absent but `<mngr_host_dir>/host_id` exists, copy that value into the new file and leave the original untouched
> * Device ids remain `HostId`s (a user's machine *is* a host): adopted legacy values already validate, and fresh installs mint a new `HostId` — uniform shape, trivial migration
> * Create the device id eagerly at `minds run` startup and tighten the code/types so an empty device id is impossible (remove the empty-id guards and the `hosting_device_id=""` provenance bug)
> * If the device id file cannot be read or created at startup — or the legacy `host_id` value is not a valid `HostId` — crash `minds run` with a clear error rather than running without identity
> * Do not write the minds device id back to mngr's `host_id` file; divergence between the two on fresh installs is acceptable
> * Bug-era server records with `hosting_device_id=""`: rely on the reconcile's existing metadata refresh to self-heal rows whose workspace is still present, and the manual "remove from list" action for gone-host rows — no new repair code
> * Per-env identity: each minds env (`~/.minds`, `~/.minds-<env>`) keeps its own device id file, matching today's per-env behavior
> * Testing: unit tests only (read-or-create, legacy adoption, crash paths), plus updating existing tests that construct stores with fake device ids
>
> We do NOT need to worry about disabling the local provider right now (we'll do that later, once we're sure that every user of minds has migrated their device id correctly)

## Overview

- Minds currently freeloads its device identity off mngr's local provider: `read_device_id` reads `<MNGR_HOST_DIR>/host_id`, a file only `LocalProviderInstance` creates (lazily, during discovery). This blocks ever disabling the local provider and causes the fresh-install bugs in issue #2538 (empty `device_id` for the whole first session, `hosting_device_id=""` provenance, skipped tombstoning).
- Minds will own its device id in its own file: `<minds data_dir>/device_id` (e.g. `~/.minds/device_id`), next to `anonymous_user_id`, created atomically at `minds run` startup before the `WorkspaceRecordStore` is constructed.
- Backwards compatibility: on first run with the new code, if `<mngr_host_dir>/host_id` exists, its value is copied into the new file (the original is left untouched for the local provider), so existing synced records keep matching this install. Otherwise a fresh id is minted.
- The id stays `HostId`-shaped (a user's machine *is* a host): adopted legacy values are already valid `HostId`s and fresh installs mint a new one, so values are uniform and the field can be strictly typed.
- Identity becomes guaranteed: the empty-device-id code paths (warning, skipped tombstoning guards) are removed, and any failure to read/create/validate the id at startup crashes `minds run` with a clear error naming the offending file.

## Expected behavior

- Existing installs (upgrade): the first `minds run` with the new code copies the value from `<mngr_host_dir>/host_id` into `<data_dir>/device_id`. The install's device id is unchanged, so all previously synced records (`hosting_device_id`) still attribute to it; tombstoning, resurrection, and greyed-out "on \<device\>" tiles behave exactly as before.
- Fresh installs: `minds run` mints a `HostId` and persists it before any sync machinery starts. The first session now has a real device id from the beginning — no "Skipping absent-host tombstoning: this install has no device id" warning, no deferred tombstoning, and no records pushed with `hosting_device_id=""`.
- Later runs: the file exists and is read back; the id is stable for the lifetime of the install (per minds env — each of `~/.minds`, `~/.minds-<env>` keeps its own id, matching today's per-env `host_id`).
- The mngr local provider's `host_id` file is no longer read by minds and is never written by minds. On fresh installs the two ids diverge; this is harmless because the device id is only ever compared against records minds itself wrote. Nothing else about the local provider changes in this task (disabling it is future work).
- Failure behavior: if the device id file cannot be read or created, or an existing device id / legacy `host_id` value is not a valid `HostId`, `minds run` exits with a clear error that names the file — it never runs without identity. (Corrupt-file recovery is: the user deletes the named file.)
- Bug-era records already on the server with `hosting_device_id=""`: rows whose workspace is still present locally self-heal via the reconcile's existing metadata refresh (the rebuilt record now carries the real id, diffs, and is pushed). Rows whose host is already gone remain as greyed-out tiles until removed via the existing manual "remove from list" action. No new repair code.
- Two concurrent processes racing on first creation converge on a single id (same `O_EXCL` create-then-read-loser semantics as the anonymous-user-id file).

## Changes

- Add a minds-local "read or create device id" function (in `apps/minds/imbue/minds/desktop_client/`, near or replacing `read_device_id` in `workspace_record_store.py`): read `<data_dir>/device_id` if present; else adopt `<mngr_host_dir>/host_id` if present (copy, don't move); else mint `HostId.generate()`; persist atomically with `O_EXCL` first-writer-wins semantics modeled on (but not sharing code with) `get_or_create_anonymous_user_id`. Returns a validated `HostId`; raises a minds error on unreadable/uncreatable files or non-`HostId` contents.
- Call it eagerly in `minds run` startup (`apps/minds/imbue/minds/cli/run.py`), replacing the `read_device_id(mngr_host_dir)` call, and pass the result into `WorkspaceRecordStore`. A raised error propagates and aborts startup.
- Delete `read_device_id` and its empty-string contract from `workspace_record_store.py`.
- Tighten `WorkspaceRecordStore.device_id` from `str` to `HostId` and remove the now-impossible empty-id guards: the `if not self.device_id` early-return in `_resurrect_locally_hosted_tombstones` and the warn-and-skip branch in `_tombstone_definitively_absent`. `ReplicaRecord.hosting_device_id` stays `str | None` (opaque wire value; legacy rows may hold anything).
- Update the field docstrings/descriptions that say the device id is "this install's mngr host_id" (`WorkspaceRecordStore.device_id`, `read_device_id` callers) to describe the minds-owned file.
- Update `specs/workspace-sync/spec.md` where it documents `device_id` as "minds env's mngr `host_id`".
- Update tests that construct stores or records with fake device ids (e.g. `device_id="device-test"` in `apps/minds/imbue/minds/desktop_client/conftest.py`) to valid `HostId` values, and any tests that pre-seed `<mngr_host_dir>/host_id` to exercise the old lazy-read behavior.
- Add unit tests (in a `_test.py` next to the new function's module) for: create-fresh, read-existing, legacy adoption (file copied, original untouched), precedence (minds file wins over legacy file), crash on unreadable file, crash on invalid contents (both the minds file and the legacy file), and first-creation race convergence.
- Add a changelog entry at `apps/minds/changelog/mngr-better-device-id.md`.
- Out of scope: disabling the local provider, any server/connector change (`hosting_device_id` remains an opaque string on the wire), any repair of gone-host bug-era rows, writing the minds device id back to mngr's `host_id` file.
