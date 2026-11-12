# Lead-side proxy flow: `mngr rsync` rationale

A rare path of the lead-proxy flow. The launcher makes this transfer itself at
`launch`, at whatever level of the dispatch is launching. Read this when you
are debugging one, or writing a sync of your own:

```bash
mngr rsync ./<SOURCE_DIR>/ <WORKER>:<DEST_DIR>/ \
    --uncommitted-changes=clobber
```

- `mngr rsync` takes `SOURCE DESTINATION` (positional): the local source dir
  first, then the `<WORKER>:<PATH>` agent endpoint. Exactly one side must
  reference an agent or remote host.
- Path resolution: mngr treats an argument as a *local path* only when it
  starts with `/`, `./`, `../`, or `~/` -- a bare `data/foo` is read as an
  *agent name* (hence the `./` on the source above). On an agent endpoint, a
  relative `<WORKER>:PATH` resolves against the worker's workdir; an absolute
  `<WORKER>:/PATH` is used verbatim.
- Use the directory form (trailing slash on both sides). mngr passes the paths
  through to rsync verbatim, so the trailing slash is load-bearing: it makes
  rsync copy directory *contents* into the destination instead of nesting the
  dir under it. Syncing a single file fails -- rsync wants a directory.
- `--uncommitted-changes=clobber` is required. Both endpoints routinely carry
  uncommitted local state, so the default `fail` mode would refuse the sync.
  `clobber` is safe here because every destination sits under gitignored
  `data/`: nothing tracked is overwritten, and nothing is pushed onto the git
  stash that every worktree of the repo shares.
- There is no `mngr file put` subcommand -- `mngr rsync` is the correct
  mechanism.
