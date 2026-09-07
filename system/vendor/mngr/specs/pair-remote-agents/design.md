# `mngr pair` for remote agents -- design sketch

Status: **implemented**. Kept as the record of why the design is shaped this way;
the sections below have been reconciled with what actually shipped, and divergences
from the original sketch are called out inline.

Decisions taken (2026-08-19):
- Sync engine stays **unison**; mutagen is not pursued (see section 6).
- Remote install is **silent** -- no consent prompt.
- The default agent images carry the *distro* unison, which has no
  `unison-fsmonitor` and so does not count as usable (see section 1), which
  makes the install rung the common path on a host mngr has not paired with
  before.
- Linux arm64 gets **no install attempt at all**: probe, and if nothing
  compatible is there, fail with a clear message.

`mngr pair` used to hard-stop with
`NotImplementedError("Pairing with remote agents is not implemented yet")` for any
non-local host. This document explains what had to happen to remove that, and now
describes shipped behavior.

## 1. The premise, corrected

The task framing was "make sure the remote host has a version-sync'ed binary".
That was the rule for unison up to 2.51. It is no longer true.

From the unison manual shipped with the local install (`unison -doc all`,
section "Version interoperability"):

> Unison 2.52 and newer are compatible with: Unison 2.52 or newer (for as long
> as backwards compatibility is maintained in the newer versions). You do not
> have to pay any attention to OCaml compiler versions.

| Client            | Server > 2.53.8 | Server 2.52-2.53.8 | Server < 2.52 |
|-------------------|-----------------|--------------------|---------------|
| newer than 2.53.8 | full interop    | full interop       | **no interop** |
| 2.52 to 2.53.8    | full interop    | full interop       | same OCaml only |
| older than 2.52   | **no interop**  | same OCaml only    | exact match    |

So the invariant we must enforce is **both sides >= 2.52**, not "same version".
That is far cheaper to satisfy and means an already-present remote unison is
usually reusable as-is.

Version is not the only thing to check, though, and two things still force us to
manage a binary:

1. **Distro unison is a trap, twice over.** Ubuntu 22.04 ships 2.51.5, which has
   *no* interop with a modern client. And no Debian or Ubuntu unison package
   ships a `unison-fsmonitor` -- bookworm's `unison` contains exactly
   `/usr/bin/unison`, and no package in the archive provides that path -- so even
   bookworm's perfectly modern 2.53.3 syncs once under `-repeat watch` and then
   dies with `Server: No file monitoring helper program found`. That is what
   `libs/mngr/imbue/mngr/resources/Dockerfile:25` apt-installs today, so mngr's
   own agent image is precisely this case. `apt-get install unison` is therefore
   not a safe blind fallback on any distro.
2. **Upstream has no Linux arm64 build.** Checked every release back to
   v2.53.2: the Linux assets are `ubuntu-*-x86_64`, `x86_64-static`, and
   `i386`. There has never been an aarch64 asset. This is the hard constraint
   that shapes the fallback ladder below.

The x86_64 static tarball is 2.1 MB and contains exactly what we need:

```
unison-2.54.0-ubuntu-22.04-x86_64-static/bin/unison            (3.1 MB)
unison-2.54.0-ubuntu-22.04-x86_64-static/bin/unison-fsmonitor  (1.1 MB)
```

Fully static, so it runs on any glibc/musl Linux without sudo.

## 2. Local or download? Both, asymmetrically

**Local: require, do not download.** The local unison *is* the client; there is
no configuration in which we can avoid having it. Keep the existing
`SystemDependency` + brew/apt path (`libs/mngr_pair/imbue/mngr_pair/api.py:196`),
but add a **version floor** check (>= 2.52) that does not exist today --
`SystemDependency` has no version concept at all, it is purely `shutil.which`
(`libs/mngr/imbue/mngr/utils/deps.py:66`).

mngr *could* download a local unison too (macOS arm64 and x86_64 assets both
exist), but that buys nothing: the user already has to install unison for
local pair to work, and a mngr-managed binary shadowing a brew one is a new
class of confusion.

