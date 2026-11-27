from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app_manifest.errors import ManifestLoadError
from app_manifest.manifest import AppManifest
from app_manifest.manifest import EntryMode
from app_manifest.manifest import LaunchPathMethod
from app_manifest.manifest import LocationScope
from app_manifest.manifest import ManifestReadMode
from app_manifest.manifest import MessageHandler
from app_manifest.manifest import PinStyle
from app_manifest.manifest import PreviewSpec
from app_manifest.manifest import RESERVED_LAUNCH_PARAM_NAMES
from app_manifest.manifest import ShortcutMode
from app_manifest.manifest import ThemingMode
from app_manifest.manifest import load_manifest
from app_manifest.manifest import manifest_icon_path
from app_manifest.manifest import scaffold_preview_spec
from app_manifest.primitives import AppName
from app_manifest.records import AppOrigin
from app_manifest.records import AppRecord
from app_manifest.records import write_record
from app_manifest.testing import APP_ICON_MARKUP
from app_manifest.testing import captured_warnings
from app_manifest.testing import write_app_manifest
from app_manifest.testing import write_repo_file


def _launch_path_params() -> list[dict[str, object]]:
    return [{"name": "path", "label": "Path", "required": False}]


def _launch_path_data() -> dict[str, object]:
    return {
        "id": "new",
        "label": "Files",
        "path": "/",
        "params": _launch_path_params(),
        "text_param": "path",
    }


def _full_manifest_data() -> dict[str, object]:
    return {
        "name": "files",
        "display_name": "Files",
        "icon": "icon.svg",
        "critical": False,
        "stop_when_no_windows": True,
        "uses_microphone": True,
        "priority": "files",
        "program": "files",
        "internal": False,
        "default_shortcut": {"launch": "new", "mode": "focus"},
        "launch_paths": [_launch_path_data()],
        "launcher_rank": 20,
        "pin": {
            "path": "/",
            "style": "avatar",
            "scope": "independent",
            "default_mode": "floating",
        },
        "window_closed_path": "/api/window-closed",
    }


def test_full_manifest_round_trips_every_field() -> None:
    manifest = AppManifest.model_validate(_full_manifest_data())

    assert manifest.name == "files"
    assert manifest.display_name == "Files"
    assert manifest.icon == "icon.svg"
    assert manifest.priority == "files"
    assert manifest.program == "files"
    assert [(shortcut.launch, shortcut.mode) for shortcut in manifest.default_shortcuts] == [
        ("new", ShortcutMode.FOCUS)
    ]
    assert [
        (launch_path.id, launch_path.path) for launch_path in manifest.launch_paths
    ] == [("new", "/")]
    assert manifest.launch_paths[0].params[0].name == "path"
    assert manifest.launch_paths[0].text_param == "path"
    assert manifest.launcher_rank == 20
    assert manifest.pin is not None
    assert manifest.pin.path == "/"
    assert manifest.pin.style is PinStyle.AVATAR
    assert manifest.pin.scope is LocationScope.INDEPENDENT
    assert manifest.pin.default_mode is EntryMode.FLOATING
    assert manifest.window_closed_path == "/api/window-closed"
    assert manifest.stop_when_no_windows is True
    assert manifest.uses_microphone is True


def test_an_app_seeds_its_shortcuts_unless_its_manifest_says_otherwise() -> None:
    minimal = {"name": "news", "display_name": "News", "icon": "icon.svg"}

    assert AppManifest.model_validate(minimal).seeds_shortcuts is True
    assert AppManifest.model_validate({**minimal, "seeds_shortcuts": False}).seeds_shortcuts is False


def test_uses_microphone_defaults_to_false() -> None:
    manifest = AppManifest.model_validate({"name": "news", "display_name": "News", "icon": "icon.svg"})
    assert manifest.uses_microphone is False


def test_stop_when_no_windows_defaults_to_false_and_is_refused_on_a_critical_app() -> (
    None
):
    manifest = AppManifest.model_validate(
        {"name": "news", "display_name": "News", "icon": "icon.svg"}
    )
    assert manifest.stop_when_no_windows is False
    with pytest.raises(ValidationError, match="critical app cannot declare stop_when_no_windows"):
        AppManifest.model_validate({**_full_manifest_data(), "critical": True, "stop_when_no_windows": True})
    assert AppManifest.model_validate(
        {**_full_manifest_data(), "critical": True, "stop_when_no_windows": False}
    ).critical


def test_a_pin_takes_the_plain_linked_bar_defaults_and_needs_only_a_path() -> None:
    manifest = AppManifest.model_validate(
        {
            "name": "news",
            "display_name": "News",
            "icon": "icon.svg",
            "pin": {"path": "/inbox"},
        }
    )
    assert manifest.pin is not None
    assert (
        manifest.pin.path,
        manifest.pin.style,
        manifest.pin.scope,
        manifest.pin.default_mode,
    ) == (
        "/inbox",
        PinStyle.PLAIN,
        LocationScope.LINKED,
        EntryMode.BAR,
    )
    with pytest.raises(ValidationError, match="path"):
        AppManifest.model_validate(
            {"name": "news", "display_name": "News", "icon": "icon.svg", "pin": {}}
        )


@pytest.mark.parametrize(
    ("pin", "field"),
    [
        ({"path": "/?chat=1"}, "path"),
        ({"path": "inbox"}, "path"),
        ({"path": "/", "style": "dot"}, "style"),
        ({"path": "/", "scope": "personal"}, "scope"),
        ({"path": "/", "default_mode": "hidden"}, "default_mode"),
        ({"path": "/", "position": {"x": 0.5, "y": 0.5}}, "position"),
    ],
)
def test_a_pin_follows_the_launch_path_rule_and_the_three_vocabularies(
    pin: dict[str, object], field: str
) -> None:
    with pytest.raises(ValidationError, match=field):
        AppManifest.model_validate(
            {"name": "news", "display_name": "News", "icon": "icon.svg", "pin": pin}
        )


@pytest.mark.parametrize("description", ["", "   ", "two\nlines", "x" * 201])
def test_a_description_is_one_non_empty_line_of_at_most_200_characters(
    description: str,
) -> None:
    with pytest.raises(ValidationError):
        AppManifest.model_validate({**_full_manifest_data(), "description": description})


def test_a_manifest_reads_its_description_and_api_schema_path() -> None:
    manifest = AppManifest.model_validate(
        {
            **_full_manifest_data(),
            "description": "Keeps notes.",
            "api_schema_path": "/api/schema",
        }
    )
    assert manifest.description == "Keeps notes."
    assert manifest.api_schema_path == "/api/schema"
    assert AppManifest.model_validate(_full_manifest_data()).description is None
    assert AppManifest.model_validate(_full_manifest_data()).api_schema_path is None
    with pytest.raises(ValidationError):
        AppManifest.model_validate({**_full_manifest_data(), "api_schema_path": "api/schema?x=1"})
    with pytest.raises(ValidationError, match="also declares description"):
        AppManifest.model_validate(
            {**_full_manifest_data(), "api_schema_path": "/api/schema"}
        )


def test_window_closed_path_follows_the_launch_path_rule() -> None:
    with pytest.raises(ValidationError, match="no query string"):
        AppManifest.model_validate(
            {**_full_manifest_data(), "window_closed_path": "/closed?x=1"}
        )


def test_message_handlers_name_the_embedder_messages_an_app_takes_and_where_the_shell_posts_them() -> (
    None
):
    manifest = AppManifest.model_validate(
        {
            **_full_manifest_data(),
            "message_handlers": [
                {"type": "minds:focus-chat", "path": "/api/focus-chat"},
                {"type": "minds:permission-resolutions", "path": "/api/verdicts"},
            ],
        }
    )

    assert [(handler.type, handler.path) for handler in manifest.message_handlers] == [
        ("minds:focus-chat", "/api/focus-chat"),
        ("minds:permission-resolutions", "/api/verdicts"),
    ]
    assert AppManifest.model_validate(_full_manifest_data()).message_handlers == ()


