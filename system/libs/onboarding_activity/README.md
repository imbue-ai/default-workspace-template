# onboarding_activity

What this workspace's owner has already done, kept as one-way markers: one empty
file per fact under `data/.state/onboarding_activity/`. The chat's activity
suggestions offer things the owner has not tried yet, and what proves a thing was
tried may not last (a second chat is deleted, a phone is put away), so each fact
is recorded once and never taken back.

## Markers

| Marker | Written by | When |
|---|---|---|
| `tour_finished` | the shell | the tour record is written with `is_finished` true |
| `phone_layout_used` | the shell | the owner's client reports what its phone layout shows |
| `second_chat_started` | the chat app | a person starts a chat while another chat a person talks to exists |
| `permission_request_filed` | the chat app | it reads a filed permission request in a chat it follows |
| `connect_data_coach_shown` | the chat app | the owner clicks the "Connect some data" suggestion |
| `app_build_started` | the build-app skill | its scaffolder runs; every run refreshes the marker's time |

The set is closed: `Marker` is an enum, and a marker's value is its file name.

## Functions

`onboarding_activity.markers` holds everything:

- `mark(marker)` sets a marker, or refreshes its time when it is already set.
- `is_marked(marker)` says whether a marker is set.
- `marked_at(marker)` is when a marker was last set (the file's modification
  time, as a timezone-aware UTC `datetime`), or `None` when it is not set.

Each takes the marker directory as an optional second argument. Left out, it is
`data/.state/onboarding_activity` under the workspace's main checkout, which is
the checkout a git worktree belongs to when the caller runs in one (a worker's
work dir), so every writer and reader shares one directory.
`$ONBOARDING_ACTIVITY_DIR` overrides it.

## When the directory cannot be used

A reader has to tell a marker that is not set from one it could not look at,
because the two call for opposite answers: a suggestion is offered while its
marker is not set, and nothing is offered when that is unknown.

- `is_marked` and `marked_at` answer `False` and `None` only when the marker, or
  its directory, is not there. Any other failure to read raises
  `MarkerUnreadableError`.
- `mark` raises `MarkerNotWrittenError` when the marker cannot be written. A
  writer logs it and carries on: the marker stays unset, so the suggestion it
  would have retired may be offered again.

Both are `OnboardingActivityError`s.

The package is **stdlib-only** and must stay that way: the build-app skill's
scaffolder and both apps import it, and a script that runs without the apps'
dependencies must be able to.
