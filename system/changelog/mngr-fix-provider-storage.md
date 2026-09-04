Modal is now a minds *connection* (a `modal_<hex>` provider instance whose block minds writes into its mngr profile, carrying the user's Modal token) rather than the ambient `modal` provider using the machine's own token. Accordingly:

- `[create_templates.modal]` no longer declares `provider = "modal"` and no longer extends `providers.modal.*` settings: the create address selects the connection instance (as the aws/gcp/azure templates already do), and the sizing (2 CPU / 4 GB) plus the ~24h sandbox timeout that lived here now ride the connection block minds writes.

- `[create_templates.modal_eval]` is gone. The eval harness shortens the sandbox lifetime through minds' `MINDS_MODAL_SANDBOX_TIMEOUT_SECONDS` override instead of a template overlay, since a template cannot name a connection instance.

- The `[providers.docker]`, `[create_templates.aws]`, `[create_templates.gcp]`, `[create_templates.azure]`, and `[create_templates.imbue_cloud]` comments now describe the provider instances minds actually uses (`aws_<hex>`, `gcp_<hex>`, `azure_<hex>` connection instances, and `imbue_cloud_<user id>` for imbue_cloud) instead of the retired `aws-<region>` / `byo-*` / `imbue_cloud_<slug>` names.

Ships alongside minds part 3 of `specs/account-owned-providers/spec.md` (mngr PR #836).