def test_a_message_handler_shows_a_page_built_from_the_message_and_names_the_pages_that_already_show_it() -> (
    None
):
    manifest = AppManifest.model_validate(
        {
            **_full_manifest_data(),
            "message_handlers": [
                {
                    "type": "open:file",
                    "show": "{path}?view",
                    "showing": ["{path}", "{path}/", "{path}/?view"],
                    "reload_when_raised": True,
                },
                {"type": "open:url", "path": "/api/open-url"},
            ],
        }
    )

    shown, posted = manifest.message_handlers
    assert (shown.type, shown.path, shown.show, shown.showing, shown.reload_when_raised) == (
        "open:file",
        None,
        "{path}?view",
        ("{path}", "{path}/", "{path}/?view"),
        True,
    )
    assert (posted.type, posted.path, posted.show, posted.showing, posted.reload_when_raised) == (
        "open:url",
        "/api/open-url",
        None,
        (),
        False,
    )


@pytest.mark.parametrize(
    "handler",
    [
        {"type": "minds:focus-chat", "path": "/api/focus-chat"},
        {"type": "open:file", "show": "{path}?view", "showing": ["{path}/"], "reload_when_raised": True},
    ],
)
def test_a_message_handler_reads_back_from_its_own_dump(handler: dict[str, object]) -> None:
    """The shell answers its inventory with these handlers dumped whole, and callers read the answer with this model."""
    loaded = MessageHandler.model_validate(handler)
    assert MessageHandler.model_validate(loaded.model_dump(mode="json")) == loaded


@pytest.mark.parametrize(
    ("handler", "match"),
    [
        pytest.param(
            {"type": "focus-chat", "path": "/api/focus-chat"},
            "lowercase prefix",
            id="no-prefix",
        ),
        pytest.param(
            {"type": "shell:open", "path": "/api/open"},
            "app contract's own prefix",
            id="shell-prefix",
        ),
        pytest.param(
            {"type": "open:file", "path": "/api/open", "showing": ["{path}/"]},
            "only beside 'show'",
            id="showing-alone",
        ),
        pytest.param(
            {"type": "Open:file", "path": "/api/open"},
            "lowercase prefix",
            id="uppercase-prefix",
        ),
        pytest.param(
            {"type": "minds:", "path": "/api/focus-chat"},
            "lowercase prefix",
            id="prefix-alone",
        ),
        pytest.param(
            {"type": "minds:Focus Chat", "path": "/api/focus-chat"},
            "kebab-case",
            id="not-kebab-case",
        ),
        pytest.param(
            {"type": "minds:focus-chat", "path": "api/focus-chat"},
            "single '/'",
            id="unrooted-path",
        ),
        pytest.param(
            {"type": "minds:focus-chat", "path": "/api/focus-chat?x=1"},
            "no query string",
            id="query",
        ),
        pytest.param(
            {"type": "minds:focus-chat"},
            "exactly one of 'path'",
            id="neither-path-nor-show",
        ),
        pytest.param(
            {"type": "open:file", "path": "/api/open", "show": "{path}"},
            "exactly one of 'path'",
            id="path-and-show",
        ),
        pytest.param(
            {"type": "open:file", "path": "/api/open", "showing": ["{path}/"]},
            "only beside 'show'",
            id="showing-alone",
        ),
        pytest.param(
            {"type": "open:file", "path": "/api/open", "reload_when_raised": True},
            "'reload_when_raised' only beside 'show'",
            id="reload-when-raised-beside-a-path",
        ),
        pytest.param(
            {"type": "open:file", "show": "{path?view"},
            "a placeholder is",
            id="unclosed-placeholder",
        ),
        pytest.param(
            {"type": "open:file", "show": "{}?view"},
            "a placeholder is",
            id="empty-placeholder",
        ),
        pytest.param(
            {"type": "open:file", "show": "{1path}"},
            "a placeholder is",
            id="placeholder-not-a-name",
        ),
        pytest.param(
            {"type": "open:file", "show": "/{path}}"},
            "a placeholder is",
            id="stray-brace",
        ),
        pytest.param(
            {"type": "open:file", "show": "{path}?view={path}"},
            "query string is fixed",
            id="query-field",
        ),
        pytest.param(
            {"type": "open:file", "show": "{path}#top"}, "no fragment", id="fragment"
        ),
        pytest.param(
            {"type": "open:file", "show": "view/{path}"},
            "single '/' or a placeholder",
            id="unrooted",
        ),
        pytest.param(
            {"type": "open:file", "show": "//{path}"},
            "single '/' or a placeholder",
            id="double-slash",
        ),
        pytest.param(
            {"type": "open:file", "show": "/a b/{path}"},
            "nothing a URL would escape",
            id="unescaped",
        ),
        pytest.param(
            {"type": "open:file", "show": "{path}", "showing": ["{path"]},
            "a placeholder is",
            id="bad-showing",
        ),
    ],
)
def test_a_malformed_message_handler_fails_to_load_with_its_reason(
    handler: dict[str, object], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        AppManifest.model_validate(
            {**_full_manifest_data(), "message_handlers": [handler]}
        )


def test_keybindings_declare_the_actions_a_person_may_rebind_in_display_order() -> None:
    manifest = AppManifest.model_validate(
        {
            **_full_manifest_data(),
            "keybindings": [
                {"id": "rename", "label": "Rename the selected entry", "default_chords": ["f2"]},
                {"id": "unbound", "label": "Something with no default"},
            ],
        }
    )

    assert [(action.id, action.label, action.default_chords) for action in manifest.keybindings] == [
        ("rename", "Rename the selected entry", ("f2",)),
        ("unbound", "Something with no default", ()),
    ]
    assert AppManifest.model_validate(_full_manifest_data()).keybindings == ()


@pytest.mark.parametrize(
    ("action", "match"),
    [
        pytest.param({"id": "Rename", "label": "Rename"}, "invalid keybinding action id", id="uppercase-id"),
        pytest.param({"id": "rename", "label": ""}, "keybinding label must not be empty", id="empty-label"),
        pytest.param({"id": "rename", "label": "two\nlines"}, "is a single line", id="two-line-label"),
        pytest.param({"id": "rename", "label": "Rename", "default_chords": [""]}, "at least 1 character", id="empty-chord"),
    ],
)
def test_a_malformed_keybinding_fails_to_load_with_its_reason(action: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        AppManifest.model_validate({**_full_manifest_data(), "keybindings": [action]})


def test_duplicate_keybinding_action_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="keybinding action ids must be unique"):
        AppManifest.model_validate(
            {
                **_full_manifest_data(),
                "keybindings": [
                    {"id": "rename", "label": "Rename"},
                    {"id": "rename", "label": "Rename again"},
                ],
            }
        )


def test_duplicate_message_handler_types_are_rejected() -> None:
    with pytest.raises(ValidationError, match="message handler types must be unique"):
        AppManifest.model_validate(
            {
                **_full_manifest_data(),
                "message_handlers": [
                    {"type": "minds:focus-chat", "path": "/api/focus-chat"},
                    {"type": "minds:focus-chat", "path": "/api/other"},
                ],
            }
        )


def test_text_param_must_name_a_declared_param_and_defaults_to_none() -> None:
    data = _full_manifest_data()
    launch_path = _launch_path_data()
    del launch_path["text_param"]
    assert (
        AppManifest.model_validate({**data, "launch_paths": [launch_path]})
        .launch_paths[0]
        .text_param
        is None
    )

    with pytest.raises(
        ValidationError,
        match="text_param 'message' is not one of the launch path's params",
    ):
        AppManifest.model_validate(
            {**data, "launch_paths": [{**launch_path, "text_param": "message"}]}
        )
    with pytest.raises(ValidationError, match="at least 1 character"):
        AppManifest.model_validate(
            {**data, "launch_paths": [{**launch_path, "text_param": ""}]}
        )


def test_a_launch_path_is_a_get_with_no_presets_and_no_draft_param_unless_it_says_otherwise() -> (
    None
):
    launch_path = AppManifest.model_validate(_full_manifest_data()).launch_paths[0]
    assert launch_path.method is LaunchPathMethod.GET
    assert launch_path.presets == {}
    assert launch_path.draft_param is None


def test_a_post_launch_path_carries_its_method_presets_and_draft_param() -> None:
    data = _full_manifest_data()
    launch_path = {
        **_launch_path_data(),
        "path": "/api/intake",
        "method": "POST",
        "presets": {"target": "current_chat", "is_draft": "true"},
        "text_param": None,
        "draft_param": "path",
    }
    manifest = AppManifest.model_validate({**data, "launch_paths": [launch_path]})
    assert manifest.launch_paths[0].method is LaunchPathMethod.POST
    assert manifest.launch_paths[0].presets == {
        "target": "current_chat",
        "is_draft": "true",
    }
    assert manifest.launch_paths[0].draft_param == "path"
    assert manifest.launch_paths[0].text_param is None


@pytest.mark.parametrize("method", ["get", "post", "PUT", ""])
def test_a_launch_path_method_is_get_or_post_spelled_in_upper_case(method: str) -> None:
    data = _full_manifest_data()
    launch_path = {**_launch_path_data(), "method": method}
    with pytest.raises(ValidationError):
        AppManifest.model_validate({**data, "launch_paths": [launch_path]})


def test_a_launch_path_declares_at_most_one_of_text_param_draft_param_and_mention_param() -> (
    None
):
    data = _full_manifest_data()
    launch_path = {**_launch_path_data(), "draft_param": "path"}
    with pytest.raises(
        ValidationError,
        match="at most one of text_param, draft_param, mention_param, and app_params",
    ):
        AppManifest.model_validate({**data, "launch_paths": [launch_path]})
    with pytest.raises(
        ValidationError,
        match="at most one of text_param, draft_param, mention_param, and app_params",
    ):
        AppManifest.model_validate(
            {
                **data,
                "launch_paths": [
                    {**launch_path, "draft_param": None, "mention_param": "path"}
                ],
            }
        )
    with pytest.raises(
        ValidationError,
        match="draft_param 'message' is not one of the launch path's params",
    ):
        AppManifest.model_validate(
            {
                **data,
                "launch_paths": [
                    {**launch_path, "text_param": None, "draft_param": "message"}
                ],
            }
        )
    with pytest.raises(
        ValidationError,
        match="mention_param 'message' is not one of the launch path's params",
    ):
        AppManifest.model_validate(
            {
                **data,
                "launch_paths": [
                    {
                        **launch_path,
                        "text_param": None,
                        "draft_param": None,
                        "mention_param": "message",
                    }
                ],
            }
        )
    mentioning = AppManifest.model_validate(
        {
            **data,
            "launch_paths": [
                {
                    **launch_path,
                    "text_param": None,
                    "draft_param": None,
                    "mention_param": "path",
                }
            ],
        }
    )
    assert mentioning.launch_paths[0].mention_param == "path"


def test_a_launch_path_about_an_app_declares_both_app_params_among_its_params_and_nothing_else() -> (
    None
):
    """The ``app_params`` pair (the multiplayer spec, section 9.2) names two distinct params of the launch path, and
    stands alone like a text, draft, or mention param."""
    data = _full_manifest_data()
    params = [
        {"name": "app", "label": "App", "required": True},
        {"name": "app_display_name", "label": "App name", "required": True},
    ]
    about_an_app = {
        **_launch_path_data(),
        "text_param": None,
        "params": params,
        "app_params": {"name": "app", "display_name": "app_display_name"},
    }
    manifest = AppManifest.model_validate({**data, "launch_paths": [about_an_app]})
    assert manifest.launch_paths[0].app_params is not None
    assert manifest.launch_paths[0].app_params.display_name == "app_display_name"
    with pytest.raises(
        ValidationError,
        match="app_params.display_name 'title' is not one of the launch path's params",
    ):
        AppManifest.model_validate(
            {
                **data,
                "launch_paths": [
                    {
                        **about_an_app,
                        "app_params": {"name": "app", "display_name": "title"},
                    }
                ],
            }
        )
    with pytest.raises(
        ValidationError,
        match="at most one of text_param, draft_param, mention_param, and app_params",
    ):
        AppManifest.model_validate(
            {**data, "launch_paths": [{**about_an_app, "text_param": "app"}]}
        )
    with pytest.raises(
        ValidationError,
        match="names one param 'app' for both the name and the display name",
    ):
        AppManifest.model_validate(
            {
                **data,
                "launch_paths": [
                    {
                        **about_an_app,
                        "app_params": {"name": "app", "display_name": "app"},
                    }
                ],
            }
        )


def test_a_preset_may_not_shadow_a_param() -> None:
    data = _full_manifest_data()
    launch_path = {**_launch_path_data(), "presets": {"path": "/notes/"}}
    with pytest.raises(
        ValidationError, match="preset 'path' is also one of the launch path's params"
    ):
        AppManifest.model_validate({**data, "launch_paths": [launch_path]})


@pytest.mark.parametrize("reserved", sorted(RESERVED_LAUNCH_PARAM_NAMES))
def test_the_launch_envelopes_names_are_reserved_for_params_and_presets(
    reserved: str,
) -> None:
    data = _full_manifest_data()
    launch_path = _launch_path_data()
    with pytest.raises(
        ValidationError,
        match=f"{reserved!r} is reserved for the shell's launch envelope",
    ):
        AppManifest.model_validate(
            {**data, "launch_paths": [{**launch_path, "presets": {reserved: "x"}}]}
        )
    with pytest.raises(
        ValidationError,
        match=f"{reserved!r} is reserved for the shell's launch envelope",
    ):
        AppManifest.model_validate(
            {
                **data,
                "launch_paths": [
                    {
                        **launch_path,
                        "params": [
                            *_launch_path_params(),
                            {"name": reserved, "label": "Reserved"},
                        ],
                    }
                ],
            }
        )


def test_duplicate_launch_path_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="launch path ids must be unique"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "launch_paths": [
                    {"id": "new", "label": "New", "path": "/new"},
                    {"id": "new", "label": "Again", "path": "/again"},
                ],
            }
        )


