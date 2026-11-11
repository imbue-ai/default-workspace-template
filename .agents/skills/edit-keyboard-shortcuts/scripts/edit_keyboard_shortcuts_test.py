from collections.abc import Sequence
from typing import Any

import pytest
from edit_keyboard_shortcuts import (
    EXIT_CLASH,
    EXIT_COULD_NOT_RUN,
    EXIT_OK,
    EXIT_REFUSED,
    Action,
    Catalog,
    Clash,
    Contract,
    CouldNotRunError,
    PartlyDoneError,
    RefusedError,
    Shell,
    UsageError,
    account_outcome,
    build_command_parser,
    build_report,
    chord_label,
    chords_taken_by_action,
    clash_candidates,
    find_clashes,
    find_refusals,
    find_warnings,
    imbue_studio_chords,
    is_workspace_wide_chord,
    main,
    normalize_chord,
    parse_catalog,
    parse_contract,
    render_report,
    requested_chords,
    run_check_or_save,
    run_reset,
)

_RAW_CONTRACT: dict[str, Any] = {
    "chord_modifier_order": ["ctrl", "alt", "shift", "meta"],
    "chord_named_keys": [
        "enter",
        "escape",
        "tab",
        "space",
        "delete",
        "plus",
        "arrowleft",
        "f4",
        "f6",
    ],
    "refused_chords": ["alt+meta+delete"],
    "browser_reserved_chords": {
        "mac": ["meta+w", "meta+q", "shift+meta+}"],
        "windows": ["meta+w", "meta+f4"],
        "linux": ["meta+w", "shift+meta+q"],
    },
    "os_kept_chords": {
        "mac": [{"chord": "meta+space", "reason": "Spotlight"}],
        "windows": [{"chord": "alt+tab", "reason": "the window switcher"}],
        "linux": [{"chord": "alt+tab", "reason": "the window switcher"}],
    },
    "host_acted_chords": {
        "mac": [],
        "windows": [{"chord": "alt+f4", "reason": "closing the window"}],
        "linux": [],
    },
    "snapshot_relative_path": "data/.state/profile/account.json",
}


def _contract() -> Contract:
    return parse_contract(_RAW_CONTRACT)


def _action(
    app: str,
    action_id: str,
    scope: str,
    chords: tuple[str, ...],
    reset_chords: tuple[str, ...] | None = None,
    source: str = "default",
) -> Action:
    return Action(
        app=app,
        app_display_name=app.title(),
        id=action_id,
        label=action_id.replace("-", " ").title(),
        scope=scope,
        chords=chords,
        default_chords=chords if reset_chords is None else reset_chords,
        reset_chords=chords if reset_chords is None else reset_chords,
        source=source,
    )


_LAUNCHER = _action("shell", "open-launcher", "workspace", ("meta+k",))
_NEXT_WINDOW = _action("shell", "next-window", "workspace", ("alt+meta+]",))
_SECONDARY_SEND = _action("shell", "secondary-send", "app", ("meta+enter",))
_CHAT_SEND = _action("chat", "send-message", "app", ("enter", "meta+enter"))
_FILES_BACK = _action("files", "back", "app", ("meta+[",))
_CATALOG = (_LAUNCHER, _NEXT_WINDOW, _SECONDARY_SEND, _CHAT_SEND, _FILES_BACK)


class _FakeShell(Shell):
    """The shell's keybinding routes over a list of actions held in memory."""

    def __init__(
        self,
        actions: Sequence[Action],
        can_save_everywhere: bool = True,
        delivery_status: str = "DELIVERED",
        failing_save_of: str | None = None,
    ) -> None:
        super().__init__("http://fake.invalid")
        self.actions = list(actions)
        self.can_save_everywhere = can_save_everywhere
        self.delivery_status = delivery_status
        self.failing_save_of = failing_save_of
        self.saves: list[tuple[str, tuple[str, ...], bool]] = []
        self.resets: list[str] = []

    def catalog(self) -> Catalog:
        return Catalog(
            actions=tuple(self.actions), can_save_everywhere=self.can_save_everywhere
        )

    def _replace(self, action: Action, chords: Sequence[str], source: str) -> None:
        index = next(
            i
            for i, held in enumerate(self.actions)
            if held.app == action.app and held.id == action.id
        )
        held = self.actions[index]
        self.actions[index] = Action(
            app=held.app,
            app_display_name=held.app_display_name,
            id=held.id,
            label=held.label,
            scope=held.scope,
            chords=tuple(chords),
            default_chords=held.default_chords,
            reset_chords=held.reset_chords,
            source=source,
        )

    def save(self, action: Action, chords: Sequence[str], is_everywhere: bool) -> Any:
        if action.id == self.failing_save_of:
            raise CouldNotRunError("the workspace answered 500")
        self.saves.append((action.id, tuple(chords), is_everywhere))
        if not is_everywhere:
            self._replace(action, chords, "workspace")
            return {"chords": list(chords), "delivery": None}
        return {
            "chords": list(chords),
            "delivery": {"status": self.delivery_status, "detail": ""},
        }

    def reset(self, action: Action) -> Any:
        self.resets.append(action.id)
        self._replace(action, action.reset_chords, "default")
        return {"status": "NOTHING_TO_DELIVER", "detail": ""}


