"""The note ``run-status resume-chats`` sends each chat it paused for an update's apply.

The whole note reaches the agent; the chat shows the user only its ``<summary>`` line, as a
notice labelled by the outcome the opening tag carries. The chat app's
``harnesses/message_display.py`` keeps a copy of the tag and the outcome values
(``WORKSPACE_UPDATE_NOTE_TAG``), and a test there pins the two equal by classifying a note this
module composed.

Standard library only, with no sibling imports: that test loads this file on its own.
"""

from __future__ import annotations

from collections.abc import Sequence

WORKSPACE_UPDATE_NOTE_TAG = "workspace-update-note"

# The ``outcome`` attribute on the opening tag: whether the update landed while the chat was
# paused, which decides the notice's label in the chat.
OUTCOME_LANDED = "landed"

OUTCOME_NOT_LANDED = "not-landed"

# The release prefix the user never sees: the app says "Update to v0.9.0" for minds-v0.9.0.
_RELEASE_PREFIX = "minds-"


def _shown_version(ref: str) -> str:
    return ref[len(_RELEASE_PREFIX) :] if ref.startswith(_RELEASE_PREFIX) else ref


def _summary(landed_ref: str | None, is_chat_paused: bool) -> str:
    if landed_ref is not None:
        version = _shown_version(landed_ref)
        if is_chat_paused:
            return f"This chat was paused while the workspace updated to {version}, and is carrying on now."
        return f"The workspace updated to {version}; a background agent of this chat was paused for it."
    if is_chat_paused:
        return "This chat was paused for a workspace update that did not go through, and is carrying on now."
    return "A background agent of this chat was paused for a workspace update that did not go through."


def compose_resume_note(
    landed_ref: str | None,
    is_chat_paused: bool,
    wip_commit: str,
    interrupted_tasks: Sequence[str],
    restarted_workers: Sequence[str],
) -> str:
    """The note for one chat: what happened while it was paused, and what it should do now.

    ``landed_ref`` is the release the apply landed, or ``None`` when it did not land.
    ``is_chat_paused`` is whether the chat itself was stopped, rather than only its background
    agents. ``wip_commit`` is the commit holding the workspace's uncommitted work, or ``""``.
    ``interrupted_tasks`` are the chat's background commands that were stopped with it, and
    ``restarted_workers`` the names of its background agents that were stopped and started again.
    """
    outcome = OUTCOME_LANDED if landed_ref is not None else OUTCOME_NOT_LANDED
    lines: list[str] = []
    if landed_ref is not None:
        lines.append(
            f"The workspace was updated to {landed_ref} while this chat's work was paused for it."
        )
        lines.append(
            "Files may have changed under you: re-read anything you were working with before you "
            "continue, rather than trusting what you remember of it."
        )
    else:
        lines.append(
            "This chat's work was paused for a workspace update that did not land, so the "
            "workspace's own code is as it was before."
        )
        lines.append("Re-read anything you were working with before you continue.")
    if wip_commit:
        lines.append(
            f"Uncommitted work in the workspace was saved as it stood in commit {wip_commit}, "
            "so nothing was lost; carry on from it."
        )
    if interrupted_tasks:
        lines.append(
            "These background commands of yours were stopped with you; start them again if you "
            "still need them: " + "; ".join(interrupted_tasks) + "."
        )
    for worker in restarted_workers:
        lines.append(
            f"Your background agent {worker} was paused too and has been started again, but it "
            "does not pick its work back up by itself: message it to carry on, and re-arm "
            "your wait on it if you had one."
        )
    lines.append("Then carry on with what you were doing.")
    body = "\n".join(lines)
    return (
        f'<{WORKSPACE_UPDATE_NOTE_TAG} outcome="{outcome}">\n'
        f"<summary>{_summary(landed_ref, is_chat_paused)}</summary>\n"
        f"{body}\n"
        f"</{WORKSPACE_UPDATE_NOTE_TAG}>"
    )
