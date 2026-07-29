The client-facing WebSocket read buffer is 64 KiB, matching the backend legs.

`simple_websocket` defaults to 4 KiB, so a large inbound message cost dozens of
recv syscalls plus reassembly -- and this server runs under gVisor, where every
syscall is several times more expensive. The backend legs of the service proxy
were already raised; this covers the client-facing side.