def test_the_open_launch_path_id_is_reserved_for_the_synthesized_root() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "launch_paths": [{"id": "open", "label": "Open", "path": "/"}],
            }
        )


@pytest.mark.parametrize(
    "launch_path_id", ["New", "-new", "", "a" * 33, "new tab", "new\n"]
)
def test_launch_path_ids_follow_the_id_rule(launch_path_id: str) -> None:
    with pytest.raises(ValidationError, match="invalid launch path id"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "launch_paths": [
                    {"id": launch_path_id, "label": "New", "path": "/new"}
                ],
            }
        )


@pytest.mark.parametrize(
    "path",
    [
        "new",
        "//new",
        "/new?message=hi",
        "/new#top",
        "/new one",
        "/new\t",
        "/a%20b",
        '/a"b',
        "/café",
        "/" + "a" * 2048,
    ],
)
def test_launch_path_values_are_rooted_query_free_and_unescaped(path: str) -> None:
    with pytest.raises(ValidationError, match="invalid launch path"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "launch_paths": [{"id": "new", "label": "New", "path": path}],
            }
        )


@pytest.mark.parametrize(
    "path", ["/", "/new", "/folders/inbox", "/a-b_c.d~", "/a:b@c!$&'()*+,;="]
)
def test_launch_path_values_that_follow_the_rule_are_accepted(path: str) -> None:
    manifest = AppManifest.model_validate(
        {
            "name": "news",
            "display_name": "News",
            "icon": "icon.svg",
            "launch_paths": [{"id": "new", "label": "New", "path": path}],
        }
    )
    assert manifest.launch_paths[0].path == path


def test_default_shortcut_launch_must_name_a_declared_launch_path() -> None:
    with pytest.raises(ValidationError, match="default_shortcut.launch"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "launch_paths": [{"id": "new", "label": "New", "path": "/new"}],
                "default_shortcut": {"launch": "other", "mode": "new"},
            }
        )


