The service websocket proxy no longer negotiates permessage-deflate on /service/ paths (proxied services carry compressed binary streams -- video, terminal -- where deflate is pure re-compression waste) and sets TCP_NODELAY on both sides of every proxied websocket so interactive messages are never held back by Nagle's algorithm.

Browser pane tabs now have a "Destroy browser" button, symmetric to the destroy buttons on agent and terminal tabs: it confirms, retires the browser in the fleet, and closes the tab (a plain "Close tab" still just detaches the pane).