def _save_args(*arguments: str) -> Any:
    return build_command_parser("save").parse_intermixed_args(list(arguments))


def test_normalize_chord_puts_modifiers_in_the_grammars_order_and_reads_the_aliases() -> (
    None
):
    contract = _contract()
    assert normalize_chord("Meta+Shift+J", contract) == "shift+meta+j"
    assert normalize_chord("cmd+alt+ctrl+k", contract) == "ctrl+alt+meta+k"
    assert normalize_chord("meta+return", contract) == "meta+enter"
    assert normalize_chord("meta+plus", contract) == "meta+plus"
    assert normalize_chord(" f6 ", contract) == "f6"


def test_normalize_chord_spells_a_shifted_key_with_the_character_it_types() -> None:
    contract = _contract()
    # Shift with ] types }, which is how the keystroke arrives and how the browser's list spells it.
    assert normalize_chord("shift+meta+]", contract) == "shift+meta+}"
    assert normalize_chord("shift+meta+3", contract) == "shift+meta+#"
    assert normalize_chord("shift+meta+=", contract) == "shift+meta+plus"
    # A character only Shift types gains the modifier; the number pad types * and + without it.
    assert normalize_chord("meta+}", contract) == "shift+meta+}"
    assert normalize_chord("meta+*", contract) == "meta+*"
    assert normalize_chord("meta+plus", contract) == "meta+plus"
    assert normalize_chord("shift+meta+n", contract) == "shift+meta+n"
    assert find_warnings(
        (normalize_chord("shift+meta+]", contract),), contract, None
    ) == (
        "On macOS, a browser keeps ⇧⌘} for itself, so this never runs when the workspace is open in a browser "
        "there.",
    )


@pytest.mark.parametrize(
    "spelling",
    ["", "meta+", "meta++", "hyper+k", "meta+meta+k", "meta+shift", "meta+banana"],
)
def test_normalize_chord_refuses_what_the_grammar_has_no_chord_for(
    spelling: str,
) -> None:
    with pytest.raises(RefusedError):
        normalize_chord(spelling, _contract())


def test_chord_label_writes_apples_symbols_in_grammar_order_and_names_elsewhere_with_the_primary_first() -> (
    None
):
    assert chord_label("shift+meta+n", is_apple=True) == "⇧⌘N"
    assert chord_label("shift+meta+n", is_apple=False) == "Ctrl+Shift+N"
    assert chord_label("alt+meta+arrowleft", is_apple=True) == "⌥⌘←"
    assert chord_label("meta+enter", is_apple=False) == "Ctrl+Enter"
    assert chord_label("f6", is_apple=True) == "F6"


def test_is_workspace_wide_chord_wants_a_modifier_beyond_shift_or_a_function_key() -> (
    None
):
    assert is_workspace_wide_chord("meta+k")
    assert is_workspace_wide_chord("f6")
    assert is_workspace_wide_chord("shift+f6")
    assert not is_workspace_wide_chord("k")
    assert not is_workspace_wide_chord("shift+k")
    assert not is_workspace_wide_chord("f")


def test_clash_candidates_follow_the_screens_rule_for_each_scope() -> None:
    # A workspace-wide action wins inside any app's page, so it can collide with every other action.
    assert set(clash_candidates(_CATALOG, _LAUNCHER)) == {
        _NEXT_WINDOW,
        _SECONDARY_SEND,
        _CHAT_SEND,
        _FILES_BACK,
    }
    # An app's own action: the rest of its app, and the workspace-wide actions of the other apps.
    assert set(clash_candidates(_CATALOG, _CHAT_SEND)) == {_LAUNCHER, _NEXT_WINDOW}
    assert set(clash_candidates(_CATALOG, _SECONDARY_SEND)) == {_LAUNCHER, _NEXT_WINDOW}