def test_default_shortcut_launch_open_is_allowed_only_without_declared_launch_paths() -> (
    None
):
    rooted = AppManifest.model_validate(
        {
            "name": "news",
            "display_name": "News",
            "icon": "icon.svg",
            "default_shortcut": {"launch": "open", "mode": "focus"},
        }
    )
    assert [shortcut.launch for shortcut in rooted.default_shortcuts] == ["open"]

    with pytest.raises(ValidationError, match="default_shortcut.launch"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "launch_paths": [{"id": "new", "label": "New", "path": "/new"}],
                "default_shortcut": {"launch": "open", "mode": "focus"},
            }
        )


def _two_launch_path_manifest_data(default_shortcut: object) -> dict[str, object]:
    return {
        "name": "screen",
        "display_name": "Screen",
        "icon": "icon.svg",
        "launch_paths": [
            {"id": "screen", "label": "Screen", "path": "/"},
            {"id": "browser", "label": "Browser", "path": "/launch/browser", "method": "POST"},
        ],
        "default_shortcut": default_shortcut,
    }


def test_an_array_of_default_shortcuts_seeds_each_in_order() -> None:
    manifest = AppManifest.model_validate(
        _two_launch_path_manifest_data(
            [{"launch": "screen", "mode": "focus"}, {"launch": "browser", "mode": "new"}]
        )
    )

    assert [(shortcut.launch, shortcut.mode) for shortcut in manifest.default_shortcuts] == [
        ("screen", ShortcutMode.FOCUS),
        ("browser", ShortcutMode.NEW),
    ]


@pytest.mark.parametrize(
    ("default_shortcut", "message"),
    [
        ([], "at least one shortcut"),
        (
            [{"launch": "screen", "mode": "focus"}, {"launch": "screen", "mode": "new"}],
            "launches must be unique",
        ),
        (
            [{"launch": "screen", "mode": "focus"}, {"launch": "other", "mode": "new"}],
            "default_shortcut.launch 'other'",
        ),
    ],
)
def test_an_array_of_default_shortcuts_is_checked_shortcut_by_shortcut(
    default_shortcut: object, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        AppManifest.model_validate(_two_launch_path_manifest_data(default_shortcut))


def test_default_shortcut_requires_a_launch() -> None:
    with pytest.raises(ValidationError, match="launch"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "default_shortcut": {"mode": "focus"},
            }
        )


@pytest.mark.parametrize("rank", [0, -3, "ten", 1.5])
def test_launcher_rank_must_be_a_positive_integer(rank: object) -> None:
    with pytest.raises(ValidationError):
        AppManifest.model_validate({**_full_manifest_data(), "launcher_rank": rank})


def test_minimal_manifest_takes_the_documented_defaults() -> None:
    manifest = AppManifest.model_validate({"name": "news", "display_name": "News", "icon": "icon.svg"})

    assert manifest.critical is False
    assert manifest.priority == "user"
    assert manifest.program == "news"
    assert manifest.internal is False
    assert manifest.default_shortcuts == ()
    assert manifest.launch_paths == ()
    assert manifest.launcher_rank is None
    assert manifest.pin is None
    assert manifest.window_closed_path is None


def test_program_defaults_to_the_name_but_an_explicit_program_wins() -> None:
    manifest = AppManifest.model_validate(
        {
            "name": "news",
            "display_name": "News",
            "icon": "icon.svg",
            "program": "news-server",
        }
    )

    assert manifest.program == "news-server"


@pytest.mark.parametrize(
    "name",
    [
        "MyApp",
        "host-abc",
        "agent-abc",
        "-leading",
        "trailing-",
        "double--hyphen",
        "",
        "dot.name",
        "localhost",
        "auth",
        "a" * 33,
        "news\n",
    ],
)
def test_invalid_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError, match="invalid app name"):
        AppManifest.model_validate({"name": name, "display_name": "X", "icon": "icon.svg"})


@pytest.mark.parametrize(
    "name",
    ["terminal", "my-app", "app2", "a", "system_interface", "openvscode-server-4"],
)
def test_valid_names_are_accepted(name: str) -> None:
    assert AppManifest.model_validate({"name": name, "display_name": "X", "icon": "icon.svg"}).name == name


@pytest.mark.parametrize("display_name", ["", "   ", "x" * 65])
def test_display_name_must_be_non_empty_and_at_most_64_characters(
    display_name: str,
) -> None:
    with pytest.raises(ValidationError, match="display_name"):
        AppManifest.model_validate({"name": "news", "display_name": display_name, "icon": "icon.svg"})


def test_icon_is_required_unless_internal() -> None:
    with pytest.raises(ValidationError, match="icon is required"):
        AppManifest.model_validate({"name": "news", "display_name": "News"})

    internal = AppManifest.model_validate({"name": "owner-exec", "display_name": "Owner exec", "internal": True})
    assert internal.icon is None


@pytest.mark.parametrize("icon", ["icon.png", "/abs/icon.svg", "icon"])
def test_icon_must_be_a_relative_svg_path(icon: str) -> None:
    with pytest.raises(ValidationError, match="invalid icon"):
        AppManifest.model_validate({"name": "news", "display_name": "News", "icon": icon})


def test_default_shortcut_mode_must_be_focus_or_new() -> None:
    with pytest.raises(ValidationError, match="mode"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "default_shortcut": {"launch": "open", "mode": "always"},
            }
        )


def test_a_manifest_that_still_carries_the_retired_handles_table_fails_to_load() -> (
    None
):
    with pytest.raises(ValidationError, match="handles"):
        AppManifest.model_validate(
            {"name": "news", "display_name": "News", "icon": "icon.svg", "handles": {}}
        )


_NEWS_MANIFEST_HEAD = 'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n'
_NEWS_LAUNCH_PATH = '[[launch_paths]]\nid = "new"\nlabel = "News"\npath = "/"\n'

# Each a manifest carrying one key the models do not declare, the line that declares it, and where it sits.
_UNKNOWN_KEY_MANIFESTS = (
    pytest.param(
        _NEWS_MANIFEST_HEAD + 'instance_lifetime = "explicit"\n',
        'instance_lifetime = "explicit"\n',
        "instance_lifetime",
        id="top-level",
    ),
    pytest.param(
        _NEWS_MANIFEST_HEAD + "[handles]\nopen_url = true\n",
        "[handles]\nopen_url = true\n",
        "handles",
        id="retired-handles-table",
    ),
    pytest.param(
        _NEWS_MANIFEST_HEAD + '[scope]\nexclude = ["docs/generated/**"]\nfuture_rule = true\n',
        "future_rule = true\n",
        "scope.future_rule",
        id="table",
    ),
    pytest.param(
        _NEWS_MANIFEST_HEAD + '[default_shortcut]\nlaunch = "open"\nmode = "focus"\nfuture_flag = true\n',
        "future_flag = true\n",
        "default_shortcut.0.future_flag",
        id="one-table-read-as-an-array",
    ),
    pytest.param(
        _NEWS_MANIFEST_HEAD
        + '[[launch_paths]]\nid = "old"\nlabel = "Old news"\npath = "/old"\n'
        + _NEWS_LAUNCH_PATH
        + "future_flag = true\n",
        "future_flag = true\n",
        "launch_paths.1.future_flag",
        id="array-of-tables-entry",
    ),
    pytest.param(
        _NEWS_MANIFEST_HEAD
        + _NEWS_LAUNCH_PATH
        + '[[launch_paths.params]]\nname = "topic"\nlabel = "Topic"\nfuture_flag = true\n',
        "future_flag = true\n",
        "launch_paths.0.params.0.future_flag",
        id="nested-array-of-tables-entry",
    ),
)


def _write_manifest_beside_its_icon(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "icon.svg").write_text(APP_ICON_MARKUP, encoding="utf-8")
    manifest_path = directory / "app.toml"
    manifest_path.write_text(body, encoding="utf-8")
    return manifest_path


