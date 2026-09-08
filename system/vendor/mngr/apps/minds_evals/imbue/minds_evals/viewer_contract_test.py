"""The vendored viewer talks to a stock harbor backend, and nothing in the toolchain connects the
two: the frontend's endpoint URLs are hand-written TypeScript, with no generated client, so a
harbor upgrade that renames a route would surface as a blank page rather than as a build failure.
These tests read the URLs straight out of the vendored source and check them against the routes
harbor actually registers."""

import re
from pathlib import Path
from typing import Final

from harbor.viewer import create_app
from starlette.routing import Route

from imbue.minds_evals.data_types import StepBoundary
from imbue.minds_evals.data_types import TrajectoryProvenance
from imbue.minds_evals.data_types import UsageSource
from imbue.minds_evals.trajectory import STEP_BOUNDARY_KIND
from imbue.minds_evals.trajectory import build_hand_built_trajectory
from imbue.minds_evals.usage import summarize_workspace_usage

VIEWER_API_CLIENT: Final[Path] = Path(__file__).parents[2] / "viewer" / "app" / "lib" / "api.ts"
VIEWER_HARNESS_ANNOTATION: Final[Path] = (
    Path(__file__).parents[2] / "viewer" / "app" / "components" / "trajectory" / "harness-annotation.tsx"
)
# Every call in the client is built from this prefix, so it is the anchor for finding them.
_CALL_PATTERN: Final[re.Pattern[str]] = re.compile(r"\$\{API_BASE\}(/api/[^`]*)")
_INTERPOLATION: Final[re.Pattern[str]] = re.compile(r"\$\{[^}]*\}")


def _as_route_shape(path: str) -> tuple[str, ...]:
    """A URL reduced to the shape a router matches on: literal segments kept, variable ones
    collapsed to a wildcard.

    Both sides interpolate, just differently -- the client writes `${encodeURIComponent(job)}`
    where FastAPI writes `{job_name}` -- so comparing shapes is what lets the two be checked
    against each other at all. A trailing query string is not part of the path; in the client it
    is either a literal `?...` or a helper glued onto the last segment (`trajectory${stepQuery()}`),
    which is why a wildcard is only recognised as such when it starts its own segment.
    """
    segments = []
    for segment in path.split("?")[0].split("/"):
        if segment.startswith("${") or segment.startswith("{"):
            segments.append("*")
        else:
            # A helper appended to a literal segment is a query suffix, not part of the path.
            segments.append(_INTERPOLATION.sub("", segment))
    return tuple(segment for segment in segments if segment)


def _client_route_shapes() -> set[tuple[str, ...]]:
    source = VIEWER_API_CLIENT.read_text()
    return {_as_route_shape(match.group(1)) for match in _CALL_PATTERN.finditer(source)}


def _raw_segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _backend_routes(tmp_path: Path) -> tuple[set[tuple[str, ...]], set[tuple[str, ...]]]:
    """The routes harbor registers, as exact shapes plus the prefixes of its catch-all routes.

    Job mode and task mode register disjoint endpoint sets on the same app factory, and the single
    frontend calls both, so the contract is the union. A `{name:path}` parameter swallows the whole
    remaining URL, so such a route is recorded as the prefix ahead of it rather than as a shape of
    fixed length -- that is what serves `files/config.json` and every other name beneath it.
    """
    exact: set[tuple[str, ...]] = set()
    prefixes: set[tuple[str, ...]] = set()
    for mode in ("jobs", "tasks"):
        app = create_app(tmp_path, mode=mode)
        for route in app.routes:
            # `routes` is typed as the base class, which carries no path; mounts for the static
            # assets sit in the same list and are not what this checks.
            if not isinstance(route, Route) or not route.path.startswith("/api/"):
                continue
            route_path = route.path
            shape = _as_route_shape(route_path)
            catch_all_at = next(
                (index for index, segment in enumerate(_raw_segments(route_path)) if ":path}" in segment),
                None,
            )
            if catch_all_at is None:
                exact.add(shape)
            else:
                prefixes.add(shape[:catch_all_at])
    return exact, prefixes


def _unserved(
    client: set[tuple[str, ...]], exact: set[tuple[str, ...]], prefixes: set[tuple[str, ...]]
) -> list[tuple[str, ...]]:
    return sorted(
        shape
        for shape in client
        if shape not in exact and not any(shape[: len(prefix)] == prefix for prefix in prefixes)
    )


def test_the_vendored_client_only_calls_routes_harbor_registers(tmp_path: Path) -> None:
    exact, prefixes = _backend_routes(tmp_path)
    missing = _unserved(_client_route_shapes(), exact, prefixes)

    assert missing == [], (
        "The vendored viewer calls endpoints this harbor does not serve: "
        + ", ".join("/".join(shape) for shape in missing)
        + ". Re-vendor the viewer at the pinned harbor tag (see viewer/VENDORED_FROM.md)."
    )


def test_the_client_is_where_this_expects_it() -> None:
    """The extraction above silently passes on an empty file, so the fixture itself is asserted."""
    assert VIEWER_API_CLIENT.is_file()
    assert VIEWER_HARNESS_ANNOTATION.is_file()
    assert len(_client_route_shapes()) > 20


def _tsx_constant(source: str, name: str) -> str:
    """The value of a `const NAME = "value";` declaration in the vendored TypeScript."""
    match = re.search(rf'const {name} = "([^"]*)";', source)
    assert match is not None, f"{name} is not declared in {VIEWER_HARNESS_ANNOTATION}"
    return match.group(1)


def test_the_viewer_reads_the_extra_namespace_the_harness_writes() -> None:
    """The harness tags its own steps in ATIF `extra` and the viewer styles them from that tag, but
    the two sides are a Python dict and a TypeScript literal with nothing between them. A rename on
    either side would not fail anything -- the boundaries would just quietly stop being drawn."""
    source = VIEWER_HARNESS_ANNOTATION.read_text()
    namespace = _tsx_constant(source, "HARNESS_NAMESPACE")
    boundary_kind = _tsx_constant(source, "STEP_BOUNDARY")

    built = build_hand_built_trajectory(
        [{"role": "user", "text": "Now change it"}],
        TrajectoryProvenance(
            driver_name="minds-persona-driver",
            driver_version="0.1.0",
            decider_model="claude-opus-4-8",
            decider_turns=(),
            harbor_session_id="session-1",
            case_id="todo-app",
            usage_source=UsageSource.TRANSCRIPT,
        ),
        summarize_workspace_usage(()),
        timestamp="2026-09-01T00:00:00Z",
        boundaries=(
            StepBoundary(
                name="adjust-requirements",
                started_at="2026-09-01T00:00:00Z",
                conversation_index=0,
                opening_message="Now change it",
            ),
        ),
    )

    assert built is not None
    marker = built.to_json_dict()["steps"][0]
    assert marker["extra"] == {namespace: {"kind": boundary_kind, "step_name": "adjust-requirements"}}
    assert boundary_kind == STEP_BOUNDARY_KIND
    # The divider's label comes from this field, so the viewer's read of it must stay valid.
    assert "namespace.step_name" in source
