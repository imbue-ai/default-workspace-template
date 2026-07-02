Fix three chat-transcript bugs in the system interface: text selection was lost when scrolling, text selection was lost while the agent streamed new output, and the scroll position oscillated ("freaked out") when scrolling up through history or sitting at the bottom during streaming.

- Text selection now survives scrolling and streaming. Markdown content is no longer re-rendered (which destroyed the selected text nodes) when its text is unchanged, the rows holding a selection are kept mounted as the transcript scrolls or streams past them, and event eviction pauses while a selection is held.

- Auto-follow stays on while text is selected: the view keeps following the live tail, the selected text scrolls off-screen but stays selected, and copying still works. Making a selection during streaming no longer fights the auto-scroll -- the view holds still while the mouse button is held and snaps back to the tail on release.

- Scrolling is smooth and stable. The transcript now solely owns its scroll position (native browser scroll anchoring is disabled), anchoring the viewport to a row so backfill loads and row measurement don't jolt it; the self-sustaining scroll-up "yank" loop and the at-bottom input-swallowing during streaming are gone.

Design docs: `specs/chat-scroll-and-selection-bugs.md` (verified root-cause analysis) and `blueprint/chat-scroll-selection-fixes/plan-chat-scroll-selection-fixes.md` (phased plan).
