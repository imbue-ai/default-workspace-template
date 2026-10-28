"""``agent-access``: the visiting-agent operations from inside the workspace.

The same module the gateway's ``/_agent/*`` routes call, exposed to callers
that reach the workspace some other way: the Imbue Studio desktop over
``mngr exec`` (the Share panel's Agents section), the hosted web chrome over
owner-exec, or a person in the terminal. It works while the workspace is
unpublished (an invite minted then is usable once it is published, within its
fifteen minutes), and ``--json`` gives a caller the answer to parse.

``--granter``, ``--revoker`` and ``--owner`` are assertions: the CLI trusts
whoever reached the workspace with exec, because that reach is the authority
(the owner's, over ``mngr exec``, or a workspace-scope agent's). Each event the
CLI records says the CLI asserted it, as opposed to the gateway, which verifies
its caller.

With ``--json`` a refusal or a misconfiguration is one JSON object on stderr,
``{"error": <message>, "kind": "refused" | "misconfigured"}``, with exit code
2 or 3; without it the message is a ``agent-access: ...`` line.

    agent-access invite --granter user:<id> [--scope workspace|<app,app>] [--expires-in-days N] [--name-hint ...]
    agent-access list
    agent-access revoke <agent_id> --revoker user:<id> [--owner]
    agent-access primer [--scope ...]
    agent-access skills
    agent-access changes [--since <iso>]
    agent-access reconcile
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from share_gateway import materials as materials_module
from share_gateway.agent_access import InvalidGrantLifetimeError
from share_gateway.agent_access import LastSeenStore
from share_gateway.agent_access import RevokeRefusedError
from share_gateway.agent_access import default_agent_access_paths
from share_gateway.agent_access import describe_grant
from share_gateway.agent_access import grant_lifetime_from_days
from share_gateway.agent_access import list_grants
from share_gateway.agent_access import mint_invite
from share_gateway.agent_access import reconcile
from share_gateway.agent_access import revoke
from share_gateway.agent_audit import events_dir_from_environ
from share_gateway.agent_grants import AGENT_ACCESS_STATE_DIR
from share_gateway.agent_grants import AgentGrantsError
from share_gateway.agent_grants import AgentScopeError
from share_gateway.agent_grants import WORKSPACE_SCOPE
from share_gateway.agent_grants import utc_now
from share_gateway.agent_grants import validate_scope
from share_gateway.agent_orientation import HELPER_PATH
from share_gateway.agent_orientation import SKILLS_INDEX_CACHE_PATH
from share_gateway.agent_orientation import build_changes_feed
from share_gateway.agent_orientation import cached_skills_index
from share_gateway.agent_orientation import default_changes_since
from share_gateway.agent_orientation import file_sha256
from share_gateway.agent_orientation import helper_source_url
from share_gateway.agent_orientation import parse_since
from share_gateway.agent_orientation import render_instruction_block
from share_gateway.agent_orientation import render_primer
from share_gateway.agent_orientation import summarize_connected_accounts
from share_gateway.agent_orientation import template_ref
from share_gateway.agent_workspace import HOST_KEY_PUBLIC_PATH
from share_gateway.agent_workspace import app_origins_for
from share_gateway.agent_workspace import read_workspace_coordinates
from share_gateway.caddyfile import APPS_TOML_PATH
from share_gateway.caddyfile import build_grantable_service_names
from share_gateway.caddyfile import read_registered_apps
from share_gateway.materials import load_or_create_auth_label
from share_gateway.materials import read_share_materials

_EXIT_REFUSED = 2
_EXIT_MISCONFIGURED = 3
_EXIT_CODE_BY_KIND = {"refused": _EXIT_REFUSED, "misconfigured": _EXIT_MISCONFIGURED}


def _parse_scope_argument(raw: str) -> "str | list[str]":
    if raw == WORKSPACE_SCOPE:
        return WORKSPACE_SCOPE
    return [name.strip() for name in raw.split(",") if name.strip()]


def _workspace_domain() -> tuple[str, bool]:
    """``(domain, is_published)``: the live materials' domain, else the one last shared under, else ''."""
    materials = read_share_materials(materials_module.MATERIALS_FILE)
    if materials is not None:
        return materials.workspace_domain, True
    try:
        return materials_module.SHARE_DOMAIN_FILE.read_text().strip(), False
    except OSError:
        return "", False


