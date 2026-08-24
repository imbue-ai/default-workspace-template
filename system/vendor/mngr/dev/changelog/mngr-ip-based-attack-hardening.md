# supertokens secret template: IPINFO_TOKEN

- `.minds/template/supertokens.sh` declares the new `IPINFO_TOKEN` key: the IPinfo (Max plan) API token for the connector's signup IP-reputation check. Leave empty to disable provider lookups (the Tor-exit-list check and signup velocity limits still apply). Every tier's Vault `supertokens` entry must gain the key (empty is fine) before its next `minds-admin env deploy`.
