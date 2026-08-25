Added the transcript smooth-scroll design spec (docs/system/specs/transcript-smooth-scroll.md): three layers (Visible / Physical / Virtual), two strict state machines (FOLLOW / USER_CONTROLLED scroll position, ELSEWHERE / SCROLLBAR scrollbar interaction), a custom overlay scrollbar with pixel-space physical and index-space virtual regions, and a 3-PR delivery plan.

CI's frontend step now runs the vitest unit-test suite (npm test) in addition to the typecheck and build.