def _write_line(text: str, is_error: bool = False) -> None:
    """The CLI's one output seam (the package has no human-output helper of its own): results to stdout, refusals to stderr."""
    print(text, file=sys.stderr if is_error else sys.stdout)


def _emit(payload: dict[str, object], text: str, is_json: bool) -> None:
    _write_line(json.dumps(payload) if is_json else text)


def _fail(args: argparse.Namespace, message: str, kind: str) -> int:
    """Report a refusal or a misconfiguration the way the caller asked for, and answer its exit code."""
    if args.json:
        _write_line(json.dumps({"error": message, "kind": kind}), is_error=True)
    else:
        _write_line(f"agent-access: {message}", is_error=True)
    return _EXIT_CODE_BY_KIND[kind]


def _command_invite(args: argparse.Namespace) -> int:
    paths = default_agent_access_paths(events_dir_from_environ(os.environ))
    apps = read_registered_apps(APPS_TOML_PATH)
    try:
        scope = validate_scope(
            _parse_scope_argument(args.scope),
            sorted(build_grantable_service_names(apps)),
        )
    except AgentScopeError as exc:
        return _fail(args, str(exc), "refused")
    try:
        lifetime = grant_lifetime_from_days(args.expires_in_days)
    except InvalidGrantLifetimeError as exc:
        return _fail(args, str(exc), "refused")
    now = utc_now()
    try:
        minted = mint_invite(paths, scope, args.granter, args.name_hint, lifetime, now, via="cli")
    except AgentGrantsError as exc:
        return _fail(args, f"the agent access files are misconfigured: {exc}", "misconfigured")
    workspace_domain, is_published = _workspace_domain()
    auth_label = load_or_create_auth_label(materials_module.AUTH_LABEL_FILE)
    auth_origin = f"https://{auth_label}.{workspace_domain}" if workspace_domain else ""
    coordinates = read_workspace_coordinates(os.environ, HOST_KEY_PUBLIC_PATH, workspace_domain.split(".", 1)[0])
    instructions = render_instruction_block(
        repo_root=Path.cwd(),
        auth_origin=auth_origin or "https://<the workspace's address once it is published>",
        code=minted.code,
        expires_at=minted.invite.expires_at,
        scope=scope,
        workspace_display_name=coordinates.display_name,
        name_hint=minted.invite.name_hint,
        app_origins=app_origins_for(apps, workspace_domain, None if scope == WORKSPACE_SCOPE else list(scope)),
        helper_source_url=helper_source_url(Path.cwd()),
        helper_sha256=file_sha256(HELPER_PATH),
    )
    payload: dict[str, object] = {
        "code": minted.code,
        "expires_at": minted.invite.expires_at.isoformat(),
        "scope": scope,
        "granter": args.granter,
        "auth_origin": auth_origin,
        "is_published": is_published,
        "instructions": instructions,
    }
    _emit(payload, instructions, args.json)
    return 0


def _command_list(args: argparse.Namespace) -> int:
    paths = default_agent_access_paths(events_dir_from_environ(os.environ))
    try:
        grants = list_grants(paths, utc_now())
    except AgentGrantsError as exc:
        return _fail(args, f"the agent access files are misconfigured: {exc}", "misconfigured")
    last_seen = LastSeenStore(AGENT_ACCESS_STATE_DIR / "last_seen.json").read_all()
    described = [describe_grant(grant, last_seen.get(grant.agent_id)) for grant in grants]
    lines = [
        f"{entry['agent_id']}  {entry['name']}  scope={entry['scope']}  granter={entry['granter']}  last_seen={entry['last_seen_at'] or 'never'}"
        for entry in described
    ]
    _emit(
        {"agents": described},
        "\n".join(lines) if lines else "No visiting agents.",
        args.json,
    )
    return 0


def _command_revoke(args: argparse.Namespace) -> int:
    paths = default_agent_access_paths(events_dir_from_environ(os.environ))
    try:
        revoked = revoke(paths, args.agent_id, args.revoker, args.owner, utc_now(), via="cli")
    except RevokeRefusedError as exc:
        return _fail(args, str(exc), "refused")
    except AgentGrantsError as exc:
        return _fail(args, f"the agent access files are misconfigured: {exc}", "misconfigured")
    _emit(
        {"revoked": revoked.agent_id, "name": revoked.name},
        f"Revoked {revoked.agent_id} ({revoked.name})",
        args.json,
    )
    return 0


