The latchkey skill now tells agents how to ask for a connection to a domain latchkey has no service for, and -- more importantly -- when not to.

Which request to send is decided by the error latchkey returned. `No service matches URL` means there is no service for that domain, so the agent asks for a new connection (`type: "custom-service"`). An error naming an existing service (`No credentials found for <service>`, `Request not permitted by the user`) means the service already exists, so the agent asks for permissions on it as before. The skill spells out that the first two of those share HTTP 400, so the message text is what distinguishes them, and that `latchkey curl` exits 0 even when the request failed.

The latchkey skill now documents the `scheme` a `custom-service` request must name (`https`, or `http` for a service with no certificate) and that a sign-in URL may use either scheme so long as it belongs to the domain.

The latchkey skill now says a custom-service domain may be a single label, a private suffix or an IPv4 address, as private networks use; the old public-DNS-shaped restrictions are gone.

A second workspace wanting an origin an earlier one connected sends the same `custom-service` request; the skill no longer tells it to send a `predefined` request naming the scope instead.
