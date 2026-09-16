Phase 9 of the chat-agent split: the plan (`docs/system/blueprint/chat-agent-split/plan-chat-agent-split.md`) now covers a rebind that takes a model pick.

- Principle 24: after a switch the chat runs on the model the user picked, else on what the switch implies.

- 4.2, 4.6, and 5.2: the pick sits on the shared transition record, and the switch route takes it for either kind.

- 5.1: why a rebind stays armed on the press rather than opening the dialog, and what its dialog variant says.

- 5.12 and 6: the rebind's apply step, its retry while the agent comes up, `failed_step: model`, and the resume marker.

- 4.2 and 6: account binding is one class per harness registered on `HarnessSpec`, and the rebind record's sessions field is the harness-neutral `sessions_dir`.
