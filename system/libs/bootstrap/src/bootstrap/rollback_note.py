"""The note bootstrap sends the agent whose apply a boot-time rollback undid.

The whole note reaches the agent; the chat shows the user only its ``<summary>`` line, as a
notice labelled by the outcome the opening tag carries. The tag and the ``not-landed`` outcome
are the update-self skill's resume note's (``update_resume_note.py``), which the chat app has
shown since minds-v0.9.0: the chat app that reads this note is the rolled-back one, which may
predate it.
``harnesses/message_display.py`` keeps its own copy of both, and a test there pins them equal by
classifying notes this module composed.

Standard library only: bootstrap depends on nothing it does not need, and the chat app's test
imports this module.
"""

from __future__ import annotations

from typing import NamedTuple

WORKSPACE_UPDATE_NOTE_TAG = "workspace-update-note"

OUTCOME_NOT_LANDED = "not-landed"

EMERGENCY_RECORD = "data/.state/update-apply/emergency.json"

# The pass runs from the target's staged copy of the update-self skill; after the rollback the
# workspace's own copy is the pre-apply revision, which may predate this note's guidance.
STAGED_APPLY_OUTCOMES = (
    "data/.tasks/update-self/skill-at-target/.agents/skills/update-self/references/"
    "apply-outcomes.md"
)


class BootRollback(NamedTuple):
    """An apply the previous container run left mid-motion, which this boot rolled back.

    ``release`` is the release an update-self apply was landing (the marker's ``target_ref``),
    or ``None`` for any other apply: a critical app's change going live, or the revert the
    update-self skill lands when the user asks to roll an update back. ``is_restore_complete``
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
        else "A change to this workspace"
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
        guide = f'{STAGED_APPLY_OUTCOMES}, "After a restart rolled it back"'
    else:
        what = f"the change {rollback.merge_ref}"
        guide = (
            "the update-app skill's references/critical-app.md, \"If a restart interrupts "
            'the go-live" (or, for a revert the update-self skill was landing, '
            f'{STAGED_APPLY_OUTCOMES}, "After a restart rolled it back")'
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
            "delete anything of theirs to do so (to free disk space, say). The summary "
            "made the user no offer to try again, so once everything is back, tell them "
            "they can ask to."
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
    return (
        f'<{WORKSPACE_UPDATE_NOTE_TAG} outcome="{OUTCOME_NOT_LANDED}">\n'
        f"<summary>{_summary(rollback)}</summary>\n"
        f"{body}\n"
        f"</{WORKSPACE_UPDATE_NOTE_TAG}>"
    )