def test_find_clashes_names_each_action_holding_a_brought_chord() -> None:
    clashes = find_clashes(_CATALOG, _LAUNCHER, ("meta+enter", "meta+j"))
    assert {(clash.chord, clash.other.id) for clash in clashes} == {
        ("meta+enter", "secondary-send"),
        ("meta+enter", "send-message"),
    }
    # Two apps' own actions may share a key: each runs only in its own window.
    assert find_clashes(_CATALOG, _FILES_BACK, ("meta+enter",)) == ()


def test_chords_taken_by_action_holds_only_the_chords_that_clash() -> None:
    sharing_two = _action("files", "forward", "app", ("meta+k", "meta+j", "meta+]"))
    clashes = (
        Clash("meta+j", sharing_two),
        Clash("meta+j", _CHAT_SEND),
        Clash("meta+k", sharing_two),
    )
    assert chords_taken_by_action(clashes) == {
        sharing_two: ("meta+j", "meta+k"),
        _CHAT_SEND: ("meta+j",),
    }


def test_find_refusals_covers_the_refused_chord_the_typing_rule_and_the_limit() -> None:
    contract = _contract()
    assert (
        len(
            find_refusals(
                _LAUNCHER, ("alt+meta+delete",), ("alt+meta+delete",), contract
            )
        )
        == 1
    )
    assert len(find_refusals(_LAUNCHER, ("shift+k",), ("shift+k",), contract)) == 1
    # An app's own action may have a key that typing produces.
    assert find_refusals(_CHAT_SEND, ("enter",), ("enter",), contract) == ()
    too_many = tuple(f"meta+{index}" for index in range(10)) + tuple(
        f"alt+meta+{index}" for index in range(7)
    )
    assert any(
        "at most 16" in refusal
        for refusal in find_refusals(_LAUNCHER, too_many, too_many, contract)
    )


def test_find_refusals_refuses_a_key_an_operating_system_keeps_naming_each_one_that_does() -> (
    None
):
    contract = _contract()
    assert find_refusals(_LAUNCHER, ("meta+space",), ("meta+space",), contract) == (
        "⌘Space on a Mac / Ctrl+Space on Windows and Linux is kept by an operating system: macOS uses ⌘Space for "
        "Spotlight. It would never reach Imbue Studio there, and a shortcut is saved for every platform, so it is "
        "refused. It can still be set by hand on the Keyboard shortcuts screen, which only warns.",
    )
    (kept_on_two,) = find_refusals(_NEXT_WINDOW, ("alt+tab",), ("alt+tab",), contract)
    assert (
        "Windows uses Alt+Tab for the window switcher; Linux uses Alt+Tab for the window switcher"
        in kept_on_two
    )
    # An app's own action is refused it too: the OS keeps the key from every app.
    assert (
        len(find_refusals(_FILES_BACK, ("meta+space",), ("meta+space",), contract)) == 1
    )


def test_find_refusals_leaves_an_os_kept_key_the_action_already_has() -> None:
    contract = _contract()
    holding_spotlights_key = _action(
        "shell", "open-launcher", "workspace", ("meta+space",)
    )
    assert (
        find_refusals(
            holding_spotlights_key, ("meta+space", "meta+j"), ("meta+j",), contract
        )
        == ()
    )
    assert (
        build_report(
            _CATALOG, holding_spotlights_key, ("meta+space", "meta+j"), contract, None
        ).refusals
        == ()
    )


def test_find_warnings_names_every_platform_whose_browser_keeps_the_chord() -> None:
    contract = _contract()
    assert find_warnings(
        ("meta+w",), contract, {"keybindings": {"imbue-studio": {"close-window": []}}}
    ) == (
        "On macOS, a browser keeps ⌘W for itself, so this never runs when the workspace is open in a browser there.",
        "On Windows, a browser keeps Ctrl+W for itself, so this never runs when the workspace is open in a browser "
        "there.",
        "On Linux, a browser keeps Ctrl+W for itself, so this never runs when the workspace is open in a browser "
        "there.",
    )
    # A key an operating system keeps is a refusal, not a warning.
    assert find_warnings(("meta+space",), contract, None) == ()
    assert find_warnings(("alt+f4",), contract, None) == (
        "On Windows, Alt+F4 is normally used for closing the window; in the Imbue Studio app it runs this action "
        "instead.",
    )
    assert find_warnings(("alt+meta+j",), contract, None) == ()


