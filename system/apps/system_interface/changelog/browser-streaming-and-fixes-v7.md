The service websocket proxy no longer negotiates permessage-deflate on /service/ paths (proxied services carry compressed binary streams -- video, terminal -- where deflate is pure re-compression waste) and sets TCP_NODELAY on both sides of every proxied websocket so interactive messages are never held back by Nagle's algorithm.

Browser pane tabs now have a "Destroy browser" button, symmetric to the destroy buttons on agent and terminal tabs: it confirms, retires the browser in the fleet, and closes the tab (a plain "Close tab" still just detaches the pane).

The service proxy now rejects cross-site requests: a websocket upgrade or a state-changing HTTP request whose Origin is a different host than the one it targets is refused, so a page on another site can't ride the user's session to drive or watch a proxied service (like a browser). Same-origin use and non-browser callers are unaffected.