@pytest.mark.parametrize(("body", "unknown_key_line", "unknown_key"), _UNKNOWN_KEY_MANIFESTS)
def test_load_manifest_refuses_an_unknown_key_in_the_strict_mode(
    tmp_path: Path, body: str, unknown_key_line: str, unknown_key: str
) -> None:
    manifest_path = _write_manifest_beside_its_icon(tmp_path / "news", body)

    with captured_warnings() as warnings:
        with pytest.raises(ManifestLoadError, match=f"field '{unknown_key}': Extra inputs are not permitted"):
            load_manifest(manifest_path, mode=ManifestReadMode.STRICT)

    assert warnings == []


@pytest.mark.parametrize(("body", "unknown_key_line", "unknown_key"), _UNKNOWN_KEY_MANIFESTS)
def test_load_manifest_warns_about_an_unknown_key_and_ignores_it_by_default(
    tmp_path: Path, body: str, unknown_key_line: str, unknown_key: str
) -> None:
    manifest_path = _write_manifest_beside_its_icon(tmp_path / "news", body)
    without_the_key = _write_manifest_beside_its_icon(tmp_path / "plain", body.replace(unknown_key_line, ""))

    with captured_warnings() as warnings:
        manifest = load_manifest(manifest_path)

    # The key alone is dropped: everything the manifest declares beside it reads as it does without the key.
    assert manifest == load_manifest(without_the_key, mode=ManifestReadMode.STRICT)
    assert manifest.name == "news"
    assert len(warnings) == 1
    assert f"unknown key '{unknown_key}'" in warnings[0]
    assert str(manifest_path) in warnings[0]


def test_load_manifest_reads_the_fields_around_ignored_keys(tmp_path: Path) -> None:
    manifest_path = _write_manifest_beside_its_icon(
        tmp_path,
        _NEWS_MANIFEST_HEAD
        + 'future_field = "0.3.0"\n'
        + '[default_shortcut]\nlaunch = "new"\nmode = "focus"\nfuture_flag = true\n'
        + _NEWS_LAUNCH_PATH
        + 'future_flag = true\ntext_param = "topic"\n'
        + '[[launch_paths.params]]\nname = "topic"\nlabel = "Topic"\nfuture_hint = "a subject"\nrequired = true\n'
        + '[scope]\nfuture_rule = true\nexclude = ["docs/generated/**"]\n'
        + '[future_table]\nexclude = ["docs/internal/**"]\n',
    )

    with captured_warnings() as warnings:
        manifest = load_manifest(manifest_path, mode=ManifestReadMode.TOLERANT)

    assert [(shortcut.launch, shortcut.mode) for shortcut in manifest.default_shortcuts] == [
        ("new", ShortcutMode.FOCUS)
    ]
    assert [(entry.id, entry.label, entry.path, entry.text_param) for entry in manifest.launch_paths] == [
        ("new", "News", "/", "topic")
    ]
    assert [(param.name, param.label, param.required) for param in manifest.launch_paths[0].params] == [
        ("topic", "Topic", True)
    ]
    assert manifest.scope.exclude == ("docs/generated/**",)
    assert sorted(warning.partition("unknown key ")[2].split()[0] for warning in warnings) == [
        "'default_shortcut.0.future_flag'",
        "'future_field'",
        "'future_table'",
        "'launch_paths.0.future_flag'",
        "'launch_paths.0.params.0.future_hint'",
        "'scope.future_rule'",
    ]


@pytest.mark.parametrize(("body", "unknown_key_line", "unknown_key"), _UNKNOWN_KEY_MANIFESTS)
def test_load_manifest_still_refuses_an_invalid_value_beside_an_unknown_key(
    tmp_path: Path, body: str, unknown_key_line: str, unknown_key: str
) -> None:
    invalid_body = body.replace('display_name = "News"', 'display_name = ""')
    manifest_path = _write_manifest_beside_its_icon(tmp_path, invalid_body)

    with pytest.raises(ManifestLoadError, match="field 'display_name'"):
        load_manifest(manifest_path, mode=ManifestReadMode.TOLERANT)


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        pytest.param(
            _NEWS_MANIFEST_HEAD + _NEWS_LAUNCH_PATH + 'future_flag = true\nmethod = "PUT"\n',
            "field 'launch_paths.0.method'",
            id="a-bad-value-in-the-entry-that-carries-the-key",
        ),
        pytest.param(
            'name = "news"\nicon = "icon.svg"\nfuture_flag = true\n',
            "field 'display_name': Field required",
            id="a-missing-required-field",
        ),
        pytest.param(
            _NEWS_MANIFEST_HEAD + "future_flag = true\nlauncher_rank = 0\n",
            "field 'launcher_rank'",
            id="a-value-out-of-range",
        ),
        pytest.param(
            _NEWS_MANIFEST_HEAD + "future_flag = true\ncritical = true\nstop_when_no_windows = true\n",
            "a critical app cannot declare stop_when_no_windows",
            id="a-cross-field-rule",
        ),
        pytest.param(
            _NEWS_MANIFEST_HEAD
            + '[theming]\nfuture_flag = true\n[[theming.parts]]\nname = "card"\ndescription = "A story"\n',
            "theming parts are declared only with mode",
            id="a-cross-field-rule-of-the-table-that-carries-the-key",
        ),
    ],
)
def test_the_tolerant_mode_applies_every_other_rule(tmp_path: Path, body: str, problem: str) -> None:
    manifest_path = _write_manifest_beside_its_icon(tmp_path, body)

    with pytest.raises(ManifestLoadError, match=problem):
        load_manifest(manifest_path, mode=ManifestReadMode.TOLERANT)


def test_the_tolerant_mode_still_checks_the_icon_and_the_references(tmp_path: Path) -> None:
    manifest_path = write_app_manifest(
        tmp_path,
        "news",
        _NEWS_MANIFEST_HEAD + 'future_flag = true\n[[references]]\npath = "docs/system/news.md"\n',
        is_icon_written=False,
    )

    with pytest.raises(ManifestLoadError, match="names icon 'icon.svg', which does not exist"):
        load_manifest(manifest_path, mode=ManifestReadMode.TOLERANT)
    (manifest_path.parent / "icon.svg").write_text(APP_ICON_MARKUP, encoding="utf-8")
    with pytest.raises(ManifestLoadError, match="docs/system/news.md"):
        load_manifest(manifest_path, mode=ManifestReadMode.TOLERANT)


def test_load_manifest_reads_a_file_and_resolves_its_icon(tmp_path: Path) -> None:
    app_dir = tmp_path / uuid4().hex
    app_dir.mkdir()
    (app_dir / "icon.svg").write_text(APP_ICON_MARKUP)
    manifest_path = app_dir / "app.toml"
    manifest_path.write_text('name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n')

    manifest = load_manifest(manifest_path)

    assert manifest.name == "news"
    assert manifest_icon_path(manifest_path, manifest) == app_dir / "icon.svg"


def test_load_manifest_reports_a_missing_icon_file(tmp_path: Path) -> None:
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text('name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n')

    with pytest.raises(ManifestLoadError, match="does not exist"):
        load_manifest(manifest_path)


def test_load_manifest_reports_invalid_toml_and_invalid_values(tmp_path: Path) -> None:
    bad_toml = tmp_path / "bad.toml"
    bad_toml.write_text("name = \n")
    with pytest.raises(ManifestLoadError, match="not valid TOML"):
        load_manifest(bad_toml)

    bad_value = tmp_path / "value.toml"
    bad_value.write_text('name = "news"\ndisplay_name = ""\nicon = "icon.svg"\n')
    with pytest.raises(ManifestLoadError, match="display_name"):
        load_manifest(bad_value)


def test_load_manifest_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestLoadError, match="cannot read"):
        load_manifest(tmp_path / "nope.toml")


def test_load_manifest_reports_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_bytes(b'name = "news"\ndisplay_name = "\xff"\n')

    with pytest.raises(ManifestLoadError, match="cannot read"):
        load_manifest(manifest_path)


