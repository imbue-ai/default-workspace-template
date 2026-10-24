# usage_dashboard (the Usage app)

The Usage app: one window over the workspace's model usage. It is the user's view
of the usage collector (`system/services/usage_collector`), which indexes every
model call made in the workspace. All the app stores of its own is where its widget
goes and how it looks (`data/.apps/usage-dashboard/settings.json`).

The app has three kinds of page, one document reading its path:

- **`/`, the home page**: one card per provider in use, answering "how much of my
  subscription am I using", then a **Total** row of tiles across every provider
  with the time window picker. Each card links to its provider's detail page, and
  the Total row to all usage.
  - **A subscription card** for every harness that reports its subscription's
    limits (Claude Pro/Max, a ChatGPT plan through Codex): each window's share
    used, as concentric rings (the longest window outermost, the shortest's share
    in the middle, each coloured by how close it is to full, with a warning such
    as "Almost out this week"), and when it resets, from the freshest reading the collector
    has. A reading is only as new as the last call that carried one, so the card
    says when it was taken; a window whose reset time has passed since then shows
    as reset. A provider is shown while it has a reading from the last 7 days.
    No page shows a subscription's usage in dollars: it is not billed per call,
    and a dollar figure beside a plan's limits reads as a charge.
  - **A spend card** for every provider used without a subscription (an API key,
    Imbue's proxy, a billing mode no writer could tell): this month's spend and
    calls, with calls of unknown cost counted rather than added as $0.
- **`/p/<item>`, a provider's detail page**, where the item is a section key
  (`subscription:anthropic`, `spend:openai`): a list of every provider (with its
  limits or spend) on the left, and on the right the provider's card, the window
  picker, its tiles, its chart, its breakdown and its latest turns, in numbered pages
  of 10, 20, 50 or 100 rows with a box to jump to a page.
  A provider's subscription use and its use without one are separate items.
  On a narrow window the list sits above the page as a row (left out when there
  is only one provider), and the page's heading names the provider in place of
  "Usage". There the latest turns (and a turn's calls) are two-line rows rather
  than a table: the source and its share of each limit (or its cost) on the first
  line, the time, model, calls, tokens and duration on the second, and its status
  only when a call failed; a turn's call leads with its time, its source being
  the turn's.
  The home page's "See details" link opens the first provider's page.
- **`/all`**: the same for every provider at once, for a link made by hand.

Nothing names a provider it must find; a card or item exists only for what the
data shows. The window is the last 30 minutes, 24 hours, 7 days or this month
(the billing period, which starts at local midnight on the 1st). Every detail
query is grouped by provider and billing mode as well, so a provider's figures
are picked out of the same answers the Total row adds up; a provider's latency
and its latest turns are asked for on their own, filtered to its provider and
billing modes, since percentiles do not add up. What the tiles, charts and breakdowns show:

- **Spend**: what was billed, with Imbue key and API key spend apart; a
  subscription's calls add nothing to it. Calls whose cost or billing mode is unknown are counted beside
  the total rather than added as $0, and a total that includes a cost the
  collector estimated from tokens carries a "~".
- **Tokens** (input, output, cache read and cache write), **calls** with how many
  turns they were in and the share that failed or were rate-limited, and **latency** (p50 and p95 of each call's
  duration; calls with no duration are counted apart, and durations from Claude
  session logs are marked approximate).
- A chart of spend or tokens over the window, stacked by source (the app, chat or
  routine that made the calls), or by model when picked beside it. A subscription's
  chart shows its **estimated usage** instead, as a share of its 5-hour or weekly
  limit; a subscription never shows a dollar figure. A harness reports only each
  window's share used for the whole account, wherever else it is signed in, so
  the page does not spread that share over this workspace's calls. It measures
  what they added instead: the rise in the window's share from its first reading
  here to its latest (the collector keeps both), divided by the API-rate price of
  this workspace's calls between the two (weighing an Opus token above a Haiku
  one, much as the limits do), gives a share per dollar, and a call's estimated
  share is its price times that. Until a window has risen there is nothing to
  measure, and the page shows no estimate for it. The breakdown shows each row's
  estimated share of each limit within that limit's most recent window; the
  latest turns show each turn's and each call's estimated share of each limit. A
  subscription's page shows these limit columns in both tables in place of spend
  and cost; any other page shows spend and cost instead. Once a window resets its
  harness reports nothing for it until the next call, so the estimate keeps the
  last reading's rate (the account's, which a reset does not change) and says so;
  dashed lines mark where the chosen window started and where it reset.
