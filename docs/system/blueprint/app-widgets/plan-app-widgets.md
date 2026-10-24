# App widgets and the usage widget

This is the spec for app widgets: a small, always-on-top view an app declares in its manifest and the shell draws on the desktop (top right until the user moves it), above every window, for every client of the workspace.
It ships with one widget, the Usage app's, which shows at a glance how much of each subscription limit is used and what pay-as-you-go spend has reached this month, the way a system-stats menu shows CPU and memory.
It is written for the people and agents implementing it, and it is the reference the implementation is judged against.
It builds on the desktop interface V1 ([plan-desktop-interface.md](../desktop-interface/plan-desktop-interface.md), [concepts.md](../desktop-interface/concepts.md), [contracts.md](../desktop-interface/contracts.md)), borrows its floating layer and per-client presentation from the pinned taskbar entries ([plan-pinned-taskbar-entries.md](../pinned-taskbar-entries/plan-pinned-taskbar-entries.md)), and amends those documents and the stop-when-no-windows spec where section 9 says.

## 1. Purpose and principles

A window is the wrong shape for a number you want to see without asking for it: it has a title bar, takes a place in the stack, and is buried as soon as you work in another window.
A widget is a page of an app that the shell keeps visible wherever the user puts it, small, translucent until hovered, and collapsible to a one-line pill.
It shows; it is not worked in. A click on it opens the app's window.

The five principles of the workspace app model hold: one owner per fact; the shell is generic and names no app; truth is shared while arrangement is scoped; minimal shell state; no two-phase commits.
Applied here:

- **The shell names no app.** An app opts in with a `[widget]` table; the shell draws whatever page it names and knows nothing of usage. The shell's ratchets (`test_project_ratchets.py`) hold unchanged.
- **The app owns the content, the shell owns the frame.** The page draws both its expanded and collapsed forms and says how big it is; the shell decides where the frame is, whether it is collapsed, how translucent it is, and how it moves.
- **Whether a widget is on is shared truth; where it sits is the client's arrangement.** One workspace-wide switch per widget, like the avatar selection; position and collapsed state per client, like a pinned entry's presentation.
- **A widget that is on keeps its app up**, as a window does (section 3.5).

## 2. Glossary

| Term | Meaning |
|---|---|
| Widget | An app's manifest declaration of a page the shell shows on the desktop, and the frame the shell draws it in |
| Widget page | The page at the widget's path under the app's origin; an ordinary page of the app that speaks the app contract |
| Enabled | Whether the workspace shows a widget at all; shared, one value per widget |
| Position | Where one client draws a widget it has moved: a point in fractions of the backdrop, or none for the default stack top right; per client |
| Mode | How one client draws a widget: `expanded` or `collapsed`; per client |
| Rest opacity | The translucency of a widget the pointer is not over |

## 3. The model

### 3.1 The declaration

A widget is declared in an app's manifest as a `[widget]` table and copied onto the app's registry row by `forward_port.py`, the way `pin` is.
The set of apps with a widget is exactly the registered, non-internal apps whose row carries one; nothing stores the set.

| Field | Type | Required | Default | Rule |
|---|---|---|---|---|
| `path` | string | yes | | The widget page: a path under the app's origin obeying the launch-path rule (one leading slash, no query string, path characters only) |
| `default_enabled` | bool | no | `true` | Whether the widget shows in a workspace that has made no choice about it |
| `tray` | bool | no | `false` | Whether the page draws a tray form (mode `tray`), so a client may put the widget in the taskbar's tray (section 4.10) |
| `toolbar` | bool | no | `false` | Whether the page draws a toolbar form (mode `toolbar`), so a client may put the widget in the taskbar's row, beside the launcher (section 4.10) |

### 3.2 Enabled, shared

