New `chat-early-sign-in` program: the provider chooser and its sign-in routes, served on their own while a new workspace is still being built, so the Imbue Studio app can show this chooser on its creation page and a sign-in made there is ready when the workspace opens.

- It serves the chat app's own chooser page (`sign_in.html`) and account routes, driven by the same sign-in service the workspace's chooser uses, on a loopback port it names in one startup line. It ends when its stdin closes. Studio starts it through `system/scripts/start_early_sign_in.sh`, which runs it the way supervisord runs the chat app.

- A sign-in runs under a staging store of its own, so the chat app's startup sweep, which kills sign-in programs and removes folders with no index row, leaves it alone. A finished sign-in is moved into the workspace's account store and indexed there in one step under the store's lock (`accounts.move_in_account`).

- The account routes now take their sign-in service from `register_routes` rather than the chat app's state, and the sign-in service takes the step that saves a finished account from its caller.
