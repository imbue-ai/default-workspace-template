The share gateway now runs caddy, frpc and the frpc reload in their own sessions, so a child that touches the workspace's terminal can no longer stop the gateway and drop every share tunnel with it.
