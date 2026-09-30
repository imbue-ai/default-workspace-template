Shared UI: a hover bubble can now sit above its trigger, and can go up on the
next tick instead of after the usual hover-intent pause -- for a control whose
bubble is the only thing naming it. The palette gains a stronger border colour
(`border-strong`), for a field that has to read as one you type into rather than
as a panel seam.

The chrome's stroke icons are Lucide's drawings, copied into the source as path
data rather than pulled in as a package, and nothing in the tree said so. A
`THIRD_PARTY_NOTICES.md` at the repo root now carries Lucide's ISC notice (which
covers the Feather icons it descends from), and both icon modules point at it.
