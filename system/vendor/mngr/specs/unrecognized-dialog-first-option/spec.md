# Answering an unrecognised dialog on its first option

## What this changes

Today an unrecognised surface holding the agent's input refuses the send: `classify` returns `Unrecognized`, which is `Blocking`, and preflight raises rather than pressing anything.

This proposes an opt-in alternative: cycle the selector onto **option 1** and press Enter, on the observation that the first option is usually the one a user would have picked.

Note that `sensibly_deal_with_dialogs = ["*"]` does **not** already do this. That wildcard gates `Answerable` only, and `Unrecognized` is not `Answerable` -- so a dialog mngr cannot name is refused today no matter what that list says. This is a new behaviour, not a config change.

## The case against, stated first

The premise deserves testing against the dialogs we have actually studied, because for half of them the first option is the wrong answer:

| dialog | option mngr deliberately lands on |
| --- | --- |
| Model switch warning | "Yes, switch to" |
| Effort switch warning | "Yes, switch to" |
| Usage limit reached | **"Stop and wait for limit to reset"** |
| LSP plugin install | **"No, and don't show plugin installation hints again"** |

A "Yes, install?" prompt puts *yes* first. An upsell puts *buy* first. Both of the surfaces above were given an explicit option precisely because their first row was not what a user would want -- which is the same sample this feature's premise is drawn from, pointing the other way.

`SelfClearing` says it outright: "Several clearable surfaces are destructive on Enter -- the rewind picker rewinds the conversation, the usage-limit upsell buys credits -- so this can only send its dismiss key, and no later edit can hand one an Enter path by accident."
This feature is that edit. It should be built knowing that.

The counter-case is real too: a refused send is a dead end the user must go fix by hand, and an unrecognised dialog is by definition one nobody has taught mngr about yet, so refusing may block a workspace on a prompt that a single Enter would have cleared.

**Recommendation: build it, gate it behind its own flag, and leave it off by default.** The cost of a wrong press is unbounded (money, a rewound conversation) while the benefit is bounded (a send that would otherwise return an actionable error). That asymmetry, not the hit rate, is what should decide the default.

## Behaviour

- When `classify` returns `Unrecognized` and the flag is on, mngr cycles the pane's selector until the highlighted row is option 1, then presses Enter.
- Highlight position is read, never counted: the same rule `cycle_to_option` already follows, so it is correct wherever the highlight starts.
- If the pane has no numbered selector, nothing is pressed and the send refuses exactly as it does now. A surface with no options is not a thing Enter can answer.
- If option 1 is never reached within the step budget, the send refuses. Cycling stops rather than pressing Enter on whatever it landed on.
- Recognised dialogs are unaffected. This applies only where nothing in the catalogue matched, so it can never override a named dialog's chosen option.
- Every press is recorded as an agent event, as the existing accept path does. A wrong answer must be traceable afterwards to whatever mngr pressed and to what the pane showed.

## Design

The machinery exists. `cycle_to_option(pane, option_label)` walks the highlight onto a row containing a substring, re-capturing after each Down.
What is needed is the same walk with a different target: a row that *is* option 1, rather than one containing given text.

`SELECTOR_HIGHLIGHTED_OPTION_RE` (`^[ \t]+❯[ \t]*\d+\.`) already recognises a highlighted numbered row. The target predicate is that same shape pinned to `1`:

```python
FIRST_OPTION_HIGHLIGHTED_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]+❯[ \t]*1\.", re.MULTILINE)
```

- Add `cycle_to_first_option(pane, max_steps) -> bool`, mirroring `cycle_to_option` and sharing its re-capture-per-press discipline. Refuse up front when the pane shows no numbered selector at all, the way `cycle_to_option` refuses when its label is absent.
- Prefer factoring both onto one internal walk taking a predicate over duplicating the loop; the wait-for-redraw comment in `cycle_to_option` is load-bearing and should not be copy-pasted into a second place where it can drift.
- `Unrecognized.deal_with` gains the branch: if the agent opted in, cycle and press Enter; otherwise raise as it does today.

### Configuration

A new `ClaudeAgentConfig` field, e.g. `answer_unrecognized_dialogs_on_first_option: bool = False`.

It must **not** ride on `sensibly_deal_with_dialogs`. That list means "dialogs mngr knows a sensible answer for, which the operator has approved" -- every entry names an option chosen by a person who looked at that dialog. Folding "and also guess at ones nobody has looked at" into the same switch would make a careful setting mean two very different things, and would silently turn guessing on for anyone who wrote `["*"]` to get the four known dialogs answered.

## Edge cases

- **A dialog whose option 1 is destructive.** Not preventable by construction; this is the accepted cost, which is why the flag is off by default. The agent event is the record.
- **A selector already on option 1.** No Down presses; Enter is pressed directly.
- **A pane that changes under the walk** (a turn rendering beneath the dialog). The existing no-progress guard in `deal_with_dialogs` still applies: if the same surface is present after `deal_with`, it raises rather than looping.
- **A numbered list in ordinary transcript output.** `SELECTOR_HIGHLIGHTED_OPTION_RE` requires the highlight glyph, which transcript text does not carry, so a rendered list is not a selector. The input-region rules that gate `classify` apply before any of this.
- **Chained dialogs.** Answering one may reveal another; `deal_with_dialogs` already loops within its pass budget, and each pass re-classifies, so a revealed dialog that *is* recognised gets its proper answer rather than another guess.

## Testing

- Cycles from option 3 to option 1 and presses Enter; asserts the press order.
- Already on option 1: presses Enter with no Down.
- No numbered selector on the pane: presses nothing and refuses.
- Option 1 unreachable within the budget: presses nothing further and refuses, rather than pressing Enter on the current row.
- Flag off (the default): `Unrecognized` still raises, no keys pressed.
- A recognised dialog with the flag on still lands on its own named option, not option 1.

## Open question

Worth deciding before building: should this apply to *every* unrecognised surface, or only where the pane looks like a plain numbered selector with no free-text input?
A confirmation list and a form that happens to contain numbered rows are different risks, and the second is where an Enter is most likely to do something unintended.
