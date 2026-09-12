Gen-2 restores no longer replay cloud-init (imbue-ai/mngr-internal#849).

- The gen-2 restore-reserve copies the artifact's `user-data`, `meta-data` and `network-config` from the meta tar verbatim and re-renders only the env file for the new ordinal and ports. The placement-keyed instance-id and the regenerated static network-config are gone: the guest keeps its stable instance-id and gets its new /30 address from the box's DHCP server, so a cross-placement restore no longer reruns the `ssh_keys` module (which rewrote an adopted host key back to the bake-time key) or the `users` module (which re-appended every bake-time root key).

- The meta tar contents are unchanged (`user-data`, `meta-data`, `network-config`, `env`); only what the restore does with them changed. A gen-2 artifact whose meta tar lacks any of the three cidata files fails the reserve with a clear error instead of booting a VM with a fabricated cidata.
