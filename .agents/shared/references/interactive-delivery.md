# The interactive-delivery shape

The shared skeleton for any task where you build something *for the user to
react to*: a fetched-and-processed dataset, an app, a report. It is the
**live half** of "live first, ratify at turn-end": put a cheap, real, throwaway
version in front of the user first, loop until they confirm its shape, and only
then spend the expensive, thorough effort, in a background worker.

## Contents

- Why this shape exists
- The skeleton: the eight phases, in order
- Cross-cutting principles

## Why this shape exists

A user who is talking with you and waiting on the result expects check-ins and
frequent chances to react. Scaffolding, testing and hardening something whose
basic shape is not yet confirmed makes them wait for work that their first
reaction almost always throws away. So everything expensive (thorough tests,
review gates, polish) is deferred to a background worker, and the user is never
blocked on it.

## The skeleton

Phases are sequential; do not skip ahead of an unconfirmed gate. Each
specialization (data, an app, ...) fills these phases in with its own
specifics, but the shape below holds for all of them.

1. **Clarify what blocks.** Ask the questions whose answers change what
   you do next. Zero is fine. Do not gather complete requirements -- the goal is
   to unblock the smallest end-to-end version, not to design the final feature.

2. **Fast feasibility pass.** Time-boxed (a few minutes, a handful of tool
   calls). Enough to not bullshit about whether the task is possible; not a
   research project.

3. **Propose a small plan, wait for approval.** A short plan the user can react
   to -- the approach and what they'll see at the end -- not an exhaustive spec.
   Wait for a go-ahead before building anything.

4. **Validate risky dependencies first.** Before any other work, exercise the
   operations whose failure could sink the whole task and that are *not under
   your control* (external APIs, auth flows,
   third-party fetches). The test: if this fails, can you work around it without
   abandoning the user's core ask? If yes (your own code, a trivial transform),
   it does not belong in this pass. If no, validate it now. Fail fast here.

5. **Put a cheap, real, demonstrative prototype in front of the user.** Produce
   the smallest *real* prototype that lets the user judge the shape and loop on
   it: present, take feedback, present an updated prototype that *visibly applies*
   the feedback. The purpose of this prototype is **fast feedback** -- getting the
   shape in front of the user in seconds -- not merely avoiding a broken surface;
   keep it cheap and disposable. Loop until the user **explicitly confirms** the
   shape is right. That confirmation is the gate to everything below.

   Two demonstrative-prototype types, chosen by **wiring-cost vs. restart-cost**:

   - *Type 1 -- janky real edit:* rough or hardcoded, but in the *real* code and
     shown through the *real* surface. Faithful, and it flows straight into the
     real implementation. Prefer it when real context is needed to convince, when
     integration is cheap, or when the rough version should simply become the real
     thing.
   - *Type 2 -- detached prototype:* a separate throwaway (a hand-built HTML mock,
     a fake UI) -- something the user opens and clicks. A picture of it shows
     none of the interactions, so show pictures only when the user asks for
     them. Cheapest and fastest, no real wiring, discarded after. Prefer it
     for the fastest look-and-feel loop, or for comparing directions when a fake
     conveys the idea and real wiring is costly -- but it may *not convince* when
     real rendering matters.

   Keep this phase **throwaway either way: no commits on the served branch and no
   production state** (nothing production-grade: no production scripts, data
   layers, or tests; the scaffold a specialization sets up to serve the prototype
   is fine). Producing a
   Type 2 prototype by hand, in-context, is fine as long as its *output* is real.
   Committing a Type 1 edit on an *isolated throwaway branch* is also fine -- a
   worktree-based live loop depends on exactly those commits; what matters is that
   nothing reaches the served branch or production state until the shape is
   confirmed.

   **Do not test the prototype yourself; that is what you are surfacing it for.**
   Your own check ends at "it came up" -- an exit code, a health probe, a `curl`,
   a log line -- and it happens *before* you put the thing in front of the user.
   Once it is in front of them, driving it (a browser session, a scripted
   interaction) buys nothing the next few seconds of their attention won't, and
   it costs the thing this phase exists for: speed. Worse, the surface is often
   live -- wired to real data, real accounts, real conversations -- so your test
   actions land in the user's view and in their state. The thorough pass is phase
   7's, and it runs in a background worker against its own instance.

6. **Hard gate: nothing hardened before confirmation.** Do not crystallize, write
   thorough tests, run review gates, or build production state on an unconfirmed
   creation. Hardening a moving target bakes in the wrong shape -- and bakes it
   into a background worker that cannot see corrections the user has not made
   yet. The confirmation from phase 5 is what unlocks this.

7. **Harden / ratify.** The expensive, thorough pass: real tests, review gates,
   polish. This **always runs in a background worker**; you never run the
   code-guardian gates or the thorough test passes yourself.
   Backgrounding never strands the user, because they already have the confirmed
   creation (or a usable build of it) to work with while the slow checks run
   behind them. Notably, this means that at the time of backgrounding there must
   be a reasonably representative sample creation; it can't just be a basic mock.
   This background pass is the **harden pass**, which a worker runs under the
   harden worker's own contract. The `crystallize-creation` skill leads the
   operation that produces a brand-new creation from a confirmed prototype:
   each specialization (`build-app`, `fetch-process-show`, ...) hands its
   confirmed creation to that skill, which dispatches the worker; this skeleton
   stays mechanism-agnostic.

8. **Deliver further capabilities one at a time.** A confirmation on the first
   creation approves *that* creation, not everything downstream of it. Every
   additional piece the task accretes -- a second view, a follow-up action,
   persistence, scheduling -- gets its *own* delivery and its *own* feedback gate.
   Do not bundle them. Build one, ship it, ask whether to do the next, wait.

## Cross-cutting principles

- **Demonstrate what's perceivable, elicit what isn't.** The user can only judge
  the parts of the result they can *perceive* -- how it looks, reads, or behaves
  -- by seeing them, so demonstrate those with a cheap creation. The parts they
  can't perceive -- architecture, persistence, internal structure -- you *elicit
  and record* instead: surface the decision and write it down, **never build it**
  during the interactive phase. Decide those once, at the last responsible
  moment, from the converged creation.

- **Default and declare.** Pick the simplest conventional default for any choice
  the user has not constrained, state it in one line, and move on. Only turn a
  choice into a question when it is *both* genuinely uncertain *and* expensive to
  reverse later.

- **Assume the narrowest scope.** Build for the situation in front of you -- one
  user (the person you are talking to), one use, the data you actually have --
  unless the task clearly implies more. Do not build speculative generality on a
  guess: multi-user concerns (accounts, sharing, permissions), extra
  configurability, or scale nobody asked for.

- **Phrase every choice in business terms, never technical.** This system serves
  non-technical users. Never ask a question the user cannot answer -- "should
  your edits still be here when you come back tomorrow?" not "should we persist
  to a database?"; "should everyone see the same list?" not "do we need
  multi-tenancy?". Translate every architectural fork into the user-visible
  consequence that motivates it.
