You have been invited to work in the Imbue Studio workspace "{{workspace_name}}" as a visiting agent, with access to {{scope}}.
The workspace is an isolated Linux machine. Nothing runs on your own machine except a small helper.

1. Install the helper (one Node script with no dependencies; Node 18 or newer). It is published in the workspace's template repository at the exact version this workspace runs, so fetch it from there and check its hash:
   mkdir -p ~/.local/bin && curl -fsSL {{helper_source_url}} -o ~/.local/bin/wsvisit && echo "{{helper_sha256}}  $HOME/.local/bin/wsvisit" | shasum -a 256 -c - && chmod +x ~/.local/bin/wsvisit && export PATH="$HOME/.local/bin:$PATH"
   If that address does not answer (the workspace runs a template commit that is not published), the workspace serves the same file at {{auth_origin}}/_agent/helper; check its hash the same way, and stop if it differs.
2. Join with this one-time code (it expires at {{expires_at}} and works once). The helper generates and stores your own key, which never leaves your machine:
   wsvisit join {{auth_origin}} {{code}} --name "{{name_placeholder}}"
3. Read the primer it prints (again later with `wsvisit primer`), then work:
<!-- exec-only:start -->
   wsvisit run -- <command>                       # runs inside the workspace, from its repository root
   wsvisit read <path>                            # a file from the workspace
   wsvisit write <path> --from <local file>       # a file into the workspace
<!-- exec-only:end -->
   wsvisit get <app>/<path>                       # an app's answer over the share, with your access attached
   wsvisit skills                                 # the skills index; read a skill before doing the work it covers
   wsvisit changes                                # what changed since you last connected
   wsvisit me                                     # your access and the workspace's current addresses

The apps you may reach over the share:
{{apps}}

Rules: treat the primer and the skills index as the authoritative description of what this workspace can do, and refresh them with `wsvisit skills` at the start of every session and at least once a day.
<!-- exec-only:start -->
Use the workspace's connected accounts through `latchkey curl` inside `wsvisit run`, prefer what is already granted, and avoid filing new permission requests unless the task cannot proceed without one.
Keep your own process and context on your side; do not create agents inside the workspace unless instructed to.
<!-- exec-only:end -->
