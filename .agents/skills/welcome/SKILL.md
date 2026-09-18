---
name: welcome
description: Greet the user when a new chat is opened with nothing to say yet. Invoked automatically as the first message of every chat that starts without one; what it says depends on how many chats have been greeted before.
metadata:
  author: imbue
---

# Welcome the user

Every chat that opens without a first message from the user starts with this skill. The very first conversation in a workspace is the onboarding chat, which arrives with the user's own message and never runs this, so the first time this runs the user is opening their *second* chat. What you say depends on how many times this has run before.

## Step 1: find out which greeting this is

Run exactly this command and read the number it prints (it also records this run):

```
python3 system/scripts/welcome_count.py
```

Do not call any other tools, do not look at the codebase, and do not mention this command or the number to the user.

## Step 2: send the greeting for that number

Output the greeting below that matches the number, verbatim, as your entire response, and stop.

### 0: the second chat ever

---

### A new chat, the same Mind

This is a new chat, but you are talking to the same Mind as in your other chats. Think of chats the way you think of threads with one person: separate conversations for separate things, all with someone who knows what you have been working on. You can refer to something from another chat here and I will know what you mean.

Use as many chats as you like, to keep things organized and to work on several things at once. Nothing is lost between them.

What would you like to work on in this one?

---

### 1: the third chat

---

### Welcome back

A quick hint: I can build you apps you can open as tabs, not just answer questions. Describe something you would like to have, from a small tracker to a dashboard over your accounts, and I will build the first version and show it to you.

What would you like to work on?

---

### 2: the fourth chat

---

### Welcome back

Another hint: you can connect the services you already use, such as email, calendar, Slack or GitHub, one permission at a time, and take any of them back whenever you like. Once connected, I can work from what is in them.

What would you like to work on?

---

### 3: the fifth chat

---

### Welcome back

One more hint: if there is something you want done on a schedule, like a briefing every morning or a weekly summary, ask for it. I can set it up to run on its own and report back here.

What would you like to work on?

---

### 4 or more: every later chat

---

### Welcome back

What would you like to work on?

---

## If the user asks for suggestions

After the greeting the user replies. If their reply asks for suggestions, says they are not sure, or otherwise signals they do not have something specific in mind, output the following message to the user, verbatim, and nothing else. (If instead they describe something they want to do, ignore this section and help them with that directly.)

---

Here are some popular ways people get started with Mind. Pick whichever fits, and we can build on it as a starting point.

1. **Unify your email & messages:** Bring every conversation into one place and respond from there.
2. **Organize your tasks:** Build a system to track what you need to do and get it done.
3. **Track your team's work:** A dashboard for everything across GitHub, Linear, Slack, and email.
4. **Keep up with what you care about:** Stay current on the products, events, or news that matter to you.

---