def _command_primer(args: argparse.Namespace) -> int:
    repo_root = Path.cwd()
    workspace_domain, _ = _workspace_domain()
    coordinates = read_workspace_coordinates(os.environ, HOST_KEY_PUBLIC_PATH, workspace_domain.split(".", 1)[0])
    scope = _parse_scope_argument(args.scope)
    now = utc_now()
    text = render_primer(
        repo_root=repo_root,
        scope=scope,
        workspace_display_name=coordinates.display_name,
        workspace_id=coordinates.workspace_id,
        skills_index=cached_skills_index(repo_root, SKILLS_INDEX_CACHE_PATH, template_ref(repo_root)),
        apps=read_registered_apps(APPS_TOML_PATH),
        workspace_domain=workspace_domain,
        connected_accounts=summarize_connected_accounts(os.environ) if scope == WORKSPACE_SCOPE else "Not applicable.",
        changes=build_changes_feed(repo_root, default_changes_since(now)),
    )
    _write_line(text)
    return 0


def _command_skills(args: argparse.Namespace) -> int:
    repo_root = Path.cwd()
    _write_line(
        json.dumps(
            cached_skills_index(repo_root, SKILLS_INDEX_CACHE_PATH, template_ref(repo_root)),
            indent=2,
        )
    )
    return 0


def _command_changes(args: argparse.Namespace) -> int:
    repo_root = Path.cwd()
    now = utc_now()
    since = default_changes_since(now)
    if args.since:
        parsed = parse_since(args.since)
        if parsed is None:
            return _fail(args, "--since must be an ISO 8601 timestamp with a timezone", "refused")
        since = parsed
    _write_line(json.dumps(build_changes_feed(repo_root, since), indent=2))
    return 0


def _command_reconcile(args: argparse.Namespace) -> int:
    paths = default_agent_access_paths(events_dir_from_environ(os.environ))
    try:
        is_changed = reconcile(paths, utc_now())
    except AgentGrantsError as exc:
        return _fail(args, f"the agent access files are misconfigured: {exc}", "misconfigured")
    _emit(
        {"changed": is_changed},
        "Reconciled." if is_changed else "Nothing to do.",
        args.json,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-access", description="Visiting-agent access to this workspace.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    invite = subparsers.add_parser(
        "invite",
        help="Mint a one-time code and print the instructions an agent follows.",
    )
    invite.add_argument("--granter", required=True, help="Who is inviting: user:<id> or agent:<id>.")
    invite.add_argument(
        "--scope",
        default=WORKSPACE_SCOPE,
        help="'workspace', or a comma-separated list of app names.",
    )
    invite.add_argument(
        "--expires-in-days",
        type=float,
        default=None,
        help="Expire the grant after this many days.",
    )
    invite.add_argument(
        "--name-hint",
        default="",
        help="A suggested name for the agent, shown in the instructions.",
    )
    invite.add_argument(
        "--json",
        action="store_true",
        help="Print a JSON document instead of the instructions.",
    )
    invite.set_defaults(handler=_command_invite)

    listing = subparsers.add_parser("list", help="List the visiting agents.")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(handler=_command_list)

    revoking = subparsers.add_parser("revoke", help="Revoke one visiting agent.")
    revoking.add_argument("agent_id")
    revoking.add_argument("--revoker", required=True, help="Who is revoking: user:<id> or agent:<id>.")
    revoking.add_argument(
        "--owner",
        action="store_true",
        help="The revoker is the workspace owner (may revoke any).",
    )
    revoking.add_argument("--json", action="store_true")
    revoking.set_defaults(handler=_command_revoke)

    primer = subparsers.add_parser("primer", help="Render the primer a visiting agent reads.")
    primer.add_argument("--scope", default=WORKSPACE_SCOPE)
    primer.set_defaults(handler=_command_primer)

    skills = subparsers.add_parser("skills", help="Print the skills index.")
    skills.set_defaults(handler=_command_skills)

    changes = subparsers.add_parser("changes", help="Print the changes feed.")
    changes.add_argument(
        "--since",
        default="",
        help="An ISO 8601 timestamp; the last seven days when omitted.",
    )
    changes.add_argument("--json", action="store_true")
    changes.set_defaults(handler=_command_changes)

    reconciling = subparsers.add_parser(
        "reconcile",
        help="Prune expired grants and re-assert the authorized_keys lines.",
    )
    reconciling.add_argument("--json", action="store_true")
    reconciling.set_defaults(handler=_command_reconcile)
    return parser


def run(argv: Sequence[str]) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
