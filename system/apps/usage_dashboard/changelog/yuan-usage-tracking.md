New app: Usage, a window over the workspace's model usage, read from the usage collector. It opens on one card per provider in use, answering how much of each subscription is used: a subscription card with each limit's share used as concentric rings and when it resets (Claude, Codex, any harness that reports limits), or a spend card with this month's spend for usage without a subscription.

Below the cards, a Total row of tiles across every provider has the time window picker. Each card opens its provider's detail page: a list of every provider on the left, and on the right that provider's card, tiles, charts, breakdown and latest turns. For the last 30 minutes, 24 hours, 7 days or this month (from local midnight on the 1st), the pages show:

- what was billed (Imbue key and API key spend), with no dollar figure for subscription usage, which is not billed per call, calls of unknown cost counted beside the total rather than added as $0, and a "~" on totals that include costs estimated from tokens;

- tokens, calls with how many turns they were in and the share that failed or were rate-limited, and p50 and p95 latency, with untimed calls counted apart and durations from Claude session logs marked approximate;

- a chart of spend or tokens over the window, stacked by source or, when picked beside it, by model, and a breakdown by source, model or billing mode, where clicking a row filters the whole page; a source goes by its app's display name, its chat's title or its routine's title;

- the latest turns as they happen (each a user message and every call it led to; a call with no turn is a turn of its own), in numbered pages of 10, 20, 50 or 100 rows with a box to jump to a page, each opening to its calls and each call to its raw event.

It stores nothing of its own, reconnects to the collector's live stream after an outage, keeps its last numbers under a banner once the collector has been unreachable for a minute (a briefer outage, such as a restart, is retried quietly and the header stays "Live"), and says when the collector is still indexing past usage.

Who sees it follows the workspace's sharing grants, like any app: a visitor with a workspace-wide grant or a grant on Usage sees the same numbers as the owner. The Share tab lists it under System.

A subscription's chart shows each source's or model's estimated usage as a share of its 5-hour or weekly limit, its breakdown shows each row's estimated share of each limit within that limit's most recent window, and its latest turns show each turn's and each call's estimated share of each limit, never a dollar figure. The estimate counts only this workspace's calls: a harness's limit reading is the whole account's, wherever else it is signed in, so instead of spreading that share over the calls here the page divides the rise in each window's share from its first reading here to its latest by the API-rate price of this workspace's calls in between, and prices each call at that rate. Until a window has risen the page shows no estimate for it. After a window resets the estimate keeps the last reading's rate and says so, and dashed lines mark where the window started and reset.

On a narrow window a provider's detail page is headed by the provider's name (for example "Claude subscription") in place of "Usage", and the provider list above it is left out when there is only one provider. Its latest turns, and a turn's calls, become two-line rows there instead of a table too wide to see: the source and its share of each limit (or its cost) first, then the time, model, calls, tokens and duration, and the status only when a call failed; a turn's call leads with its time, since its source is the turn's.
