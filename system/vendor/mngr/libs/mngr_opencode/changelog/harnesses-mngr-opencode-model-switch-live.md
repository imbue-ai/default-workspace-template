Fixed the opencode chat model bar not updating when the model changed without sending a
turn. The lifecycle plugin recorded the live model only from assistant `message.updated`
events, so switching the model from the chat UI (POST /api/session/{id}/model) or via
`/model` in the attached TUI -- both of which emit `session.next.model.switched` -- left
`opencode_model_state.json` stale until the next assistant message, so the bar's model chip
never reconciled. The plugin now also records the model on `session.next.model.switched`
(the analog of pi's `model_select`), so the chip updates immediately.
