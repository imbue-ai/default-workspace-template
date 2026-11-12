# Right-click menus

Every right-click in the workspace opens one menu: the element menu, which every page installs
(`installElementContextMenu`; a scaffolded app's page script does it, see the `build-app` skill). It lists the
page's own rows for what was clicked, then the standard rows (Copy, Paste, the link and image rows), then "Copy
reference" / "Explain..." / "Modify...". It follows the workspace's theme, and it looks and behaves the same in a
built-in app and in one you build.

In the Imbue Studio desktop app, a right-click on text being edited, on text the user selected, or on a link the
page has no rows of its own for opens the system's native menu instead (spelling suggestions, Look Up, Paste). So
does any right-click with Shift held. None of the page's rows appear there.

A page gets all of that only by leaving the right-click to the element menu. A `contextmenu` handler that calls
`preventDefault` draws a menu of its own in its place, so an app never draws a right-click menu of its own: it
hands its rows to the element menu instead.

## Giving an element its own rows

Return the rows from `extraRows(target)`. It runs at each right-click; `target.element` is what was clicked, so
mark each row of a list with the `data-*` attribute of the record it shows and look it up with `closest`:

```js
installElementContextMenu({
  connection,
  handshake: () => handshake,
  extraRows: (target) => {
    const item = target.element.closest("[data-task-id]");
    if (item === null) return [];
    const id = item.dataset.taskId;
    return [
      { kind: "action", key: "rename", label: "Rename", icon: "edit", onSelect: () => rename(id) },
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
      { kind: "action", key: "delete", label: "Delete", tone: "danger", icon: "trash", onSelect: () => remove(id) },
    ];
  },
});
```

- A row is an `action` (`onSelect`; `isDisabled` greys it, `tooltip` says why), a `check` row (`isChecked`,
  `onToggle`), a `divider`, or a `submenu` of those, one level deep. A submenu's `rows` is read each time the
  submenu opens, so it only reads state and changes nothing.
- Every `key` is unique across the whole menu, a submenu's rows included.
- `tone` is `"danger"` for a destructive row or `"quiet"` for a secondary one.
- `icon` names one of the workspace's shared icons (the keys of `STROKE_PATHS` in
  `system/libs/workspace_ui/src/components/icons.ts`, `trash` and `edit` among them), or is
  `{ markup: "<svg ...>" }` for a glyph of the app's own.
- Doing something else on the right-click, without the menu, is fine: a handler that only selects the row under
  the pointer (and calls no `preventDefault`) leaves the menu to the element menu.

These are the rows every built-in app's right-click menu is made of too, so whatever a built-in menu shows, yours
can. A Mithril page hands the installer `open: createContextMenuOpener().open` so the menu it draws is the shared
Menu. The chat rail (`railContextMenuRoutes` in `system/apps/chat/frontend/src/root/ChatRail.ts`, installed in
`root/index.ts`) is a working example. Its `open` goes further than a page that only adds rows needs: it draws the
rail's own row menu, reading the rows afresh while the menu is up.

## When a page has to keep the right-click

The element menu's rows are a list of things to pick. What that cannot show stays in a menu the page draws on its
own: a search field or any other control inside the menu, a pick that keeps the menu up, a second level of
submenu. A right-click that is input for something else (a remote desktop, a canvas) or that only closes something
already open also keeps its handler.

Such a handler says why in a marker among the comment lines directly above it:

```js
// own-context-menu: forwards -- the canvas takes the right-click as an erase stroke
canvas.addEventListener("contextmenu", (event) => event.preventDefault());
```

The reason is one of `forwards` (the right-click is input to something on the page), `dismisses` (it only closes or
raises something already open), `touch` (it stops a long press from opening the browser's menu), `custom-content`
(the menu holds what the element menu's rows cannot, as above), or `element-menu` (the element menu itself).
`system/test_context_menu_rules.py` fails on an unmarked handler that calls `preventDefault` on a right-click, in
the template's apps and in yours alike. Name every marker you add in your report; before adding one, check whether
the menu's rows fit the element menu after all, and move them there if they do.
