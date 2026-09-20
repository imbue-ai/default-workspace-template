The files app follows phases 6 and 7 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`):

- The app's Python package (the instances API wrapper around dufs) is deleted. The supervisord program line registers the app through `forward_port.py --manifest` and then runs dufs directly, serving `data/` with the app's assets.

- The page honours the `path` launch parameter: opening `/?path=/notes/` lands in that folder (a path that would leave the served root is ignored), so the `new` launch path and `layout.py open files --param path=...` both work.

- The manifest declares launch paths only; the retired `instances` and `actions` keys are gone.