def test_find_warnings_says_a_mac_control_chord_works_on_a_mac_only_and_checks_it_there_alone() -> (
    None
):
    kept_by_every_browser = {
        **_RAW_CONTRACT,
        "browser_reserved_chords": {
            "mac": ["ctrl+tab"],
            "windows": ["ctrl+tab"],
            "linux": [],
        },
    }
    warnings = find_warnings(("ctrl+tab",), parse_contract(kept_by_every_browser), None)
    assert len(warnings) == 2
    assert "works on a Mac only" in warnings[0]
    assert warnings[1].startswith("On macOS, a browser keeps ⌃⇥")


def test_find_warnings_names_imbue_studios_own_shortcut_by_default_and_as_the_account_changed_it() -> (
    None
):
    contract = _contract()
    (by_default,) = [
        warning
        for warning in find_warnings(("meta+n",), contract, None)
        if "Imbue Studio's own" in warning
    ]
    assert '"New window"' in by_default
    changed = {
        "keybindings": {"imbue-studio": {"new-window": ["alt+meta+n"], "quit": []}}
    }
    assert not any(
        "Imbue Studio's own" in warning
        for warning in find_warnings(("meta+n",), contract, changed)
    )
    assert any(
        '"New window"' in warning
        for warning in find_warnings(("alt+meta+n",), contract, changed)
    )
    assert imbue_studio_chords(changed)["quit"] == ("Quit", ())
    assert imbue_studio_chords({"keybindings": {"imbue-studio": {"quit": "meta+q"}}})[
        "quit"
    ] == ("Quit", ("meta+q",))


def test_requested_chords_replaces_or_adds_without_repeating_a_key() -> None:
    contract = _contract()
    assert requested_chords(_LAUNCHER, ["meta+j"], False, contract) == ("meta+j",)
    assert requested_chords(_LAUNCHER, ["meta+j", "META+K"], True, contract) == (
        "meta+k",
        "meta+j",
    )
    assert requested_chords(_LAUNCHER, [], False, contract) == ()


def test_build_report_checks_only_the_keys_the_step_brings() -> None:
    contract = _contract()
    holding_a_browser_key = _action("shell", "close-window", "workspace", ("meta+w",))
    report = build_report(
        (*_CATALOG, holding_a_browser_key),
        holding_a_browser_key,
        ("meta+w", "meta+k"),
        contract,
        None,
    )
    assert report.brought == ("meta+k",)
    assert [clash.other.id for clash in report.clashes] == ["open-launcher"]
    assert report.warnings == ()
    rendered = render_report(report, "NOT SAVED")
    assert "CLASHES (ask the user before taking a key from another action):" in rendered
    assert (
        '⌘K on a Mac / Ctrl+K on Windows and Linux is the shortcut of "Open Launcher"'
        in rendered
    )


def test_render_report_vouches_only_for_the_keys_it_checked() -> None:
    contract = _contract()
    added = build_report(_CATALOG, _LAUNCHER, ("meta+k", "alt+meta+j"), contract, None)
    assert render_report(added, "SAVED in this workspace.").splitlines()[-1] == (
        "No clash, and nothing known to keep ⌥⌘J on a Mac / Ctrl+Alt+J on Windows and Linux [alt+meta+j] from "
        "working on any platform or in a browser."
    )
    unchanged = build_report(_CATALOG, _LAUNCHER, ("meta+k",), contract, None)
    assert (
        render_report(unchanged, "SAVED in this workspace.")
        .splitlines()[-1]
        .startswith("No new key was checked")
    )


def test_parse_catalog_reads_the_shells_answer_and_refuses_one_without_its_fields() -> (
    None
):
    raw: dict[str, Any] = {
        "apps": [
            {
                "app": "files",
                "display_name": "Files",
                "actions": [
                    {
                        "id": "back",
                        "label": "Back",
                        "chords": ["meta+k"],
                        "default_chords": ["meta+["],
                        "reset_chords": ["meta+["],
                        "scope": "app",
                        "source": "workspace",
                    }
                ],
            }
        ],
        "can_save_everywhere": False,
    }
    catalog = parse_catalog(raw)
    assert not catalog.can_save_everywhere
    assert catalog.actions == (
        Action(
            app="files",
            app_display_name="Files",
            id="back",
            label="Back",
            scope="app",
            chords=("meta+k",),
            default_chords=("meta+[",),
            reset_chords=("meta+[",),
            source="workspace",
        ),
    )
    with pytest.raises(CouldNotRunError):
        parse_catalog(
            {
                "apps": [
                    {
                        "app": "files",
                        "display_name": "Files",
                        "actions": [{"id": "back"}],
                    }
                ]
            }
        )
    with pytest.raises(CouldNotRunError):
        parse_catalog(["not", "a", "catalog"])