- Sources go by the names the user knows them by: an app by its display name (from
  the app registry), a chat by its title (from the chat app's chat list) and a
  routine by its title (from its definition file); one the lookup cannot name
  shows a short id.
- A breakdown by source (the app, chat or routine that made the calls), model or
  billing mode, opening on source. Clicking a row filters the whole page to it.
- The latest turns, newest first, as they happen: one row per turn (a user
  message and every call it led to, by the calls' `turn_id`; a call without
  one is a turn of its own), with its calls, tokens, cost or limit shares,
  how long it took and how many calls failed. A turn opens to its calls, and a
  call to its raw event.

Day views ask the collector for hourly buckets and add them up into local days in
the browser, because the collector's own day buckets start at UTC midnight.

## Server

`usage-dashboard` serves the page on 8060 and forwards the page's reads to the
collector, which listens on 127.0.0.1:8795 only:

| Route | Answers |
|---|---|
| `GET /api/usage/totals`, `/latency`, `/calls`, `/limits`, `/health` | the collector's `/v1/totals`, `/v1/latency`, `/v1/calls`, `/v1/limits` and `/health`, with the same query |
| `GET /api/usage/stream` | the collector's server-sent events, relayed |
| `GET /api/apps` | the display name of every registered app |
| `GET /api/health` | whether the app is up and its page built |
| `GET /widget` | the Usage widget, which the workspace shell keeps on the desktop; `?variant=rings\|minimal\|bars`, `?size=small\|medium\|large`, `?detail=less\|standard\|more`, `?tray=rings\|ring\|bars\|numbers\|stacked` and `?toolbar=chip\|meters\|inline\|line` show that look on this page alone |
| `GET /api/settings`, `POST /api/settings` | the app's settings, `{"widget_variant", "widget_size", "widget_detail", "widget_tray", "widget_place", "widget_toolbar"}` (`widget_place` is `desktop`, `toolbar` or `tray`: where the widget goes); a field left out takes its default |

Who sees usage is the workspace's sharing grants, as for any app: the share
gateway admits a visitor with a workspace-wide grant or a grant on this app, and
the collector, listening on 127.0.0.1 only, is reached from outside the
workspace only through this server. The manifest declares `system = true`, so
the Share tab lists the app under System with System Monitor.
An unreachable collector is answered with 503; the page then keeps its last
numbers and retries. The collector is almost always up and most outages are
brief (a restart, or the tunnel answering 502-504 for a moment), so the banner
shows only once it has stayed unreachable for a minute, and the header keeps
saying "Live" through a stream drop shorter than that.

## Frontend

`frontend/` is a Vite, TypeScript and Mithril build in the `system/package.json`
npm workspace, using `@imbue/workspace-ui`. `npm run build` in `system/` writes it
to `src/usage_dashboard/static/`. Charts are plain SVG.

The manifest declares a `[widget]` at `/widget`, enabled by default
(`docs/system/blueprint/app-widgets/plan-app-widgets.md`, section 7): a second
Vite entry (`widget.html`, `src/widget.ts`) that reads the limits, this month's
totals and the collector's health, and reuses the page's provider cards, so the
two never disagree. Where it goes and how it looks are picked for the whole workspace
beside "Widget" at the foot of the Usage page, on the left, and kept in the app's
settings: first its place (desktop, the taskbar's row, or its tray), then only that
place's choices. On the desktop they are its
style (rings: concentric rings per subscription, weekly outside and 5-hour inside;
minimal: those rings alone, with the 5-hour figure in each and nothing else in view;
bars: a row per subscription with a 5-hour bar above a thinner weekly one), its
size (small, medium, large: `--w-*` lengths in `style.css`) and how much it shows
(less, standard, more). In the tray they are its tray design (rings, ring only, bars,
numbers, stacked: `views/widgetTray.ts`), and in the taskbar's row its toolbar design
(chip, meters, inline, line: `views/widgetToolbar.ts`), each with how much it shows.
Its detail is in tooltips (`data-widget-tooltip`), which
the shell shows outside the widget's frame. A pick in the same browser reaches the
widget at once over a broadcast channel. Adding a style is a view in `views/`, a
name in `models/widgetLook.ts`, and a member of `WidgetVariant` in
`src/usage_dashboard/settings.py`, which the settings route checks every pick
against. With no
subscription, the collapsed widget is the month's spend. It reports its size to the
shell after every change, hides itself while there is no usage, and opens the
Usage window when clicked on the desktop; in the taskbar a click opens it expanded
in a popover above the bar. While the widget is enabled the shell keeps this app
running, though it declares `stop_when_no_windows`.
