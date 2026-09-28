A codex account whose plan does not offer GPT-6 Sol no longer gets a codex agent that fails every turn. `[agent_types.codex]` now sets `fall_back_to_account_default_model = true` alongside its `model = "gpt-6-sol"` pin, so before mngr starts a new codex thread it reads the account's model list, and if GPT-6 Sol is not in it (the catalog leaves it out for `promax`) the thread starts on the account's default model instead. A chat whose thread already exists keeps its model.

mngr is pinned to the public export of mngr's `gabriel/codex-model-fallback` branch, which adds that setting.