def test_parse_contract_refuses_a_file_missing_a_list_rather_than_reading_it_as_empty() -> (
    None
):
    without_os_kept = {
        key: value for key, value in _RAW_CONTRACT.items() if key != "os_kept_chords"
    }
    with pytest.raises(CouldNotRunError):
        parse_contract(without_os_kept)


def test_account_outcome_reads_delivered_only_as_delivered() -> None:
    assert account_outcome({"delivery": {"status": "DELIVERED", "detail": ""}}) == (
        True,
        "ACCOUNT DELIVERY: DELIVERED",
    )
    is_delivered, line = account_outcome(
        {"delivery": {"status": "PERMISSION_PENDING", "detail": "asked"}}
    )
    assert not is_delivered
    assert line.startswith(
        "ACCOUNT DELIVERY: PERMISSION_PENDING: Imbue Studio is showing the user a request"
    )
    assert 'The workspace said: "asked".' in line
    assert account_outcome({"delivery": None})[0] is False


def test_save_stops_on_a_clash_and_with_take_removes_only_the_key_the_clash_named(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Back shares meta+k with the launcher already; only meta+[ is the clash the user is asked about.
    back_sharing = _action("files", "back", "app", ("meta+k", "meta+["))
    shell = _FakeShell((_LAUNCHER, back_sharing))
    assert run_check_or_save(
        _save_args("shell", "open-launcher", "meta+[", "--add"),
        shell,
        _contract(),
        True,
    ) == (EXIT_CLASH)
    assert shell.saves == []
    assert "NOT SAVED: another action has a key" in capsys.readouterr().out

    taking = _save_args("shell", "open-launcher", "meta+[", "--add", "--take")
    assert run_check_or_save(taking, shell, _contract(), True) == EXIT_OK
    assert shell.saves == [
        ("back", ("meta+k",), False),
        ("open-launcher", ("meta+k", "meta+["), False),
    ]
    output = capsys.readouterr().out
    assert "RESULT: SAVED in this workspace." in output
    assert "CLASHES" not in output
    assert (
        'TOOK ⌘[ on a Mac / Ctrl+[ on Windows and Linux [meta+[] FROM: "Back"' in output
    )


def test_check_never_saves_and_says_what_a_take_would_remove(
    capsys: pytest.CaptureFixture[str],
) -> None:
    shell = _FakeShell(_CATALOG)
    checking = build_command_parser("check").parse_intermixed_args(
        ["files", "back", "--take", "meta+k"]
    )
    assert run_check_or_save(checking, shell, _contract(), False) == EXIT_OK
    assert shell.saves == []
    assert (
        'WOULD TAKE ⌘K on a Mac / Ctrl+K on Windows and Linux [meta+k] FROM: "Open Launcher"'
        in (capsys.readouterr().out)
    )


def test_save_needs_a_key_or_an_explicit_none() -> None:
    shell = _FakeShell(_CATALOG)
    with pytest.raises(UsageError):
        run_check_or_save(_save_args("files", "back"), shell, _contract(), True)
    with pytest.raises(UsageError):
        run_check_or_save(
            _save_args("files", "back", "meta+j", "--none"), shell, _contract(), True
        )
    assert (
        run_check_or_save(
            _save_args("files", "back", "--none"), shell, _contract(), True
        )
        == EXIT_OK
    )
    assert shell.saves == [("back", (), False)]


def test_save_everywhere_is_refused_without_an_account_and_with_a_key_to_take(
    capsys: pytest.CaptureFixture[str],
) -> None:
    everywhere = _save_args("files", "back", "alt+meta+j", "--everywhere")
    no_account = _FakeShell(_CATALOG, can_save_everywhere=False)
    assert run_check_or_save(everywhere, no_account, _contract(), True) == EXIT_REFUSED
    assert "no connection to the account" in capsys.readouterr().out
    taking_everywhere = _save_args("files", "back", "meta+k", "--everywhere", "--take")
    shell = _FakeShell(_CATALOG)
    assert (
        run_check_or_save(taking_everywhere, shell, _contract(), True) == EXIT_REFUSED
    )
    assert shell.saves == []
    assert "cannot be done in one step" in capsys.readouterr().out


def test_save_everywhere_reports_the_account_by_the_delivery_not_by_a_reread(
    capsys: pytest.CaptureFixture[str],
) -> None:
    everywhere = _save_args("files", "back", "alt+meta+j", "--everywhere")
    assert (
        run_check_or_save(everywhere, _FakeShell(_CATALOG), _contract(), True)
        == EXIT_OK
    )
    delivered = capsys.readouterr().out
    assert "RESULT: SAVED to the account, for every workspace." in delivered
    assert delivered.rstrip().endswith("ACCOUNT DELIVERY: DELIVERED")
    waiting = _FakeShell(_CATALOG, delivery_status="PERMISSION_PENDING")
    assert run_check_or_save(everywhere, waiting, _contract(), True) == EXIT_OK
    pending = capsys.readouterr().out
    assert "it has NOT reached the account yet" in pending
    assert "ACCOUNT DELIVERY: PERMISSION_PENDING" in pending


def test_a_failure_after_a_key_was_taken_says_what_already_changed() -> None:
    shell = _FakeShell(_CATALOG, failing_save_of="back")
    with pytest.raises(PartlyDoneError) as raised:
        run_check_or_save(
            _save_args("files", "back", "meta+k", "--take"), shell, _contract(), True
        )
    assert shell.saves == [("open-launcher", (), False)]
    (done,) = raised.value.done_lines
    assert done.startswith(
        "TOOK ⌘K on a Mac / Ctrl+K on Windows and Linux [meta+k] FROM"
    )


def test_reset_stops_when_it_would_bring_back_a_key_another_action_has_now(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The launcher gave up meta+k in this workspace, and Back took it; a reset of the launcher brings it back.
    launcher = _action(
        "shell",
        "open-launcher",
        "workspace",
        (),
        reset_chords=("meta+k",),
        source="workspace",
    )
    back = _action(
        "files",
        "back",
        "app",
        ("meta+k",),
        reset_chords=("meta+[",),
        source="workspace",
    )
    shell = _FakeShell((launcher, back))
    resetting = build_command_parser("reset").parse_intermixed_args(
        ["shell", "open-launcher"]
    )
    assert run_reset(resetting, shell, _contract()) == EXIT_CLASH
    assert shell.resets == [] and shell.saves == []
    assert "NOT RESET" in capsys.readouterr().out

    taking = build_command_parser("reset").parse_intermixed_args(
        ["shell", "open-launcher", "--take"]
    )
    assert run_reset(taking, shell, _contract()) == EXIT_OK
    assert shell.saves == [("back", (), False)]
    assert shell.resets == ["open-launcher"]
    output = capsys.readouterr().out
    assert "RESULT: RESET: this workspace's own choice is gone" in output
    assert "the app's default" in output


def test_reset_says_so_when_the_workspace_had_no_choice_of_its_own(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from_the_account = _action("files", "back", "app", ("meta+j",), source="account")
    resetting = build_command_parser("reset").parse_intermixed_args(["files", "back"])
    assert run_reset(resetting, _FakeShell((from_the_account,)), _contract()) == EXIT_OK
    assert "RESULT: NOTHING TO RESET" in capsys.readouterr().out


def test_main_answers_a_command_it_cannot_parse_as_could_not_run_and_help_as_ok(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["save", "files", "back", "meta+j", "--nonsense"]) == EXIT_COULD_NOT_RUN
    assert main(["frobnicate"]) == EXIT_COULD_NOT_RUN
    assert main([]) == EXIT_COULD_NOT_RUN
    assert "RESULT: COULD NOT RUN" in capsys.readouterr().err
    assert main(["--help"]) == EXIT_OK
    assert main(["save", "--help"]) == EXIT_OK


def test_flags_may_stand_before_between_or_after_the_keys() -> None:
    args = _save_args("--add", "files", "back", "meta+j", "--take", "meta+k")
    assert (args.app, args.action, args.chords, args.add, args.take) == (
        "files",
        "back",
        ["meta+j", "meta+k"],
        True,
        True,
    )
