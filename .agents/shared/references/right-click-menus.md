# Right-click menus

Every right-click in the workspace opens one menu: the element menu, which every page installs
(`installElementContextMenu`; a scaffolded app's page script does it, see the `build-app` skill). In the Imbue
Studio desktop app on macOS it is a native menu: the system's items (spelling suggestions, Look Up, Share,
Speech, Services) with the page's own rows and "Copy reference" / "Explain..." / "Modify..." among them. Anywhere
else (the desktop app on Linux, a browser, a sharing link) the page draws the same rows as its own HTML menu.

A page gets that only by leaving the right-click to the element menu. A `contextmenu` handler that calls
`preventDefault` keeps the native menu away, so an app never draws a right-click menu of its own: it hands its rows
to the element menu instead.

## Giving an element its own rows

Return the rows from `extraRows(target)`. It runs at each right-click; `target.element` is what was clicked, so
mark each row of a list with the `data-*` attribute of the record it shows and find it with `closest`:

```js
installElementContextMenu({
  connection,
  handshake: () => handshake,
  extraRows: (target) => {
    const item = target.element.closest("[data-task-id]");
    if (item === null) return [];
    const id = item.dataset.taskId;
    return [
      { kind: "action", key: "rename", label: "Rename", symbol: "pencil", onSelect: () => rename(id) },
      { kind: "check", key: "pinned", label: "Pinned", isChecked: isPinned(id), onToggle: () => togglePinned(id) },
      {
        kind: "submenu",
        key: "move",
        label: "Move to",
        rows: () => lists.map((list) => ({
          kind: "action", key: `move:${list.id}`, label: list.name, onSelect: () => move(id, list.id),
        })),
      },
      { kind: "divider" },
      { kind: "action", key: "delete", label: "Delete", tone: "danger", symbol: "trash", onSelect: () => remove(id) },
    ];
  },
});
```

- A row is an `action` (`onSelect`; `isDisabled` greys it, `tooltip` says why), a `check` row (`isChecked`,
  `onToggle`), a `divider`, or a `submenu` of those, one level deep. A submenu's `rows` is read once, at the
  right-click.
- Every `key` is unique across the whole menu, a submenu's rows included: a pick in the native menu comes back by
  key.
- `symbol` names an SF Symbol (`pencil`, `trash`, `folder`) the native menu shows beside the row; `tone` and `icon`
  style the row only where the page draws the menu.
- The desktop app keeps at most 20 rows in each section and each submenu, and the first 120 characters of a label.
- Doing something else on the right-click, without the menu, is fine: a handler that only selects the row under
  the pointer (and calls no `preventDefault`) leaves the menu to the element menu.

A Mithril page hands the installer `open: createContextMenuOpener().open` so the menu it draws is the shared Menu;
the Files app (`system/apps/files/frontend/src/index.ts`) is a working example of `extraRows`.

## When a page has to keep the right-click

A native menu is a snapshot taken at the right-click, closed by any pick. What it cannot show stays in a menu the
page draws on its own: a search field or any other control inside the menu, rows that change while it is open, a
pick that keeps the menu up, a second level of submenu. A right-click that is input for something else (a remote
desktop, a canvas) or that only closes something already open also keeps its handler.

Such a handler says why in a marker among the comment lines directly above it:

```js
// own-context-menu: forwards -- the canvas takes the right-click as an erase stroke
canvas.addEventListener("contextmenu", (event) => event.preventDefault());
```

The reason is one of `forwards` (the right-click is input to something on the page), `dismisses` (it only closes or
raises something already open), `touch` (it stops a long press from opening the browser's menu), `custom-content`
(the menu holds what a native one cannot, as above), or `element-menu` (the element menu's own fallback).
`system/test_context_menu_rules.py` fails on an unmarked handler that calls `preventDefault` on a right-click, in
the template's apps and in yours alike. Name every marker you add in your report; before adding one, check whether
the menu's rows fit the element menu after all, and move them there if they do.