def _manifest_with_preview(preview: dict[str, object]) -> AppManifest:
    return AppManifest.model_validate(
        {
            "name": "news-feed",
            "display_name": "News",
            "icon": "icon.svg",
            "preview": preview,
        }
    )


def test_a_manifest_without_a_preview_table_previews_by_the_scaffold_convention() -> None:
    # The scaffold binds <PACKAGE_UPPER>_PORT/_HOST and reads <PACKAGE_UPPER>_DATA_DIR, with the
    # package name being the app name with hyphens as underscores.
    manifest = AppManifest.model_validate({"name": "news-feed", "display_name": "News", "icon": "icon.svg"})

    assert manifest.preview == scaffold_preview_spec(AppName("news-feed"))
    assert manifest.preview.env == {
        "NEWS_FEED_PORT": "{port:main}",
        "NEWS_FEED_HOST": "{host}",
        "NEWS_FEED_DATA_DIR": "{copy:data}",
    }
    assert manifest.preview.copies == {"data": "data/.apps/news-feed"}
    assert manifest.preview.command == ()
    assert manifest.preview.ports == ("main",)
    assert manifest.preview.health_path == "/health"
    assert manifest.preview.open_path == "/"


def test_an_explicit_preview_table_says_what_it_needs_and_nothing_else() -> None:
    manifest = _manifest_with_preview(
        {
            "ports": ["main", "sidecar"],
            "command": [
                "news-server",
                "--events-url",
                "http://127.0.0.1:{port:sidecar}",
            ],
            "env": {
                "NEWS_PORT": "{port:main}",
                "NEWS_STORE": "{copy:store}/records.json",
            },
            "copies": {"store": "data/.apps/news-feed"},
            "health_path": "/api/health",
            "open_path": "/{key}",
            "open_path_takes_key": True,
        }
    )

    assert manifest.preview.ports == ("main", "sidecar")
    assert manifest.preview.command[0] == "news-server"
    assert manifest.preview.copies == {"store": "data/.apps/news-feed"}
    assert manifest.preview.open_path_takes_key is True


@pytest.mark.parametrize(
    ("preview", "problem"),
    [
        ({"env": {"NEWS_PORT": "{port:sidecar}"}}, "port 'sidecar'"),
        ({"args": ["--store", "{copy:store}"]}, "copy 'store'"),
        ({"command": ["news-server", "{worktree}"]}, "unknown placeholder"),
        ({"env": {"X": "{host:main}"}}, "takes no name"),
        ({"ports": ["sidecar"]}, "must include 'main'"),
        ({"ports": ["main", "main"]}, "unique"),
        ({"copies": {"data": "/etc"}}, "repo-relative"),
        ({"copies": {"data": "../outside"}}, "repo-relative"),
        ({"open_path": "/{key}"}, "open_path_takes_key"),
        ({"open_path": "/", "open_path_takes_key": True}, "open_path_takes_key"),
        ({"open_path": "/{port:main}"}, "may carry only"),
        ({"health_path": "health"}, "start with '/'"),
    ],
)
def test_a_preview_table_that_cannot_be_resolved_is_rejected(preview: dict[str, object], problem: str) -> None:
    with pytest.raises(ValidationError, match=problem):
        _manifest_with_preview(preview)


def test_the_scaffold_preview_spec_validates_its_own_placeholders() -> None:
    # The convention is itself a table, so it obeys the rules every table does.
    spec = scaffold_preview_spec(AppName("news"))

    assert PreviewSpec.model_validate(spec.model_dump()) == spec


# references and scope

_REFERENCING_MANIFEST = """
name = "news"
display_name = "News"
icon = "icon.svg"

[[references]]
path = ".agents/skills/news-refresh"
note = "Fetches stories on a schedule; calls POST /api/ingest"

[[references]]
path = "system/scripts/run_news.sh"

[scope]
exclude = ["system/apps/news/frontend/dist/**"]
"""


def test_wiring_programs_are_unique_and_never_the_apps_own() -> None:
    base = {"name": "news", "display_name": "News", "icon": "icon.svg"}

    assert AppManifest.model_validate(
        {**base, "wiring": {"programs": ["xvfb"]}}
    ).wiring.programs == ("xvfb",)
    with pytest.raises(ValidationError, match="unique"):
        AppManifest.model_validate({**base, "wiring": {"programs": ["xvfb", "xvfb"]}})
    with pytest.raises(ValidationError, match="own program"):
        AppManifest.model_validate({**base, "wiring": {"programs": ["news"]}})


def test_a_manifest_reads_its_references_and_scope() -> None:
    manifest = AppManifest.model_validate(
        {
            "name": "news",
            "display_name": "News",
            "icon": "icon.svg",
            "references": [
                {
                    "path": ".agents/skills/news-refresh",
                    "note": "Drives the ingest route",
                },
                {"path": "system/scripts/run_news.sh"},
            ],
            "scope": {"exclude": ["system/apps/news/frontend/dist/**"]},
        }
    )

    assert [reference.path for reference in manifest.references] == [
        ".agents/skills/news-refresh",
        "system/scripts/run_news.sh",
    ]
    assert manifest.references[0].note == "Drives the ingest route"
    assert manifest.references[1].note is None
    assert manifest.scope.exclude == ("system/apps/news/frontend/dist/**",)


def test_a_manifest_without_references_or_scope_takes_the_empty_defaults() -> None:
    manifest = AppManifest.model_validate({"name": "news", "display_name": "News", "icon": "icon.svg"})

    assert manifest.references == ()
    assert manifest.scope.exclude == ()


@pytest.mark.parametrize(
    "reference_path",
    [
        "",
        "   ",
        "/etc/passwd",
        "../outside",
        "system/../../escape",
        ".agents/skills/*/run.py",
        "docs/system/?.md",
        "docs/[abc].md",
        "docs/a]b.md",
        ".agents/skills/news-refresh/",
        "docs\\windows.md",
        "system/vendor/mngr/libs/mngr",
        "data/.apps/news",
    ],
)
def test_reference_paths_that_are_not_literal_repo_relative_paths_are_rejected(
    reference_path: str,
) -> None:
    with pytest.raises(ValidationError):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "references": [{"path": reference_path}],
            }
        )


@pytest.mark.parametrize(
    ("reference_path", "expected_problem"),
    [
        ("./docs/news.md", "'.' segments"),
        ("docs/./news.md", "'.' segments"),
        ("docs/news/.", "'.' segments"),
        ("docs//news.md", "empty segments"),
    ],
)
def test_a_reference_path_with_a_dot_or_an_empty_segment_is_rejected(
    reference_path: str, expected_problem: str
) -> None:
    # git names a changed file in normalized form, so a path with a hole in it never matches
    # anything the diff reports, however real the artifact at the end of it is.
    with pytest.raises(ValidationError, match=expected_problem):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "references": [{"path": reference_path}],
            }
        )


def test_a_reference_path_naming_a_glob_says_so() -> None:
    with pytest.raises(ValidationError, match="glob character"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "references": [{"path": "docs/**/news.md"}],
            }
        )


@pytest.mark.parametrize(
    "note", ["", "   ", "two\nlines", "a carriage\rreturn", "x" * 201]
)
def test_a_reference_note_must_be_one_non_empty_line(note: str) -> None:
    with pytest.raises(ValidationError, match="note"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "references": [{"path": "docs/news.md", "note": note}],
            }
        )


def test_duplicate_reference_paths_are_rejected() -> None:
    with pytest.raises(ValidationError, match="reference paths must be unique"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "references": [
                    {"path": "docs/news.md"},
                    {"path": "docs/news.md", "note": "the same artifact again"},
                ],
            }
        )


@pytest.mark.parametrize("exclude_glob", ["", "  ", "/abs/**", "../outside/**", "system\\apps\\**"])
def test_exclude_globs_must_be_repo_relative(exclude_glob: str) -> None:
    with pytest.raises(ValidationError, match="exclude glob"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "scope": {"exclude": [exclude_glob]},
            }
        )


