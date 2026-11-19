"""The note bootstrap sends the agent whose apply a boot-time rollback undid.

The whole note reaches the agent; the chat shows the user only its ``<summary>`` line, as a
notice labelled by the outcome the opening tag carries. The tag is the one the update-self
skill's resume note uses (``update_resume_note.py``); the chat app's
``harnesses/message_display.py`` keeps its own copy of the tag and of these outcomes, and a test
there pins them equal by classifying notes this module composed.

Standard library only: bootstrap depends on nothing it does not need, and the chat app's test
imports this module.
"""

from __future__ import annotations

from typing import NamedTuple

WORKSPACE_UPDATE_NOTE_TAG = "workspace-update-note"

# The tree and every pre-apply copy are back.
OUTCOME_ROLLED_BACK = "rolled-back"

# The tree is back, but a pre-apply copy could not be put back (``recover``'s emergency exit).
OUTCOME_ROLLBACK_INCOMPLETE = "rollback-incomplete"

EMERGENCY_RECORD = "data/.state/update-apply/emergency.json"


class BootRollback(NamedTuple):
    """An apply the previous container run left mid-motion, which this boot rolled back.

    ``release`` is the release an update-self apply was landing (the marker's ``target_ref``),
    or ``None`` for any other apply: a critical app's change going live. ``is_restore_complete``
    is False when the tree went back but a pre-apply copy could not be put back.
    """

    dri_agent: str
    merge_ref: str
    release: str | None
    is_restore_complete: bool


def _summary(rollback: BootRollback) -> str:
    subject = (
        "The update to this workspace"
        if rollback.release is not None
        else "A change to this workspace's apps"
    )
    if rollback.is_restore_complete:
        return (
            f"{subject} stopped part-way when the workspace restarted, so the workspace has "
            "been put back the way it was. You can ask to try it again."
        )
    return (
        f"{subject} stopped part-way when the workspace restarted, and the workspace couldn't "
        "be fully put back, so some things may not work until it's repaired."
    )


def compose_rollback_note(rollback: BootRollback) -> str:
    """The note for the agent that was driving the apply: what the boot did, and what to do now."""
    if rollback.release is not None:
        what = f"the workspace update to {rollback.release} ({rollback.merge_ref})"
        guide = (
            'the update-self skill\'s references/apply-outcomes.md, "After a restart rolled '
            'it back"'
        )
    else:
        what = f"the change {rollback.merge_ref}"
        guide = (
            'the update-app skill\'s references/critical-app.md, "If a restart interrupts '
            'the go-live"'
        )
    lines = [
        f"The workspace restarted while you were applying {what}, and the recovery at boot "
        "rolled the apply back. The user has seen only the summary line above.",
    ]
    if rollback.is_restore_complete:
        lines.append(
            "The workspace is back on the revision it had before, and the worker's branch and "
            "report are kept."
        )
    else:
        lines.append(
            "The workspace's files are back on the revision it had before, but not every "
            "pre-apply copy could be put back, so the workspace may be running against a "
            f"mismatch; {EMERGENCY_RECORD} names what is left. Finish that restore yourself "
            "now, without waiting to be asked: it puts back what the workspace had, it does "
            "not retry anything. Fix what stopped it first, and ask the user before you "
            "delete anything of theirs to do so (to free disk space, say)."
        )
    lines.append(
        "Do not retry the apply on your own: it may be what restarted the workspace (by "
        "pushing memory past the container's limit, say), and a retry could restart it again. "
        "Retry only when the user asks."
    )
    lines.append(
        f"Follow {guide}: check that the workspace is healthy, wind the pass down, then tell "
        "the user in a line or two how the workspace checked out."
    )
    body = "\n".join(lines)
    outcome = OUTCOME_ROLLED_BACK if rollback.is_restore_complete else OUTCOME_ROLLBACK_INCOMPLETE
    return (
        f'<{WORKSPACE_UPDATE_NOTE_TAG} outcome="{outcome}">\n'
        f"<summary>{_summary(rollback)}</summary>\n"
        f"{body}\n"
        f"</{WORKSPACE_UPDATE_NOTE_TAG}>"
    )
