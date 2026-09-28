Update the chat service's autocompact scheduler to pass all eligible chat agents to a single `mngr autocompact run` invocation, replacing the per-agent loop and concurrency workers.