**Remote: probe first, download only when needed.** Never install blindly.

## 3. Remote resolution ladder

`ensure_remote_unison(host) -> Path` returns the path to use as `-servercmd`.

A candidate is **usable** when it is >= 2.52 *and* a `unison-fsmonitor` sits beside
it or on the host's PATH -- the two places unison looks. Both halves are checked at
every rung, because `-repeat watch` needs a watcher for the far replica too and the
server runs that one (section 1, item 1).

1. **Probe PATH.** `unison -version` on the host, parse
   `unison version X.Y.Z (ocaml A.B.C)`, and look for the watcher. Free when it
   succeeds, and the only rung that ever succeeds on a host somebody has set up by
   hand. It does *not* cover images built from
   `libs/mngr/imbue/mngr/resources/Dockerfile`: their apt-installed unison has no
   watcher.
2. **Probe the mngr-managed copy.** `~/.mngr/bin/unison`, checked the same way.
   This is the rung that makes later pairings with the same host free, since the
   install below lays a watcher down next to the binary.

   *Shipped differently from the sketch:* there is no version-stamp file. Running
   `-version` on the candidate is cheap here and strictly more truthful than a
   stamp, because it also catches a binary that is present but broken.
3. **Install.** If `uname -s`/`uname -m` is `Linux`/`x86_64` (or `amd64`),
   download the pinned static tarball from GitHub releases, sha256-verify, extract
   `bin/unison` and `bin/unison-fsmonitor` into `~/.mngr/bin/`, then run the
   installed binary and report the version *it* prints. Installing the pair
   together is the point: it is the only route to a watcher on a distro that
   packages none.

   *Shipped differently from the sketch:* the sketch said `i686` too, which was
   wrong -- the pinned asset is `x86_64-static` and will not run on `i686`. The
   sketch also gated on the architecture alone; the asset is a Linux ELF, so the
   OS has to be checked too or an x86_64 macOS host installs a binary it cannot
   exec. And the install is validated by running it, for the same reason the
   version-stamp file was dropped (see step 2).
4. **Anything else (notably Linux arm64, and anything not running Linux).** No
   install attempt. Fail with an error naming the host platform, the version
   floor, the watcher requirement, and the fact that mngr only installs the Linux
   x86_64 build -- the user can install both binaries themselves and step 1 will
   then pick them up. We do not
   try apt here: on the one distro where it would help it is a coin flip
   (jammy = 2.51.5 = unusable), and a silent `apt-get install` that mutates
   system state is a worse failure mode than a clear error.

Design notes:

- Install to `~/.mngr/bin`, not `/usr/local/bin`. No sudo needed, no clash with
  a distro unison, and `-servercmd` makes PATH irrelevant anyway. (This is a
  deliberate departure from `libs/mngr_ttyd/imbue/mngr_ttyd/plugin.py:122`,
  which needs sudo precisely because it targets `/usr/local/bin`.)
- Pin as constants next to the plugin, same shape as `RESTIC_VERSION` in
  `libs/mngr/imbue/mngr/resources/Dockerfile:47`:
  `UNISON_REMOTE_VERSION = "2.54.0"` plus a per-asset sha256 map.

  *Shipped differently from the sketch:* `REMOTE_UNISON_VERSION` and a single
  `REMOTE_UNISON_SHA256`, because there is only ever one asset to pin -- step 3
  installs on exactly one platform.
- The whole ladder is **one** `host.execute_idempotent_command` running a
  single POSIX-sh script, per the "batch remote operations" rule in CLAUDE.md.
- Model it on `libs/mngr_latchkey/imbue/mngr_latchkey/owner_exec_vm.py:59-88`
  (version-stamp early exit, `curl --retry`, `sha256sum -c`, `install -m 0755`
  to `.new`, atomic `mv -f`, stamp written last). That is the best of the four
  existing remote-install implementations in the repo; the ttyd one is
  presence-only and so never upgrades an existing host. Everything but the stamp
  carries over -- see step 2 for why the stamp went.

