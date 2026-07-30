"""Create ONE workspace in a running box. The single place that knows the create payload shape;
used both as the `workspace` utility (ad-hoc) and per-case by `launch`.

dwt_repo / dwt_branch pass through verbatim (a git URL, a local /work/clones/<x> path, empty
branch, etc.)."""

from __future__ import annotations

from imbue.mngr_minds_eval import minds_client


def _print_stage(stage: str) -> None:
    print("   ... {}".format(stage), flush=True)


def build_payload(*, dwt_repo: str, dwt_branch: str, name: str, backup_provider: str) -> dict:
    """Create-form fields. Workspaces are always Modal. Empty branch: a local clone is already on its
    commit, and passing a branch trips mngr's checkout_branch(FETCH_HEAD) on the use-in-place path.

    No AI-provider / Anthropic-key fields: the create API drives auth via the in-workspace sign-in
    modal now, and the eval agent picks up ANTHROPIC_API_KEY from the workspace host env (forwarded
    by the modal template's pass_host_env)."""
    payload = {
        "git_url": dwt_repo,
        "branch": dwt_branch,
        "launch_mode": "MODAL",
        "backup_provider": backup_provider.upper(),
    }
    if name:
        payload["host_name"] = name
    return payload


def create_workspace(
    *,
    port: str,
    dwt_repo: str,
    dwt_branch: str = "",
    name: str = "",
    backup_provider: str = "configure_later",
    quiet: bool = False,
    on_stage=None,
) -> str:
    """POST a Modal create and wait; return the new agent id. Raises minds_client.CreateError on
    failure (callers decide whether to abort or continue). Pass on_stage(caption) to route progress
    (launch's live table does this); else quiet suppresses prints, or it prints its own lines."""
    payload = build_payload(
        dwt_repo=dwt_repo,
        dwt_branch=dwt_branch,
        name=name,
        backup_provider=backup_provider,
    )
    if on_stage is None and not quiet:
        print(
            ">> creating modal workspace {} from {}@{} ...".format(
                name or "<auto>", dwt_repo, dwt_branch or "<default>"
            ),
            flush=True,
        )
        on_stage = _print_stage
    agent_id = minds_client.create_and_wait(port, payload, on_stage=on_stage)
    if on_stage is None and not quiet:
        print("  workspace up: {} (agent {})".format(name or "<auto>", agent_id), flush=True)
    return agent_id