def test_an_exclude_glob_cannot_negate_its_way_past_the_built_in_excludes() -> None:
    with pytest.raises(ValidationError, match="denylist"):
        AppManifest.model_validate(
            {
                "name": "news",
                "display_name": "News",
                "icon": "icon.svg",
                "scope": {"exclude": ["!system/vendor/mngr/**"]},
            }
        )


def test_load_manifest_derives_the_repo_root_from_the_apps_layout(tmp_path: Path) -> None:
    manifest_path = write_app_manifest(tmp_path, "news", _REFERENCING_MANIFEST, is_icon_written=True)
    write_repo_file(tmp_path, ".agents/skills/news-refresh/SKILL.md", "# refresh\n")
    write_repo_file(tmp_path, "system/scripts/run_news.sh", "#!/bin/sh\n")

    manifest = load_manifest(manifest_path)

    assert [reference.path for reference in manifest.references] == [
        ".agents/skills/news-refresh",
        "system/scripts/run_news.sh",
    ]


def test_load_manifest_rejects_a_reference_to_something_that_does_not_exist(
    tmp_path: Path,
) -> None:
    manifest_path = write_app_manifest(tmp_path, "news", _REFERENCING_MANIFEST, is_icon_written=True)
    write_repo_file(tmp_path, ".agents/skills/news-refresh/SKILL.md", "# refresh\n")

    with pytest.raises(ManifestLoadError, match="run_news.sh"):
        load_manifest(manifest_path, repo_root=tmp_path)


def test_load_manifest_rejects_a_reference_inside_the_apps_own_directory(
    tmp_path: Path,
) -> None:
    manifest_path = write_app_manifest(
        tmp_path,
        "news",
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n'
        '[[references]]\npath = "system/apps/news/runner.py"\n',
        is_icon_written=True,
    )
    write_repo_file(tmp_path, "system/apps/news/runner.py", "\n")

    with pytest.raises(ManifestLoadError, match="own directory"):
        load_manifest(manifest_path, repo_root=tmp_path)


def test_load_manifest_rejects_a_reference_to_another_apps_directory(
    tmp_path: Path,
) -> None:
    manifest_path = write_app_manifest(
        tmp_path,
        "news",
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n[[references]]\npath = "system/apps/files/src"\n',
        is_icon_written=True,
    )
    write_repo_file(tmp_path, "system/apps/files/src/runner.py", "\n")

    with pytest.raises(ManifestLoadError, match="another app"):
        load_manifest(manifest_path, repo_root=tmp_path)


def test_load_manifest_accepts_a_reference_to_a_file_directly_under_the_apps_directory(
    tmp_path: Path,
) -> None:
    # system/apps/README.md sits beside the app directories without being one, so the
    # other-app rule (which keys on the directory a path enters) leaves it alone.
    manifest_path = write_app_manifest(
        tmp_path,
        "news",
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n[[references]]\npath = "system/apps/README.md"\n',
        is_icon_written=True,
    )
    write_repo_file(tmp_path, "system/apps/README.md", "# apps\n")

    manifest = load_manifest(manifest_path, repo_root=tmp_path)

    assert [reference.path for reference in manifest.references] == [
        "system/apps/README.md"
    ]


def test_load_manifest_rejects_a_reference_that_goes_through_a_symlinked_directory(
    tmp_path: Path,
) -> None:
    manifest_path = write_app_manifest(
        tmp_path,
        "news",
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n'
        '[[references]]\npath = ".claude/skills/news-refresh"\n',
        is_icon_written=True,
    )
    write_repo_file(tmp_path, ".agents/skills/news-refresh/SKILL.md", "# refresh\n")
    # The repo's own .claude/skills is a tracked symlink to .agents/skills, and git reports a
    # changed file only under the real directory, so a reference through it covers nothing.
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "skills").symlink_to(Path("..") / ".agents" / "skills")

    with pytest.raises(ManifestLoadError, match="symlink"):
        load_manifest(manifest_path, repo_root=tmp_path)


def test_load_manifest_off_the_apps_layout_skips_the_location_rules_until_a_root_is_given(
    tmp_path: Path,
) -> None:
    loose_directory = tmp_path / uuid4().hex
    loose_directory.mkdir()
    (loose_directory / "icon.svg").write_text(APP_ICON_MARKUP)
    manifest_path = loose_directory / "app.toml"
    manifest_path.write_text(_REFERENCING_MANIFEST)

    assert len(load_manifest(manifest_path).references) == 2

    with pytest.raises(ManifestLoadError, match="does not exist"):
        load_manifest(manifest_path, repo_root=tmp_path)


def test_secret_declarations_round_trip_and_reject_a_repeated_file_or_an_empty_variable_list() -> (
    None
):
    data = {
        **_full_manifest_data(),
        "secrets": [
            {
                "file": "widget",
                "variables": ["WIDGET_TOKEN", "WIDGET_ORG"],
                "note": "a Widget API token",
            },
            {"file": "mailer", "variables": ["SMTP_PASSWORD"]},
        ],
    }
    manifest = AppManifest.model_validate(data)
    assert [
        (str(secret.file), tuple(secret.variables)) for secret in manifest.secrets
    ] == [
        ("widget", ("WIDGET_TOKEN", "WIDGET_ORG")),
        ("mailer", ("SMTP_PASSWORD",)),
    ]
    assert manifest.secrets[1].note is None
    for bad in (
        [
            {"file": "widget", "variables": ["A"]},
            {"file": "widget", "variables": ["B"]},
        ],
        [{"file": "widget", "variables": []}],
        [{"file": "widget", "variables": ["A", "A"]}],
        [{"file": "Widget", "variables": ["A"]}],
        [{"file": "widget", "variables": ["1A"]}],
    ):
        with pytest.raises(ValidationError):
            AppManifest.model_validate({**_full_manifest_data(), "secrets": bad})


def test_theming_defaults_to_tokens_with_no_parts() -> None:
    manifest = AppManifest.model_validate(_full_manifest_data())

    assert manifest.theming.mode == ThemingMode.TOKENS
    assert manifest.theming.parts == ()


def test_theming_parts_declare_the_elements_a_theme_may_style() -> None:
    data = {
        **_full_manifest_data(),
        "theming": {
            "mode": "parts",
            "parts": [
                {"name": "user-message", "description": "A message the user sent"},
                {"name": "composer", "description": "The box a message is typed in"},
            ],
        },
    }

    manifest = AppManifest.model_validate(data)

    assert manifest.theming.mode == ThemingMode.PARTS
    assert [str(part.name) for part in manifest.theming.parts] == ["user-message", "composer"]


