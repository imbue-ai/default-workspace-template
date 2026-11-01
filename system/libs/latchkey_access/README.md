# latchkey-access

The latchkey gateway as a workspace program reaches it. Every supervised program (the shell,
the chat app, the file browser, the routines runner) runs under the environment mngr gives the
workspace's agents, so each holds the same gateway address and credentials an agent does:

- `LATCHKEY_GATEWAY` -- the gateway's address;
- `LATCHKEY_GATEWAY_PASSWORD` -- its listen password, sent as `X-Latchkey-Gateway-Password`;
- `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE` -- the permissions-override JWT a desktop-hosted
  gateway needs (a request without it is evaluated against a deny-all default), sent as
  `X-Latchkey-Gateway-Permissions-Override`; a VPS gateway omits it.

`gateway.py` is the one place that contract is spelled: the environment and header names, and
`GatewayAccess`, which reads the environment (`from_environ`, None when the address or the
password is missing), builds a gateway URL from a rooted path (`url`), and answers the headers
every request carries (`headers`). The secrets are held as pydantic `SecretStr`, so a logged
model never prints them.

```python
import os

import httpx
from latchkey_access.gateway import GatewayAccess

gateway = GatewayAccess.from_environ(os.environ)
if gateway is not None:
    httpx.get(gateway.url("/permissions/self"), headers=gateway.headers(), timeout=5.0)
```

Programs with a richer client (the file browser's streaming relay) keep their own client and
take the names from here; the stdlib-only skill scripts, which cannot import a workspace
package, spell the same names themselves.
