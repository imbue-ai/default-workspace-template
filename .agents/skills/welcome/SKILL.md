---
name: welcome
description: Send the workspace's opening greeting, and offer the starter suggestions if the user asks what they could do. The minds desktop client invokes this automatically as the very first message of a new workspace, so it is the first thing the user ever sees. The greeting is fixed copy sent verbatim with no tool calls.
metadata:
  author: imbue
---

# Welcome the user

This skill has two parts: the opening greeting you always send first, and a list of suggestions you offer only if the user asks for ideas.

## Opening message

Output the following welcome message verbatim, as your entire response. Do not call any tools, look at the codebase, or add anything around it:

---

### Welcome to Minds

I'm an AI operating system built to extend *you* — so you can do your best work.

I can take on tasks for you, build custom apps and skills you can easily edit, connect to the tools you already use to pull in information, or just brainstorm ways to make your work better.

**Let's get started**

Already have something in mind? Tell me what you'd like to work on below. If not, I'm happy to suggest a few ways to get started.

---

That is the entire opening message. Stop after printing it.

## If the user asks for suggestions

After the opening message the user replies. If their reply asks for suggestions, says they're not sure, or otherwise signals they don't have something specific in mind, output the following message to the user, verbatim, and nothing else. (If instead they describe something they want to do, ignore this section and help them with that directly.)

---

Here are some popular ways people get started with Minds. Pick whichever fits, and we can build on it as a starting point.

1. **Unify your email & messages:** Bring every conversation into one place and respond from there.
2. **Organize your tasks:** Build a system to track what you need to do and get it done.
3. **Track your team's work:** A dashboard for everything across GitHub, Linear, Slack, and email.
4. **Keep up with what you care about:** Stay current on the products, events, or news that matter to you.

---