@pytest.mark.parametrize(
    ("theming", "match"),
    [
        ({"mode": "tokens", "parts": [{"name": "row", "description": "A row"}]}, "only with mode"),
        ({"mode": "parts", "parts": [{"name": "Row", "description": "A row"}]}, "lowercase"),
        (
            {"mode": "parts", "parts": [{"name": "row", "description": "A"}, {"name": "row", "description": "B"}]},
            "unique",
        ),
        ({"mode": "parts", "parts": [{"name": "row", "description": "A row\nof the list"}]}, "one line"),
        ({"mode": "skins"}, "mode"),
    ],
)
def test_theming_refuses_misdeclared_parts_and_unknown_modes(theming: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        AppManifest.model_validate({**_full_manifest_data(), "theming": theming})


# How an app runs and what it needs: the fields a self-contained app directory declares.

_NOTES_MANIFEST_HEAD = 'name = "notes"\ndisplay_name = "Notes"\nicon = "icon.svg"\n'
_NOTES_SELF_CONTAINED_MANIFEST = (
    _NOTES_MANIFEST_HEAD
    + """
version = "0.3.0"
verified_template = "minds-v0.11.0"

[run]
command = "notes"
args = ["--listen", "{host}:{port:main}"]
env = { NOTES_PORT = "{port:main}" }

[[services]]
role = "indexer"
command = "notes-indexer"
env = { NOTES_URL = "http://{host}:{port:main}" }

[[permissions]]
scope = "slack-api"
permission = "slack-read-all"
note = "Reads the channels the digest covers"

[llm]
method = "keyed"

[environment]
apt = ["poppler-utils"]
uv_tools = { ruff = "0.6.9" }

[[dependencies]]
name = "datalib"
repo_url = "https://github.com/example/datalib"

[[adaptation]]
summary = "The digest channel is hardcoded"
resolution = "Ask which channel to read"

[publish]
exclude = ["docs/internal/**"]
"""
)


def test_a_manifest_says_how_its_programs_run_and_what_an_adopter_needs(tmp_path: Path) -> None:
    manifest_path = _write_manifest_beside_its_icon(tmp_path, _NOTES_SELF_CONTAINED_MANIFEST)

    manifest = load_manifest(manifest_path, mode=ManifestReadMode.STRICT)

    assert manifest.version == "0.3.0"
    assert manifest.verified_template == "minds-v0.11.0"
    assert manifest.run is not None
    assert manifest.run.command == "notes"
    assert manifest.run.args == ("--listen", "{host}:{port:main}")
    assert manifest.run.env == {"NOTES_PORT": "{port:main}"}
    assert [(service.role, service.command) for service in manifest.services] == [("indexer", "notes-indexer")]
    assert [(note.scope, note.permission) for note in manifest.permissions] == [("slack-api", "slack-read-all")]
    assert manifest.llm is not None
    assert manifest.llm.method == "keyed"
    assert manifest.environment.apt == ("poppler-utils",)
    assert manifest.environment.uv_tools == {"ruff": "0.6.9"}
    assert [(dependency.name, dependency.repo_url) for dependency in manifest.dependencies] == [
        ("datalib", "https://github.com/example/datalib")
    ]
    assert [note.summary for note in manifest.adaptation] == ["The digest channel is hardcoded"]
    assert manifest.publish.exclude == ("docs/internal/**",)


def test_a_manifest_that_declares_none_of_them_runs_the_scaffolds_way(tmp_path: Path) -> None:
    manifest = load_manifest(_write_manifest_beside_its_icon(tmp_path, _NOTES_MANIFEST_HEAD))

    assert manifest.run is None
    assert manifest.services == ()
    assert manifest.version is None
    assert manifest.verified_template is None
    assert manifest.permissions == ()
    assert manifest.llm is None
    assert manifest.environment.apt == ()
    assert manifest.dependencies == ()
    assert manifest.adaptation == ()
    assert manifest.publish.exclude == ()


@pytest.mark.parametrize(
    ("addition", "problem"),
    [
        pytest.param('[run]\nargs = ["{copy:data}"]\n', "takes only", id="run-arg-with-a-copy-placeholder"),
        pytest.param('[run]\nenv = { NOTES_STATE = "{scratch}" }\n', "takes only", id="run-env-with-scratch"),
        pytest.param('[run]\ncommand = "notes\\n--evil"\n', "single line", id="run-command-with-a-line-break"),
        pytest.param('[run]\ncommand = "npm start"\n', "run.args", id="run-command-carrying-its-arguments"),
        pytest.param(
            '[[services]]\nrole = "indexer"\ncommand = "node indexer.js"\n',
            "services.args",
            id="service-command-carrying-its-arguments",
        ),
        pytest.param('[run]\nenv = { "NOT A NAME" = "1" }\n', "env", id="run-env-name-that-is-no-variable"),
        pytest.param(
            '[run]\nenv = { NOTES_TITLE = "It\'s \\"notes\\"" }\n',
            "one kind of quote",
            id="run-env-value-with-both-quotes",
        ),
        pytest.param(
            '[run]\nenv = { NOTES_TITLE = "say \\"hi\\"" }\n',
            "one kind of quote",
            id="run-env-value-ending-in-a-quote",
        ),
        pytest.param(
            '[run]\nenv = { NOTES_TITLE = "Notes #1" }\n',
            "start of a comment",
            id="run-env-value-with-a-hash-after-a-space",
        ),
        pytest.param(
            '[[services]]\nrole = "indexer"\ncommand = "x"\nenv = { NOTES_ORDER = "first ;second" }\n',
            "start of a comment",
            id="service-env-value-with-a-semicolon-after-a-space",
        ),
        pytest.param('[[services]]\nrole = "Indexer"\ncommand = "x"\n', "service role", id="service-role-in-capitals"),
        pytest.param('[[services]]\nrole = "indexer"\n', "command", id="service-with-no-command"),
        pytest.param(
            '[[services]]\nrole = "indexer"\ncommand = "a"\n\n[[services]]\nrole = "indexer"\ncommand = "b"\n',
            "service roles must be unique",
            id="two-services-of-one-role",
        ),
        pytest.param('version = "1.2"\n', "semantic version", id="version-with-two-parts"),
        pytest.param('verified_template = "0.11.0"\n', "template release", id="template-version-without-its-prefix"),
        pytest.param('[publish]\nexclude = ["../other"]\n', "publish exclude", id="publish-exclude-leaving-the-app"),
        pytest.param('[publish]\nexclude = ["!keep.md"]\n', "publish exclude", id="publish-exclude-negated"),
        pytest.param(
            '[[dependencies]]\nname = "datalib"\n\n[[dependencies]]\nname = "datalib"\n',
            "dependency names must be unique",
            id="one-dependency-twice",
        ),
        pytest.param('[[permissions]]\nscope = "slack-api"\n', "permission", id="permission-with-no-permission"),
    ],
)
def test_a_program_or_publish_declaration_off_its_rule_fails_to_load(
    tmp_path: Path, addition: str, problem: str
) -> None:
    manifest_path = _write_manifest_beside_its_icon(tmp_path, _NOTES_MANIFEST_HEAD + addition)

    with pytest.raises(ManifestLoadError, match=problem):
        load_manifest(manifest_path, mode=ManifestReadMode.STRICT)


# An app with a record: its manifest's [[references]] and [wiring] are not read.

_REFERENCING_A_MISSING_PATH = (
    'name = "notes"\ndisplay_name = "Notes"\nicon = "icon.svg"\n'
    '\n[[references]]\npath = ".agents/skills/somewhere-else"\nnote = "Left in by the publisher"\n'
)


def _write_notes_record(repo_root: Path) -> None:
    write_record(repo_root, AppRecord(name=AppName("notes"), package="notes", port=8084, origin=AppOrigin.INSTALLED))


def test_a_reference_to_a_path_this_workspace_lacks_fails_the_load_of_an_app_with_no_record(tmp_path: Path) -> None:
    manifest_path = write_app_manifest(tmp_path, "notes", _REFERENCING_A_MISSING_PATH, is_icon_written=True)

    with pytest.raises(ManifestLoadError, match="does not exist"):
        load_manifest(manifest_path, repo_root=tmp_path)


def test_an_app_with_a_record_loads_whatever_its_manifest_still_references(tmp_path: Path) -> None:
    manifest_path = write_app_manifest(tmp_path, "notes", _REFERENCING_A_MISSING_PATH, is_icon_written=True)
    _write_notes_record(tmp_path)

    manifest = load_manifest(manifest_path, repo_root=tmp_path)

    assert manifest.name == "notes"


@pytest.mark.parametrize(
    "leftover",
    [
        pytest.param('\n[[references]]\npath = "docs/notes.md"\n', id="references"),
        pytest.param('\n[wiring]\nprograms = ["notes-display"]\n', id="wiring"),
    ],
)
def test_the_authoring_check_refuses_references_and_wiring_in_an_app_with_a_record(
    tmp_path: Path, leftover: str
) -> None:
    body = 'name = "notes"\ndisplay_name = "Notes"\nicon = "icon.svg"\n' + leftover
    manifest_path = write_app_manifest(tmp_path, "notes", body, is_icon_written=True)
    write_repo_file(tmp_path, "docs/notes.md", "# Notes\n")
    _write_notes_record(tmp_path)

    with pytest.raises(ManifestLoadError, match="an app with a record declares no"):
        load_manifest(manifest_path, repo_root=tmp_path, mode=ManifestReadMode.STRICT)
