- The hosted web client's page can call a shared workspace's routes (`/api/workspace/...` on an internal app) and the gateway's `/_agent/...` routes from its own origin. For those requests alone, caddy answers the browser's permission check itself and adds the headers that let the page read the answer, and `/_auth/verify` marks the identity it stamps on a workspace route's request with `is_from_web_client`. Nothing changes for a share with no web client, or for any other origin, path or app.


- `/_health` answers the owner the label of the gateway's auth address (`auth_label`) beside the service labels, so the owner's page can reach the `/_agent/...` routes, which answer only there. A visitor is answered neither.