Whether each widget shows is one shared value for the workspace, held in a small state file (section 5.1) and broadcast when it changes, so every window of every client changes together.
A widget with no stored value takes its manifest's `default_enabled`.
A stored value naming an app that no longer declares a widget is kept (so a widget withdrawn and restored keeps the user's choice) and ignored.

### 3.3 Presentation, per client

How one client shows a widget is three values on the client record, keyed by app: its position, its mode, and its placement (`desktop`, the default, `tray` or `toolbar`, section 4.10).
They are global to the client rather than per desktop, for the reason the pinned entries give: an always-on-top thing that jumped when the desktop switched would read as broken.
A client with no presentation for an app, or one with no position, draws it in the default stack top right, `expanded`.

Top right rather than bottom right because the chat's floating avatar defaults to the bottom-right corner, and the taskbar's tray sits under it.

A widget can be put anywhere on the backdrop. Its position is the corner of its box nearest the backdrop's corner of the quadrant it sits in, in fractions of the backdrop (`{"x", "y"}`, each 0 to 1): a widget by the right edge is kept by its right edge, one in the lower half by its bottom edge. So when its page reports a new size (on a collapse, or as its content changes) it grows away from the edges it sits by rather than off them, and a backdrop of another size keeps it in the same relative place. The box is then shifted, if need be, to lie wholly inside the backdrop.

### 3.4 The default stack

Widgets with no position stack downward from the top-right corner in registry order, separated by the theme's widget gap, inset from the backdrop's edges by the theme's widget inset.
The stack starts a window title bar's height below the inset, so a maximized window's minimize, maximize, and close controls stay clear of it.
A moved widget takes no place in the stack; moved widgets may overlap each other or the stack, as the user put them.

### 3.5 Lifecycle

A widget that is enabled keeps its app's program running: the no-window rule of the stop-when-no-windows spec (section 6.1) counts it as it counts a window, and clears the app's idle mark.
This follows that spec's own principle (section 6.2: "the rule is the user's arrangement, not the browser's"): the widget counts because it is on, not because some browser is drawing it, so a workspace no one is looking at keeps an enabled widget's app up exactly as it keeps a minimized window's.
A disabled widget counts for nothing.
Quit (spec section 7) still stops the program at once; an enabled widget's frame then wakes it on its next request, through the port parker, as any request does. The widget's menu says so: Quit is not offered there, and "Hide widget" is.

### 3.6 Who sees it

Every client of the shell draws every enabled widget.
No per-visitor gating is needed: the share gateway admits a visitor holding only a per-app grant to that app's origin and not to the shell (`shell/share_grants.py`), so every client the shell has holds the workspace grant, which reaches every app's origin.

### 3.7 Ownership

| Fact or verb | Owner |
|---|---|
| Whether an app has a widget; its path and default | The app's manifest, through the registry |
| Whether a widget is enabled | Shell, shared |
| A widget's position and mode | Shell, per client, on the client record |
| The widget's size | The widget page, reported per frame; never stored |
| What the widget shows | The app |
| Where each widget is drawn | Derived in the browser |

### 3.8 Invariants

- Nothing in the shell package names an app, imports mngr, or runs mngr.
- A client draws at most one frame per enabled widget, wholly inside the backdrop.
- No widget frame is larger than the theme's widget maximum, whatever its page reports.
- An enabled widget's app is never stopped by the no-window rule.

## 4. Behaviour

### 4.1 The layer

Widgets are drawn in their own layer, a sibling of the floating entries' layer inside the backdrop's stacking context, at the same sticky level: above every window and live page (a maximized window goes under them), below the launcher overlay, menus, and modals.
The layer is inert; only each widget's own box takes a press.
The backdrop ends above the taskbar, so no widget is drawn over it.

### 4.2 The frame

A widget's frame is an iframe of the app's `url` plus the widget's `path`, with the shell's chrome around it: a light panel (the theme's widget surface, edge, and radius) with no bar, which the page fills but for a thin rim of the theme's rim width on every side. The rim is the widget's handle. The page draws on a transparent background over the panel.
The frame is not drawn until its page first reports a size, so a page that is loading or fails to load leaves no empty box on the desktop; a page that never reports is logged once and never drawn.
The frame's size is the last size its page reported for the current mode, clamped to the theme's widget maximum (320 by 240) and minimum (24 by 24), plus the rim on each side.
At rest the frame is drawn at the rest opacity; while the pointer is over it, or a menu of it is open, fully opaque.

### 4.3 Collapse and expand

A double-click on the rim, or Collapse or Expand in its menu, toggles the client's mode for that widget and writes it to the client record.
The shell tells the page its mode after every handshake and on every change; the page draws the matching form and reports its size again.
The page decides what each form shows; the shell never draws a pill of its own.

### 4.4 Moving

A press anywhere on the widget drags it. On the rim, the shell's gesture layer takes it, with its threshold, pointer capture, and click suppression, as a floating entry drags. On the page, the page's contract reports the drag (`shell:widget-drag`, section 6), and the shell moves the widget the same way; a press that travels less than the contract's threshold stays the page's click, and hover is the page's throughout, since nothing covers it.
During the drag the frame follows the pointer, held inside the backdrop; on release it stays where it was let go, and its position (section 3.3) is written to the client record once. A drop where it already was writes nothing.
A press that never travels the threshold is a click on the rim, which does nothing.

### 4.5 Clicking

A click inside the frame is the page's.
The widget page opens its app by a link to its own origin with `target="_blank"`: under the app contract's link rule (contracts section 7) that is a new-window click on the page's own origin, which sends `shell:open` with `ifPresent: "focus"`.
A widget stands for its app, not for one page of it, so from a widget that open raises any window of the app, whatever page it is on: the one this client used last on the active desktop, else the first on another desktop (switching to it); a window is opened at the link's path only when the app has none.

### 4.6 Menus

The rim's context menu (right click, or long press on touch):

1. Collapse or Expand (on the desktop only)
2. Move to desktop, Move to toolbar, Move to tray: each place it is not, that its page draws a form for (not on a phone)
3. Open <display name>, as the page's own link does
4. divider
5. Hide widget, which disables it for the whole workspace, saying so: "Hide the <display name> widget for everyone on this workspace"

A right-click anywhere on the widget's page opens the same menu (the page's contract sends `shell:widget-menu`, section 6), as does one on a taskbar slot.

A hidden widget is shown again from its app's shortcut menu and its launcher row's menu, which gain "Show widget" when the app declares a widget that is disabled, and "Hide widget" when it is enabled.

### 4.7 Hidden by its page

A page may say it has nothing to show (the Usage widget before any usage exists).
The shell then draws nothing for it on that client and keeps the frame loaded, so the page can say otherwise; the widget still counts as enabled for the lifecycle rule.

### 4.8 Phone

The phone layout has no floating layer.
In a strip just above its bar (the bar's two controls and pill already fill a phone's width), it draws each enabled widget not hidden by its page, always collapsed, no taller than the phone's widget height.
A tap is the page's, as a click is on the desktop; its link opens the app's window, which the phone shows.
There is no rim, menu, or corner on a phone; each widget is the same panel, filled by its page.

### 4.9 Touch

Touch mode uses long press on the rim for the menu, as elsewhere; the rim is wider (12 pixels) so a finger can press it, and the frame is fully opaque.

### 4.10 The taskbar: tray and toolbar

A client may put a widget in the taskbar instead, in one of two places: the tray, beside the desktop switcher, for a page that draws a tray form; or the bar's row (the toolbar), right after the launcher and before the windows' entries, for a page that draws a toolbar form. Either way it is always there, never covering a window, the closest thing to a menu-bar monitor. The trade is a smaller glance, and no reading of the detail without a click or a hover. The tray form is small and sits with the system's own; the toolbar form is drawn to sit among the entries, as tall as one and in the bar's own text.

- Its frame is moved to a host in the taskbar (it reloads once, on the move from the desktop) and told mode `tray` or `toolbar`; it is laid over a slot the size its page reports for that form: in the tray no taller than `--desk-widget-tray-height` and no wider than `--desk-widget-tray-max-width`, in the row no taller than `--desk-taskbar-entry-size` and no wider than `--desk-widget-toolbar-max-width`, with an entry's hover and active tints.
- A click on it is not the page's: the page's contract sends `shell:widget-press` and the shell opens its popover above the bar, over the slot's end of it (the tray's right end, or the row's start), the desktop widget's panel at the size the page reports expanded. The same frame is laid over the popover and told `expanded`, so opening it reloads nothing; the slot keeps its taskbar size meanwhile, as a placeholder.
- The popover closes on a click on the slot again, a press anywhere else in the shell, Escape (in the shell, or on the page, whose contract sends `shell:widget-dismiss`), or the page opening its app.
- Tooltips are shown as on the desktop. A widget in the taskbar does not move.
- The phone layout has no tray or toolbar: a widget placed there is drawn in the phone's strip like any other.

## 5. The shell backend

### 5.1 State files

- `widgets.json`, new: `{"version": 1, "apps": {"<app>": {"is_enabled": bool}}}`; absent means every widget takes its manifest default.
- `clients.json` (version 2, unchanged): each client gains `widgets`, a map from app name to `{"position": {"x", "y"} | null, "mode"}`; absent means the defaults.

### 5.2 The manifest and the registry

`app_manifest` gains the `[widget]` table of section 3.1, and `RegistryRow` gains `widget`.
`forward_port.py` copies it onto the row as it copies `pin`, refusing a malformed one with the same shape of error.
The inventory's and `apps_updated`'s `app` object carries `widget` (the table, or `null`).

### 5.3 Routes and messages

| Route | Change |
|---|---|
| `GET /api/widgets` | new: `{"widgets": [{"app", "is_enabled"}]}`, one per app declaring a widget, the effective value |
| `POST /api/widgets/<app>` | new: `{"is_enabled"}`; writes `widgets.json`, broadcasts `widgets_updated`, answers as `GET`; `404` for an app declaring no widget |
| `POST /api/clients/<client_id>/widgets/<app>` | new: `{"position", "mode"}`; answers the client record and announces `client_widgets_changed` to that client's windows |
| `GET /api/inventory` | gains `widgets`, as `GET /api/widgets` answers it |

New socket messages:

| Type | Payload | When |
|---|---|---|
| `widgets_updated` | `{"widgets": [{"app", "is_enabled"}]}` | on connect, after `widgets.json` is written, and after a registry read that changed the rows |
| `client_widgets_changed` | `{"client_id", "widgets"}` | to that client's windows, after its widget presentation was written |

### 5.4 The lifecycle rule

`AppLifecycleManager` gains an enabled-widgets reader beside its window counts and granted apps: a running app that declares `stop_when_no_windows`, is not `internal`, and has an enabled widget clears its idle mark, as one with a window does.
The reader reads `widgets.json` and the registry once per sweep pass, and only when the pass finds a running, windowless app that declares the field.

### 5.5 Agent ops

None in this version: no op enables, disables, or moves a widget. `desktops` and `list` answer the inventory document, so they list the switches with it.
Whether a widget is on is the user's choice and the manifest's default.

## 6. The app contract

The app contract evolves by adding types (contracts section 7); nothing shipped changes meaning.

| Direction | Type | Payload |
|---|---|---|
| shell to page | `shell:handshake` | gains `surface`: `"window"` (the existing case; a handshake without it reads as a window) or `"widget"`; a widget's handshake carries `windowId: null` and the client's active desktop as `desktopId`, sent again when it changes |
| shell to page | `shell:widget-mode` | `{"mode": "expanded" \| "collapsed" \| "tray" \| "toolbar"}`; to a widget page only, after every handshake and on every change; `tray` (`toolbar`) only to a page whose manifest says it draws a tray (toolbar) form (section 4.10) |
| page to shell | `shell:widget-size` | `{"width", "height"}`, CSS pixels of the page's content in its current mode; the shell clamps it (section 4.2) |
| page to shell | `shell:widget-hidden` | `{"isHidden": bool}`; section 4.7 |
| page to shell | `shell:widget-tooltip` | `{"text", "left", "top", "width", "height"}` (the element's place in the frame), or `{"text": null}`; section 7.5a |
| page to shell | `shell:widget-menu` | `{"x", "y"}`, a right-click on the page in the frame's pixels: the widget's menu (section 4.6) |
| page to shell | `shell:widget-press` | `{}`, from a widget in the taskbar (tray or toolbar): a click on it, which opens or closes its popover (section 4.10) |
| page to shell | `shell:widget-dismiss` | `{}`, Escape on a widget's page: its popover closes if it is open |
| page to shell | `shell:widget-placement` | `{"placement": "desktop" \| "tray" \| "toolbar"}`, from any page of an app declaring a widget (its window too): the shell places the app's widget so for this client, as the menu's Move rows do; the tray or toolbar only for a widget whose page draws that form, never on a phone |
| page to shell | `shell:widget-drag` | `{"phase": "start" \| "move" \| "end" \| "cancel", "screenX", "screenY"}`, and at the start `"pressX", "pressY"` (where in the frame the press began) and `"pressScreenX", "pressScreenY"`; section 4.4 |

From a widget page the shell ignores `shell:location`, `shell:focused`, and `shell:capabilities`, and takes `shell:open`, `shell:open-link`, `shell:message`, `shell:start-with-text`, and `shell:draft-text` as from a window, on the client's active desktop.
`connectToShell` gains `onWidgetMode(mode)` among its handlers and `widgetSize(width, height)` and `widgetHidden(isHidden)` on the connection, plus `surface()`, so one page module serves both a window and a widget.
On a widget it also sends `shell:widget-tooltip` itself for any element marked `data-widget-tooltip` the pointer enters (and one with no text once it leaves every such element, or presses), which the shell shows with the workspace's own tooltip (`showTooltipAtRect` in `hoverTooltip.ts`) under the element's place, after the usual delay. It also sends `shell:widget-drag` itself, with no code in the page: a primary press that travels `WIDGET_DRAG_THRESHOLD_PX` (4) is a drag, reported in screen pixels (which stay true while the shell moves the frame under the pointer) until it ends, and the click that ends it is swallowed before the page or the link rule sees it. A press on a control (a button, a field, an element marked `data-no-drag`) is never a drag, and the browser's own drag of a link is prevented. It also sends `shell:widget-menu` for a right-click, `shell:widget-dismiss` for Escape, and in mode `tray` swallows a click and sends `shell:widget-press` instead (and never a drag), so a page needs no code for the tray either.

## 7. The Usage widget

### 7.1 The manifest

```toml
[widget]
path = "/widget"
default_enabled = true
```

### 7.2 The page

`/widget` is a second Vite entry of the Usage app (`frontend/widget.html`, `src/widget.ts`), a small bundle beside the dashboard's, served by the Flask server like the dashboard's page.
It reads what the dashboard reads, through the same server's `/api/usage/*` relay and its stream, and reuses the dashboard's models (`models/api.ts`, `models/providers.ts`, `models/format.ts`) so the two can never disagree: limit levels come from the same `limitLevel()` (75% high, 90% critical) and a window reset since its reading is recognised by the same rule.

### 7.3 The look

The widget follows a Mac system monitor's panels (iStat Menus): the shell's light panel, small uppercase headings, figures in tabular numerals, and rings over faint tracks. A ring is drawn in its window's own hue while the window is comfortable (the 5-hour limit cyan, the weekly one violet) and in its level's colour as it nears the cap (orange at 75%, red at 90%, from the same `limitLevel()`); each row's dot is its ring's colour.

### 7.4 Expanded

At the medium size about 224 pixels wide, a section per subscription in the dashboard's card order, then one section of pay-as-you-go spend:

- A subscription: its name as the heading; beneath it, its ring pair (section 7.5) beside a row per window (the 5-hour and the weekly limit), each with its dot, its name, and the share used.
- Pay-as-you-go: "API this month" as the heading, and a row per provider with this month's spend, with `~` when any of it is estimated.
- A provider with no data is not drawn.

### 7.5 Collapsed

A pair of concentric rings per subscription, in the expanded order, after the activity rings of a watch: the longest window (the weekly limit) outside and the shortest (the 5-hour limit) inside, with the 5-hour percentage in the middle and the subscription's short name in small capitals beneath, one alone as well. Each ring is a faint full track with, over it, the share used drawn clockwise from twelve o'clock.
With no subscription, the collapsed widget is one figure of this month's total spend (`$165.21`).

### 7.5a Tooltips

Nothing in the widget changes under the pointer. The detail a hover asks for is a tooltip: a ring pair's says both windows (each one's share used and reset time) and the reading's time, a window's row or bar says its window ("5-hour limit: 61% used, resets 7:00 PM"; a weekly reset gives its weekday), a subscription's name the whole subscription, a spend row its call count. The page marks each with `data-widget-tooltip`, and its app contract hands it to the shell (`shell:widget-tooltip`, section 6), which shows the workspace's own tooltip under the element's place outside the widget's frame: a bubble drawn in the page would be clipped by the small frame, and a native `title` is not shown by every host of the desktop (Imbue Studio shows none).

### 7.6 States

| State | Expanded | Collapsed |
|---|---|---|
| A window reset since its reading | `—` in place of its percentage | its ring is a bare track |
| An old reading not yet reset | drawn as usual; the time is in the tooltip | as usual |
| Stream disconnected | every row dimmed, last values kept, a small "Reconnecting" dot | the pill dimmed |
| Collector catching up | one row, "Indexing usage..." | `...` |
| No usage data at all | `shell:widget-hidden {isHidden: true}`, sent again with `false` on the first event | the same |

### 7.7 Opening the dashboard

The whole widget is one link, `<a href="/" target="_blank">`, so a click raises the Usage window (section 4.5).

### 7.8 The look: style, size and detail

How the widget looks is the Usage app's own setting, not a flag and nothing the shell knows of: fields of `data/.apps/usage-dashboard/settings.json`, read and written through `GET` and `POST /api/settings`, and picked beside "Widget" at the foot of the Usage page, on the left. The first pick is where the widget goes (`widget_place`: Desktop, the default, Toolbar or Tray); only that place's choices follow it: on the desktop its style, size and detail; in the toolbar or the tray its design there and its detail. Every pick also asks the shell to put this client's widget in the picked place (`shell:widget-placement`, section 6), where the pick is seen. The widget reads them with its figures; a pick in the same browser reaches it at once over a `BroadcastChannel`, and one made elsewhere on its next read (within a minute). `?variant=`, `?size=`, `?detail=`, `?tray=` and `?toolbar=` on the widget's URL override them for that page alone, to look at a choice without changing anyone's widget. Every style reads the same `WidgetModel`, so adding one is a view, a name in `models/widgetLook.ts`, and a member of the server's `WidgetVariant` (`settings.py`), which the settings route checks every pick against.

| Style (`widget_variant`) | Expanded | Collapsed |
|---|---|---|
| `rings` (default) | section 7.4 | section 7.5 |
| `minimal` | the ring pairs alone in a row, the 5-hour figure in each; nothing else in view (less: the 5-hour ring alone; more: the weekly figure under each pair); the panel fits them rather than taking the expanded width | the same, the rings a size smaller |
| `bars` | a row per subscription: its name, the 5-hour bar above a thinner weekly bar, and their shares used (the 5-hour one in bold); then the API spend | the short name, both bars, and the 5-hour share |

| Detail (`widget_detail`) | What it adds or takes away |
|---|---|
| `less` | the 5-hour limit alone (one ring, one bar, one row), and no API spend |
| `standard` (default) | sections 7.4 and 7.5, and the bars as above |
| `more` | in view rather than only in tooltips: each limit's reset time (expanded), the reading's time beside each heading (expanded rings), the weekly figure and the 5-hour reset beside a collapsed ring pair, and the weekly figure beside the collapsed bars |

In the taskbar (section 4.10) the widget draws the design the workspace picked for where it is: in the tray the tray design (`widget_tray`), in the bar's row the toolbar design (`widget_toolbar`), whatever size was picked; detail still applies (less: the 5-hour limit alone). They are picked once Toolbar or Tray is the picked place. A click opens the widget expanded in the popover.

| Tray design (`widget_tray`) | Per subscription |
|---|---|
| `rings` (default) | a 22-pixel ring pair beside the name in small capitals over the 5-hour figure (more: and the weekly one) |
| `ring` | the ring pair alone, like a menu-bar icon; the name and figures are in its tooltip |
| `bars` | the name over the 5-hour and weekly bars, beside the 5-hour figure |
| `numbers` | the name and the 5-hour figure as text (more: and the weekly one) |
| `stacked` | the name beside the 5-hour figure over the weekly one, each with its colour's dot |

| Toolbar design (`widget_toolbar`) | Per subscription, in the bar's 13-pixel text, as tall as an entry |
|---|---|
| `chip` (default) | like a window's entry: the ring pair as its icon, the name and the 5-hour figure as its title (more: and the weekly one) |
| `meters` | upright 5-hour and weekly meters, 24 pixels tall, beside the name over the 5-hour figure |
| `inline` | the names and 5-hour figures as one line of text, the subscriptions joined by a middle dot |
| `line` | the name and the 5-hour figure over a thin line per window, filled to its share |

A figure near its cap takes its level's colour in every tray and toolbar design.

| Size (`widget_size`) | Text | Ring | Collapsed bars | Expanded width |
|---|---|---|---|---|
| `small` | 11px | 32px | 44px | 212px |
| `medium` (default) | 12px | 40px | 60px | 224px |
| `large` | 14px | 56px | 84px | 284px |

A size is a set of `--w-*` lengths on the widget's root (`style.css`), and the widget's text is set in `em` against its base, so everything in it scales together.

## 8. Theme tokens and selectors

Tokens, read into the metrics record with the others: `--desk-widget-inset: 16px`, `--desk-widget-gap: 8px`, `--desk-widget-max-width: 320px`, `--desk-widget-max-height: 240px`, `--desk-widget-min-size: 24px`, `--desk-widget-rest-opacity: 0.72` (touch: `1`), `--desk-widget-rim: 6px` (touch: `12px`), and, read by no behaviour, `--desk-widget-radius: 12px`, `--desk-widget-surface: rgb(255 255 255 / 0.94)`, `--desk-widget-edge: rgb(0 0 0 / 0.08)`, `--desk-widget-tray-height: 36px`, `--desk-widget-tray-max-width: 240px`, `--desk-widget-toolbar-max-width: 320px`, `--desk-phone-widget-height: 32px`.

| Attribute | On |
|---|---|
| `data-widgets` | the widget layer |
| `data-widget="<app>"`, `data-widget-moved="true\|false"`, `data-widget-mode="expanded\|collapsed"` | each widget's frame |
| `data-widget-grip="<app>"` | each widget's chrome too, whose rim is its handle |
| `data-menu-row="widget-collapse\|widget-expand\|widget-to-tray\|widget-to-toolbar\|widget-to-desktop\|widget-open\|widget-hide\|widget-show"` | the widget rows of the rim, shortcut, and launcher menus |
| `data-tray-widget="<app>"`, `data-popover-open="true\|false"` | each tray widget's slot (which carries `data-widget="<app>"` too) |
| `data-toolbar-widgets`, `data-toolbar-widget="<app>"` | the box in the bar's row holding the toolbar widgets' slots (and a toolbar widget's popover), and each slot (which carries `data-widget="<app>"` and `data-popover-open` too) |
| `data-widget-popover="<app>"` | a taskbar widget's open popover |
| `data-tray-widget-frames` | the taskbar widgets' frames' host, in the taskbar |
| `data-widget-frames`, `data-widget-frame="<app>"` | the widget frames' host, and each widget's iframe |
| `data-phone-widgets`, `data-phone-widget="<app>"` | the strip above the phone's bar, and each widget in it |

## 9. Amendments to other documents

Landed with this plan:

- contracts.md 2, 3, 4, 5, 6, 7, 11, 12, 13: the manifest table, the row field, the files, the routes, the messages, the contract types, the tokens, and the selectors above.
- `docs/system/specs/stop-when-no-windows.md` 6.1 and 6.2: an enabled widget clears the idle mark.

Landed with the implementation:

- plan-pinned-taskbar-entries.md 4.2: the floating layer has a sibling widget layer at the same level.
- plan-phone-interface.md: a strip above the bar draws collapsed widgets.
- The `build-app` skill's manifest notes: the `[widget]` table, and that an app declaring one must answer its page cheaply, since it is loaded by every client.
- The Usage app's README: the widget page.

## 10. Testing

Unit tests, beside the code:

- The manifest table and its refusals; the registry copy (`forward_port.py`); the inventory field.
- `widgets.json`: defaults, a stored value, an unusable file; the switch routes and their refusals; the client record's `widgets` and its route.
- The lifecycle rule: an enabled widget clears the idle mark until it is hidden; an internal app's widget holds nothing up.
- `app_contract.ts`: the `surface` field, the four new types, that a widget sends no location or focus, and that a press on a widget's page past the threshold is reported as a drag whose click is swallowed, while a short press, a press on a control, or one on a window is not.
- Geometry: the default stack and its order, its title-bar offset, size clamping, a moved widget anchored by its corner nearest the edges and kept inside the backdrop, and the position a drop is stored as.
- The store: switches and presentations from the inventory and the pushes, drawing only after a size report, the optimistic mode write and its undo, the drag and the position it writes, show and hide.
- Usage widget models: rows and the pill from fixture readings and totals, an empty read, a collector still indexing or unreachable.

End to end, with a stub app declaring a widget whose page reports a size per mode:

- `test_e2e.py`: the widget appears top right below a title bar's height, at the size its page reported; a double-click on its rim changes it to the collapsed size; a drag of the rim leaves it where it is let go, written to the client record by its nearest corner and kept across a reload; a drag that starts on its page moves it too and opens nothing; a click on its `_blank` link raises one window of its app however often it is clicked; Hide widget removes it and turns the switch off.
- `test_e2e.py`, phone viewport: the widget is drawn collapsed in the strip above the bar, and a tap on its link opens its app's window, which the phone shows.
- `test_app_lifecycle_system.py`: an app declaring `stop_when_no_windows` is not stopped with its widget enabled and no window, and is stopped once the widget is hidden.

## 11. Delivery

This is one pull request on `yuan/app-widgets`, stacked on `yuan/usage-tracking`, merged after it.
A suggested commit order, each commit leaving the repository green:

1. The manifest table, the registry and inventory fields, `widgets.json`, and its routes.
2. The app contract's types.
3. The widget layer, frame, collapse, positions, the default stack, and menus, with the stub-app end-to-end tests.
4. The lifecycle rule.
5. The phone bar.
6. The Usage widget page and its manifest table.
7. The remaining document amendments of section 9 and the changelog entries for `system_interface`, `workspace_ui`, and `usage_dashboard`.

## 12. Deferred

- A native, always-on-top widget outside the workspace (a macOS menubar item or panel from Imbue Studio); this design keeps all usage data inside the workspace, and a native host would load the same widget page.
- Per-user enablement; this version has the shared switch only.
- Agent ops for widgets.
- Alerts (toasts, sounds) at a limit's level.
- A System Monitor widget, which this contract makes a manifest table and a page.
