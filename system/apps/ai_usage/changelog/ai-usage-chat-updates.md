AI Usage Limits can put a one-line note into a chat each time a usage limit goes up, such as "Claude usage 2%". Off until the user ticks "Updates in chat" on the app's page.

- When on, each new reading is compared with the previous one, and every limit whose whole percent went up is named in one line.
- The line goes only to the chats on that account that were working between the two readings (the chats the increase is credited to), through the chat app's loopback-only notes route. If no chat was working, the usage happened elsewhere and no line is sent.
- A note never reaches the AI in the chat, so it cannot itself use more of the limit.
- The choice is kept in `settings.json` in the app's data directory and read and changed through `GET` and `PUT /api/settings`.
