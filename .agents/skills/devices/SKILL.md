---
name: devices
description: List the user's devices (desktops) and route Studio requests to specific ones.
metadata:
  author: imbue
---

# Devices

## Instructions

Use this skill when you need to know which devices (desktops) the user has,
or when you need to control which device a request to the Studio goes to.


## Listing devices

Call `latchkey curl http://latchkey-self.invalid/devices`. The response looks like this:

    {"devices": [{"device_id": "...", "hostname": "...", "last_seen_at": "..."}, ...],
     "announcement_interval_seconds": 30}

Devices are listed most recently seen first, including ones that are currently
offline or asleep. A connected device checks in every
`announcement_interval_seconds`, so a device whose `last_seen_at` is much older
than that is probably not reachable.


## Routing requests with X-Latchkey-Device

Requests to the Studio (sending notifications, accessing files, requesting
permissions, and other calls under `/minds-api-proxy`, `/permissions` and
`/permission-requests`) accept an optional `X-Latchkey-Device` header. Its value
can be:

- `*`, to send the request to every known device, including offline ones;
- a single device ID, to send the request only to that device;
- a comma-separated list of device IDs, to send it to each of them (unknown IDs
  are ignored).

If the header is omitted, the request goes to the most recently seen device.
Unknown device IDs, or no known device at all, result in a 503 response.
Requests to third-party services ignore the header. For a local workspace,
there is only one device and the header has no effect.

If the request is routed to more than one device, the response has the
`X-Latchkey-Multiple-Desktops-Matched: true` header and its body has this form:

    {"responses": [{"device_id", "hostname", "status", "content_type", "body"}, ...]}

`body` holds each device's raw response body as a string. A device that could
not be reached has an `error` field instead of `content_type` and `body`.
If the request ends up going to a single device, that device's response is
returned unchanged, without the header.