## 4. Wiring unison to a remote root

### 4.1 What `_build_unison_command` looks like today

`libs/mngr_pair/imbue/mngr_pair/api.py:88` builds a flat argv from two plain
`Path` fields (`source_path`/`target_path`, `api.py:61-62`). A path is
stringified in **six** places, and every one of them is a *root reference*
that unison resolves against the two roots it was given:

| Site | Arg |
|---|---|
| `api.py:92-93` | the two positional roots |
| `api.py:110` | `-prefer <source>` (ConflictMode.SOURCE) |
| `api.py:112` | `-prefer <target>` (ConflictMode.TARGET) |
| `api.py:124` | `-force <source>` (SyncDirection.FORWARD) |
| `api.py:126` | `-force <target>` (SyncDirection.REVERSE) |

All six must move to a root abstraction together. Missing one is not a subtle
bug -- verified locally with unison 2.54.0:

```
$ unison /tmp/a /tmp/b -auto -batch -prefer /nonexistent/a
Error: Argument to preference 'prefer': /nonexistent/a
is not uniquely identifying one of the current roots:
  /tmp/a
  /tmp/b
```

It is a hard error, which is the good outcome. Note the wording: unison does
**substring** matching, not exact matching, and requires the match to be
unique. `-prefer scratchpad/a` against root `/long/path/scratchpad/a` works.
That is a trap in the other direction -- a bare `/home/me/work` could
accidentally uniquely-identify the root `ssh://root@h//home/me/work` and
appear to work, so we should not lean on it. Render the full root string.

### 4.2 The root abstraction

```python
class SshEndpoint(FrozenModel):
    """Everything needed to reach one host over SSH, from get_ssh_connection_info()."""
    user: str
    hostname: str
    port: int
    key_path: Path
    known_hosts_path: Path | None


class UnisonRoot(FrozenModel):
    """One side of a unison sync -- a local path, or an ssh:// root on a host."""
    path: Path
    ssh: SshEndpoint | None

    def as_root_arg(self) -> str:
        if self.ssh is None:
            return str(self.path)
        # The doubled slash is unison's absolute-path marker: a single slash
        # after the hostname means "relative to the remote home directory".
        return f"ssh://{self.ssh.user}@{self.ssh.hostname}/{self.path}"
```

`UnisonSyncer` swaps its two `Path` fields for two `UnisonRoot`s, and
`_build_unison_command` calls `as_root_arg()` at all six sites. This part is
pure and fully unit-testable with no host.

Note there is **no port** in the root syntax. Unison's `ssh://` URI accepts
`user@host` and a path, nothing else -- the port has to travel with the SSH
transport, which leads to the next part.

### 4.3 SSH transport: use `-sshcmd`, not `-sshargs`

The obvious move is to reuse `build_ssh_transport_command`
(`libs/mngr/imbue/mngr/hosts/common.py:58`) -- the helper that already
carries mngr's key, port, `StrictHostKeyChecking`, and the
`IdentitiesOnly=yes` / `IdentityAgent=none` pair that keeps a `BatchMode`
child from hanging forever on the macOS 1Password agent -- and hand it to
unison as `-sshargs`.

**That does not work.** Verified by pointing `-sshcmd` at a script that dumps
its argv:

```
$ unison /tmp/a ssh://someuser@somehost//remote/path \
    -sshcmd ./fakessh.sh \
    -sshargs "-i '/path with space/key' -p 2222 -o IdentitiesOnly=yes" \
    -servercmd /root/.mngr/bin/unison

# argv the fake ssh actually received, one per line:
[-l] [someuser] [somehost] [-e] [none]
[-i] ['/path] [with] [space/key']      <-- three arguments
[-p] [2222] [-o] [IdentitiesOnly=yes]
[/root/.mngr/bin/unison] [-server] [__new-rpc-mode]
```

Two findings:

