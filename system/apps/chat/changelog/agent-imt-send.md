A message sent to a chat whose agent is busy now gets its own step on the chat's progress timeline, shown under the message once a small model has titled it (about two seconds after sending), so the user sees the request acknowledged without waiting for the agent.

- At send, the chat reserves a tk step id for the message before delivering it, and the agent's hooks name that id beside the message. The step is written under that id only when the harness's one-shot model (Haiku) has written its final title, so the step appears once and never changes its words.

- A message the model judges not a task (thanks, a question about progress) gets no step, and a notice a program posts to the chat (a background command's report) is not sent to the model at all. A failed title call is tried again, up to three attempts with a 10 s limit each; if all fail, the chat logs an error and makes no step.

- A message sent while the agent is idle gets no step from the chat; the agent declares its own steps as before.
