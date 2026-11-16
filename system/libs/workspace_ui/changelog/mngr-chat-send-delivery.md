`isUnansweredRequest` tells a request that got no answer from the app it was sent to (no HTTP response, or a gateway's 502 or 504 with no detail of the app's own) from one the app answered, so a caller can repeat a request that is safe to repeat.

`describeRequestErrorKind` now returns mngr's `state_unknown`, `agent_not_found` and `not_submitted` kinds rather than reading them as `unknown`.