1. **`-sshargs` is split on bare whitespace with no shell-quote processing.**
   unison `exec`s ssh directly; no shell is involved. `build_ssh_transport_command`
   `shlex.quote()`s the key path (`common.py:80`), so its output is not merely
   unhelpful here, it is actively wrong: the quotes arrive as literal
   characters and a path containing a space is torn into several arguments.
   mngr key paths live under the user's home, so any user whose home directory
   contains a space would get a broken remote pair with a confusing error.
   There is no escaping that fixes this -- the interface cannot represent a
   space.

2. unison lays the argv out as
   `-l <user> <host> -e none <sshargs...> <servercmd> -server __new-rpc-mode`,
   i.e. **the options come after the destination**. That looks broken, because
   `ssh [options] destination [command]` implies everything after the host is
   the remote command. It is fine: OpenSSH re-enters its option loop after
   consuming the hostname (the `goto again` in `ssh.c`). Confirmed:
   `ssh -v -l nobody 127.0.0.1 -p 2222 true` reports
   `Connecting to 127.0.0.1 [127.0.0.1] port 2222`. Worth a comment in the
   code so nobody "fixes" the ordering later.

So: **generate a small `ssh` wrapper script and pass it as `-sshcmd`, with no
`-sshargs` at all.** The wrapper gets `build_ssh_transport_command`'s output as
its body (quoting intact, because a shell now interprets it), and ends in
`exec ... "$@"` so unison's own `-l user host -e none ...` arguments are appended:

```sh
#!/bin/sh
exec ssh -i '/Users/Jane Doe/.mngr/keys/agent' -p 2222 \
    -o IdentitiesOnly=yes -o IdentityAgent=none \
    -o UserKnownHostsFile='"/Users/Jane Doe/.mngr/known_hosts"' \
    -o StrictHostKeyChecking=yes "$@"
```

This keeps a single source of truth for mngr's SSH options -- the same helper
rsync and git already use -- and it is immune to whitespace in every path.

*Shipped differently from the sketch:* the wrapper goes into a
`tempfile.TemporaryDirectory` that lives exactly as long as the sync, not into
the agent state dir. The sketch's own section 8 established that `host_dir` is a
path *on the host*, so it could not have held a script the local unison must
exec; a per-session temp dir also has no staleness or cleanup concerns.

### 4.4 The remaining unison flags

- `-servercmd <path from section 3>` -- pins which unison runs on the far
  side, so a stale distro unison on `PATH` can never be picked up by accident.
- ~~`UNISON=~/.mngr/unison` on the server side~~ -- **deliberately dropped.**
  unison names archives by a hash of the two roots, so mngr's cannot collide with
  a user's own, and relocating them would mean wrapping `-servercmd` in a shell
  purely to set an environment variable. Recorded in `remote.py` next to the
  script builder.
- `-repeat watch` needs a file watcher on **both** ends, and each end runs its
  own: the far replica's watcher is a `unison-fsmonitor` that the *server*
  process starts, found beside its own binary or on the host's PATH. unison has
  no built-in watcher on any platform, Linux included, which is why the ladder
  in section 3 checks for the helper rather than assuming inotify. The static
  tarball ships it beside the binary, so the install rung covers it. The
  existing macOS-only *local* `unison-fsmonitor` requirement (`api.py:202`,
  `:210`) understates the local need for the same reason, but changing it would
  change local-only pairing behavior, so it is left for a follow-up.

## 5. The git-sync half

### 5.1 What already works

Half of this is a non-problem. `_pull_agent_into_local` (`api.py:317`) and
`_push_local_to_agent` (`api.py:355`) delegate to `git_pull` / `git_push`
(`libs/mngr/imbue/mngr/api/git.py:571`, `:535`), and those already take a
`remote_host: OnlineHostInterface` and branch on it internally:
`_build_git_url_and_env` (`git.py:433`) returns a bare path for a local host
and `ssh://user@host:port/<path>/.git` plus a `GIT_SSH_COMMAND` env for a
remote one. `stash_guard` is likewise already host-agnostic through
`GitContextInterface` / `RemoteGitContext` (`git.py:215`, `:300`).

