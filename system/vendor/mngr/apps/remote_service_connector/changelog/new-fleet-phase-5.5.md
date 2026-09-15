`POST /hosts/lease` gains an optional `max_box_generation` capability field (absent defaults to 1), so clients that predate it can never receive a gen-2 pool row -- their slow-path rebuild would produce an unsandboxed runc container.

A generation-capped lease that exhausts because the tier's gen-1 stock is fully retired (while gen-2 rows exist) answers a clear update-the-app 503 instead of the generic no-capacity error, with a new `update_required` outcome on the `host_lease_request` metric.

`POST /hosts/claim` derives its generation cap from the tier's pinned template ref (pre-minds-v0.6 tags pin to gen-1).
