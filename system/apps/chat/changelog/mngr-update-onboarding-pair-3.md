A new workspace's first conversation can now help its user find something to build.

- The welcome offers a third start suggestion, "Get ideas for what to make", between "Describe what you want to make" and "See what Studio can do". Picking it has the agent ask what the workspace is for and which apps the user has, and then list ideas that fit both answers.

- The two questions come with pickers drawn in the agent's message: three columns of categories (Work, Life, Creative), and a row of fifteen apps with their logos. Clicking a pill adds its name to the message box, and clicking it again takes it out; the user sends the message themselves and can type anything of their own. A picker in a message that has been answered is greyed out. The first category picked in a workspace points a bubble at the send button, "Click this button to send your message".

- The replies offered under these messages are the chat's own, with no model asked, so they appear the moment the reply ends and work with every AI provider: "Show me some ideas" under the question about apps, and one per idea, then "More ideas" and "Something else", under a list of ideas. Typing anything else at the start or under the ideas ends the scripted conversation, after which the chat gets suggested replies like any other.

- An assistant message that carries `<studio_category_picker/>` or `<studio_connector_picker/>` on a line of its own draws that picker. The chat's folder holds the conversation's step in `first_run_tree.json`, and the chat settings gain `is_picker_send_hint_shown`.