So the **transfer** side of git sync is remote-ready today. Only the
**detection** side is not.

### 5.2 The one thing that is genuinely broken

`determine_git_sync_actions` (`api.py:215`) answers "who is ahead, the agent
or me?". Every step runs a local subprocess against `agent_path`:

```python
if not is_git_repository(agent_path, cg) or not is_git_repository(local_path, cg):
agent_branch = get_current_branch(agent_path, cg)
agent_commit = get_head_commit(agent_path, cg)
...
cg.run_process_to_completion(
    ["git", "fetch", str(local_path), local_branch],   # <-- the problem
    cwd=agent_path,
)
agent_ahead = is_ancestor(agent_path, local_commit, agent_commit, cg)
local_ahead = is_ancestor(agent_path, agent_commit, local_commit, cg)
```

The reads are merely misdirected -- `RemoteGitContext` already has
`is_git_repository` and `get_current_branch`, and `get_head_commit` is one
`git rev-parse HEAD` through `host.execute_idempotent_command`.

The **fetch** is the part that cannot be patched in place. Ancestry
comparison needs both sides' commit objects in one repository, and this line
gets them there by fetching *the local repo* *into the agent's repo*, using
`str(local_path)` as a git remote URL. That URL is a local filesystem path.
Run on a remote host it either does not exist (fetch fails, and the code
falls into the `except ProcessError` at `api.py:257` that reports "neither
side is ahead" -- so pair would silently skip git sync entirely) or, worse,
it *does* exist on the remote and is a completely different repository.

### 5.3 The fix: invert the fetch

Do everything in the local repo, and bring the agent's objects to it instead:

1. Read the agent's branch and HEAD via `RemoteGitContext` /
   `execute_idempotent_command` (one round trip if the two reads are
   combined).
2. `git -C <local_path> fetch <agent-url> <agent_branch>` -- where
   `<agent-url>` is exactly what `_build_git_url_and_env` already produces for
   `git_pull`. This needs a public `git_fetch(local_path, remote_host,
   remote_path, extra_args, cg)` in `libs/mngr/imbue/mngr/api/git.py`,
   mirroring `git_pull` (`git.py:571`) line for line: it reuses
   `add_safe_directory_on_remote`, `_build_git_url_and_env`,
   `_split_options_and_positionals`, and `_run_git_command`. Small, and
   independently useful.
3. Run both `is_ancestor` calls against `local_path`, which now holds both
   sets of objects.

The step-2 URL is the same one `git_pull` uses moments later in
`_pull_agent_into_local`, so if the fetch succeeds the pull will too -- the
failure modes collapse into one.

This is a strict improvement for local agents as well. The current docstring
(`api.py:222`) concedes the fetch is "a read-only side effect on agent's
repo": mngr writes objects into a repository the *agent* owns and is actively
working in. After the inversion the write lands in the user's own repo as
`FETCH_HEAD`, which is the side the user controls and can clean up.

### 5.4 The smaller local-path assumptions

| Site | Problem | Fix |
|---|---|---|
| `api.py:428` `agent_path.is_dir()` | stats the local filesystem | `_dir_exists(host, path)` already exists at `libs/mngr/imbue/mngr/api/rsync.py:82`; lift it to a shared helper |
| `api.py:432` same-directory guard | compares `resolve()`d local paths, meaningless across hosts | compare `(host.id, resolved path)`; for a remote agent it can only ever trip if the host is local |
| `api.py:445-446` `is_git_repository` x2 | local git | thread a `GitContextInterface` through instead of a bare `cg` |
| `cli.py:203` | the `NotImplementedError` | delete |

## 6. The alternative worth five minutes: mutagen

`mngr pair` is re-solving the problem [mutagen](https://github.com/mutagen-io/mutagen)
was built for. mutagen ships one local binary carrying an agent bundle for
every platform, copies the correct agent to the remote over SSH automatically,
and version-matches it -- no pinning, no checksum table, no arch ladder. Its
v0.18.1 release has `mutagen_linux_arm64`, which unison does not and never has.
It also does bidirectional sync with conflict modes and ignore patterns
natively.

Costs: swapping the sync engine; `--conflict newer` has no direct analogue
(mutagen's modes are two-way-safe / two-way-resolved / one-way-safe /
one-way-replica); and the project's last release was 2025-02-24, so it is
maintained but not fast-moving.

Recommendation: ship the unison ladder now -- it is a contained diff and
preserves existing `mngr pair` semantics exactly. Revisit mutagen if arm64
remote hosts become common, because that is the case the ladder handles worst.

## 7. Phasing

1. `SshEndpoint` + `UnisonRoot` + remote-aware `_build_unison_command` + the
   `-sshcmd` wrapper generator. All pure and unit-testable, no host required.
2. Remote unison version parsing + `ensure_remote_unison` ladder + pinned
   version/sha256 constants.
3. `git_fetch` in `libs/mngr/imbue/mngr/api/git.py`, then invert
   `determine_git_sync_actions` onto it and make it host-aware.
4. The smaller local-path assumptions in `pair_files` (table in 5.4), and drop
   the `NotImplementedError`.
5. Bake unison into the default-workspace-template image, matching
   `libs/mngr/imbue/mngr/resources/Dockerfile:25`.

   *Not shipped.* Once the watcher requirement landed, matching `Dockerfile:25`
   stopped being useful: `apt-get install unison` supplies no watcher, so the
   image would still fall through to the install rung. Baking the static build in
   instead means an arch-conditional Dockerfile, which is a separate change; the
   ladder handles the image correctly in the meantime.
6. Docs: `libs/mngr_pair/README.md` still says "Only local agents are
   supported"; `libs/mngr/docs/commands/primary/pair.md`.
7. Changelog entries under `libs/mngr_pair/changelog/` and
   `libs/mngr/changelog/` (the latter is owed as soon as `git_fetch` or the
   `_dir_exists` lift touches `libs/mngr`).
8. Tests: unit tests for root construction, the six root-reference sites, the
   wrapper script, and remote version parsing; an acceptance test pairing
   against a docker-provider agent (`@pytest.mark.docker`).

## 8. Verified facts this design rests on

Established empirically against unison 2.54.0 and OpenSSH on macOS, rather
than assumed:

- unison >= 2.52 interoperates across versions (manual, "Version
  interoperability"); < 2.52 does not interoperate with > 2.53.8 at all.
- No aarch64 Linux asset exists in any unison release back to v2.53.2.
- The x86_64 static tarball contains `bin/unison` + `bin/unison-fsmonitor`
  and nothing else that matters; 2.1 MB compressed.
- No Debian or Ubuntu package ships a `unison-fsmonitor`: bookworm's `unison`
  file list is `/usr/bin/unison` plus docs, and an archive-wide contents search
  for a path ending in `unison-fsmonitor` returns nothing in any section or
  architecture. So the watcher check, not the version check, is what rejects
  mngr's own agent image.
- `-prefer` with a root that matches neither side is a hard error, and root
  matching is unique-substring, not exact.
- `-sshargs` is whitespace-split with no shell-quote processing, so
  shell-quoted paths break. Hence `-sshcmd` + a wrapper script.
- unison passes ssh options *after* the destination; OpenSSH accepts this.

Verified after implementation by running the generated provisioning script
directly: the happy path (existing compatible unison found, nothing downloaded),
rejection of a faked 2.51.5, acceptance at exactly 2.52.1, the full
download/checksum/install path with a faked `x86_64` arch, and an idempotent
second run. Validated under both `sh` and `dash`. Most of that is now covered by
executed-script unit tests in `libs/mngr_pair/imbue/mngr_pair/remote_test.py`,
including the install rung (with a fabricated tarball) and its refusal to report
a binary that cannot run.

Still unverified: the end-to-end `unison <local> ssh://... -sshcmd <wrapper>
-servercmd <path>` composition against a live remote agent.
