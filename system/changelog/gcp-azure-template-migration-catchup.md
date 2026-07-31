The `gcp` and `azure` create templates were left behind by three repo-wide
migrations and referenced paths and binaries that no longer exist. Both are
byte-identical to each other and describe themselves as mirroring the `aws`
template, but had not been updated since they were added on 2026-07-09, so they
now match `aws` again.

Four stale references, one per migration that skipped them:

- `build_arg__extend` pointed at `--file=Dockerfile`. The Dockerfile moved to
  `system/Dockerfile` in the tree restructure (9e6a0315); there is no root
  `Dockerfile`, so the image build could not resolve it.
- `target_path` was `/mngr/code/`, from before the `/home/user` user-data layout
  cutover (0cce093f). Every other template uses `/home/user/workspace/`, which is
  also the image's `WORKDIR`.
- The outer-VM autostart unit ran
  `/mngr/code/scripts/minds_start_services_agent.sh`. Scripts moved to
  `system/scripts/`, so a VM reboot could not relaunch the system-services agent.
- `post_host_create_command__extend` ran `/usr/local/bin/fct-seed`, the
  pre-rename name of the first-boot seed. The Dockerfile installs it as
  `/usr/local/bin/default-workspace-template-seed`.

Not changed: the `pass_host_env__extend` line, which is deliberate for these two
templates (it mirrors the docker template's forwarding) rather than stale.
