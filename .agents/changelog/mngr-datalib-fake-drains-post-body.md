The datalib skill's `sync_request` tests: the fake Datalib server reads a POST's body before refusing it, so the refused-token test sees the 401 instead of a reset connection.
