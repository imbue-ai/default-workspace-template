- The hosted web client's page can call a shared workspace's routes (`/api/workspace/...` on an internal app) and the gateway's `/_agent/...` routes from its own origin. For those requests alone, caddy answers the browser's permission check itself and adds the headers that let the page read the answer, and `/_auth/verify` marks the identity it stamps on a workspace route's request with `is_from_web_client`. Nothing changes for a share with no web client, or for any other origin, path or app.


- `/_health` answers the owner the label of the gateway's auth address (`auth_label`) beside the service labels, so the owner's page can reach the `/_agent/...` routes, which answer only there. A visitor is answered neither.

- A test runs caddy over the rendered web client access in front of a workspace route that answers an image: the headers that let the web client's page read an answer are added whatever the answer is (an image, a 304, an answer with no body), and the image and its caching headers arrive as the app sent them.
