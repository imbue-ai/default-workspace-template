# disk_headroom

How much the workspace can still write to its disk.

On a cloud machine, everything the workspace writes counts against a disk quota
on the machine's data filesystem. The container cannot see that quota: `df`
and statfs report only the filesystem's own free space. Either one can run out
first, depending on how much of the disk the filesystem holds for its own
metadata, so `df` can show gigabytes free while writes already fail with
"Disk quota exceeded", or show less than the quota allows.

The machine publishes the quota's limit and usage into the workspace's volume,
at `/mngr-vol/.host-diskinfo` (mngr's gen-2 slice VM publisher). This library
reads that file while it is fresh and reports the smaller of the two limits.
Where no fresh file exists (a local docker workspace, or a machine that
publishes none), the filesystem's own figures are the answer. Paths on another
filesystem, such as the `/tmp` RAM disk, are never held to the quota.

The reader, `disk_headroom.space`, is stdlib-only, so scripts that run under a
bare `python3` can put `src` on `sys.path` and import it.

## API

- `read_disk_space(path) -> DiskSpace` -- `total`, `used` and `free` as the
  workspace can use them, plus `filesystem_free`, `quota_free` (None where no
  quota applies) and `is_quota_bound`. Under a quota, `used` is the quota's
  usage and `total` is `used + free`.

## Command

`uv run --no-sync disk-headroom [PATH] [--json]` (PATH defaults to `/`) prints
what can still be written and which limit it comes from. Use it wherever you
would run `df` to decide whether something fits.
