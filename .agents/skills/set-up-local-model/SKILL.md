---
name: set-up-local-model
description: Set up, or bring back, an AI model on the user's own computer so an app, a routine or a skill in this workspace can run AI work there instead of on a paid provider. Use when the user asks for AI work to "run locally", to "use a local model", to run "on my own computer" or "on my machine", to stay private or go "without sending data to a provider", or names Ollama; before writing any code that calls a local model; and whenever a local model something here depends on is missing, not installed or not answering (a refresh that cannot reach it). Never ask the user to install or start Ollama or to check it is running. This skill asks for one approval in Imbue Studio, which installs Ollama if the computer runs no model server and downloads a model that suits its memory, follows the progress, checks the model answers from this workspace, and records where refresh code reaches it. Not for putting a chat on a local model (a chat's provider menu does that); never done by running commands on the user's computer.
compatibility: Requires the latchkey gateway env mngr injects into every agent (LATCHKEY_GATEWAY, LATCHKEY_GATEWAY_PASSWORD) and an Imbue Studio recent enough to set up local models.
metadata:
  author: imbue
---

# Setting up a model on the user's computer

The user asked for AI work to run on their own computer: a refresh that
summarizes on a local model, a digest that never leaves their machines. This
skill gets a model running there and proves this workspace can call it, with one
click from the user and no visit to any settings page.

Imbue Studio, the app on the user's computer, does the installing. You ask for a
permission; the user approves it once; Imbue Studio installs Ollama (the program
that runs the model) if the computer has no model server running, downloads a
model that suits the computer's memory, and tells this chat. You read the
progress, check that the model answers, and build on it.

Three things this is not:

- **Not a cheaper default.** Use it when, and only when, the user asked for the
  work to run on their computer. A local model is smaller and slower than Claude.
- **Not for a chat.** Putting a chat itself on a local model is done in that
  chat's provider menu, by the user. This skill is for code: an app, a routine,
  a skill's scripted model step.
- **Not for everything a model can do.** Only one prompt, one answer runs there:
  no tools, no files, no web search. The `use-ai-integration` skill says what
  fits.

## The commands

```bash
python3 .agents/skills/set-up-local-model/scripts/set_up_local_model.py check
python3 .agents/skills/set-up-local-model/scripts/set_up_local_model.py start [--model <name>]
python3 .agents/skills/set-up-local-model/scripts/set_up_local_model.py wait [--timeout <seconds>]
python3 .agents/skills/set-up-local-model/scripts/set_up_local_model.py verify [--context <tokens>] [--model <name>]
```

Each prints a few plain lines and then one JSON object on its last line. Its
`state` says what to do next:

| `state` | What it means | What you do |
|---|---|---|
| `NOT_GRANTED` | The user has not allowed this workspace yet. | Ask (step 2). |
| `NOT_STARTED` | Allowed, nothing set up, nothing under way. | `start` (step 4). |
| `INSTALLING_OLLAMA`, `DOWNLOADING_MODEL` | Imbue Studio is working on it. | Wait in the background (step 5). |
| `READY` | The model is on the computer and its server answers. | `verify` (step 6). |
| `FAILED` | The last attempt failed; `detail` says why. | See "When it does not work". |
| `UNAVAILABLE` | This computer cannot be set up; `detail` says why. | See "When it does not work". |
| `STUDIO_UNREACHABLE`, `STUDIO_TOO_OLD`, `STUDIO_ERROR` | Imbue Studio gave no status. | See "When it does not work". |

A status from Imbue Studio also carries `computer` (its name and memory),
`model_id` (the model being set up), `model_download` (bytes done and total),
`models` (what the computer already has) and `recommended` (every model Imbue
Studio offers, each with its download size, the memory it needs, whether the
computer already has it, and `is_suggested` on the largest one this computer's
memory suits). The summary lines already turn a download's bytes into megabytes
or gigabytes; quote those to the user.

## The flow

### 1. Check

```bash
python3 .agents/skills/set-up-local-model/scripts/set_up_local_model.py check
```

Do this before building anything that depends on the model: it is the one part
of the task you do not control. `READY` on the first check means the user set
this up before; go to step 6.

The request and every command here go to the computer Imbue Studio was last
seen on. When the user runs it on two computers, that is not one computer: two
calls in a row can land on different ones. So unless you know the user has a
single computer, look first (`latchkey curl http://latchkey-self.invalid/devices`),
and with more than one device listed, pin one as the last section says before
you file the request.

### 2. Ask, on `NOT_GRANTED`

File the request with this command as the whole tool call: nothing before or
after it, in the foreground, its output untouched. The chat builds the user's
approval card from it.

```bash
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "local-model", "payload": {}, "rationale": "<what the user asked for, in their words, such as: Run the daily digest refresh on a model on your own computer.>"}'
```

The body is exactly those four fields, and `payload` stays `{}`: Imbue Studio
works out what to install from what the computer already has, and shows it.
File it once: while a request you filed in this chat has had no answer, it is
still waiting in Imbue Studio, so remind the user of it and do not file another.

Then tell the user, once and in plain words:

- a request, "Local models on this computer", is waiting for them in Imbue
  Studio on their computer;
- approving it lets this workspace use a model on that computer;
- the request lists what Imbue Studio will install and how large each download
  is: Ollama, the program that runs the model, only if the computer has no model
  server running, and one model chosen to suit the computer's memory, which is
  several gigabytes;
- you will carry on as soon as they answer.

Work that does not depend on the model (the app's page, fetching its data) can
go on in the same turn. Then end your turn. Imbue Studio's message about their
answer starts your next one; do not poll for it, and do not ask them to tell you
when they have answered.

### 3. Check again

Your next turn opens with a message ending `(resolution: granted, ...)` or
`(resolution: denied, ...)`. It wakes you; it is not proof. Run `check` and go by
its `state`.

Denied: say plainly that the work cannot run on their computer without it, and
offer the workspace's usual provider instead. Switch only if they say yes, and
do not ask again unless they raise it. A denial that says the request was
approved but could not be applied to the workspace is not the user's no: when
`check` still answers `NOT_GRANTED`, file the request once more (step 2) and
tell them why they are asked again.

### 4. Start, when nothing is under way

Approving starts the setup, so `check` usually answers `INSTALLING_OLLAMA`,
`DOWNLOADING_MODEL` or `READY`. On `NOT_STARTED`:

```bash
python3 .agents/skills/set-up-local-model/scripts/set_up_local_model.py start
```

Imbue Studio picks the model: the largest one that suits the computer's memory
among those already there, else the one it suggests, which it downloads. When
the user wants another (a smaller, faster one; one they named), add
`--model <name>`: a name from `recommended` whose `min_memory_bytes` is no more
than the computer's `memory_bytes`, or any Ollama model name the user gave.
Imbue Studio downloads whatever you name to their disk, without checking that
it suits the computer, so say its size first.

Once you have named a model, name it on every later `start` and `verify`. A
`start` with no name always means the model Imbue Studio picks, also after a
failed download of the one you named.

### 5. Wait in the background while it installs and downloads

```bash
python3 system/scripts/workspace_bare_scripts/run_in_background.py --description "Wait for the model on your computer" --waiter -- \
    python3 .agents/skills/set-up-local-model/scripts/set_up_local_model.py wait
```

Tell the user what is under way and how big it is, from the summary ("Imbue
Studio is downloading qwen3:8b to your Mac, 5.2 GB"). Then carry on with work
that does not need the model, or end your turn; never sit in a loop of checks.

The wait ends when the state changes, and its report starts your next turn:
Ollama is in and the model has begun to download, the model is ready, or
something failed. Each time, tell the user the new state in one sentence with
the numbers the summary gives. Start the wait again only while the state is
`INSTALLING_OLLAMA` or `DOWNLOADING_MODEL`; for any other state do what the
table says. `NOT_STARTED` after a wait means the setup stopped without failing,
as it does when Imbue Studio is restarted in the middle: `start` again. Exit
code 3 means half an hour passed with no change: say how far the download has
got and wait again. Give no estimate of the time left unless you worked it out
from two readings.

### 6. Verify

```bash
python3 system/scripts/workspace_bare_scripts/run_in_background.py --description "Check the model on your computer answers" -- \
    python3 .agents/skills/set-up-local-model/scripts/set_up_local_model.py verify
```

This connects the workspace to the model, prepares the model for a window of
16,384 tokens (`--context <tokens>` for another size; the window holds the
prompt and the answer together), asks it one short question with its reasoning
switched off, and records where refresh code reaches it, in
`data/.state/local_model/endpoint.json`. It runs in the background because the
first answer waits for the model to be read off disk, minutes for a large one.

When it reports, tell the user which model is set up, on which computer, and
how fast it answered: "qwen3:8b is ready on your Mac. It answered a short test
in 6 seconds." When `is_load_included` is true, add that later answers start
sooner. To verify a model other than the one the setup is ready with, name it:
`verify --model <name>`.

### 7. Build on it

Write the model calls with the copyable helper in the `use-ai-integration`
skill (its local-model section). The helper reads the record, so the code names
no address and no model. Measure one real item and tell the user how long a full
run takes.

Schedule the refresh with the `manage-routines` skill, as a routine that runs a
command. A routine that runs a skill runs in a chat on the workspace's usual
provider, not on this model.

## When a model that was set up stops answering

Something here that ran on the local model can no longer reach it: an app's
refresh reports that the model did not answer, a routine was skipped, the
helper raises `LocalModelUnreachableError`, or the user says the local model is
not working. While you are working with the user, this is yours to fix, not
theirs. Do not tell them to open anything, start anything or check anything
first. Run `check` and go by its `state`.

The record, `data/.state/local_model/endpoint.json`, names the model that was
set up as `base_model`. Give that name to every `start` and `verify` here
(`--model <base_model>`), so that the same model comes back. With no name Imbue
Studio picks afresh, and its pick can be another model than the one your code
was set up and measured on, and one it has to download.

- `NOT_STARTED` or `FAILED`: the computer has lost its model server or its
  model (Ollama was removed, quit, or never installed). Run
  `start --model <base_model>`; Imbue Studio installs or starts what is missing,
  with no new approval while the permission stands. Then wait (step 5) and
  `verify --model <base_model>` (step 6).
- `NOT_GRANTED`: the permission was taken away. Ask again (step 2).
- `READY`: a model is there and only the connection was lost.
  `verify --model <base_model>` opens it again.
- `STUDIO_UNREACHABLE`: only now is it the user's to fix. Their computer is
  asleep or offline, or Imbue Studio is closed on it; ask them to open Imbue
  Studio on that computer.

After `verify` passes, run the thing that failed once more and tell the user
what was wrong in one sentence ("Ollama was no longer on your Mac; I had Imbue
Studio install it again").

## A refresh on a night the computer is not there

The model answers only while the user's computer is awake and Imbue Studio is
open on it. A run that cannot reach it **skips that run and says so**: it keeps
the data the app already has, exits 0, and writes one line to
`$ROUTINES_RUN_DIR/result.json` as its `summary` ("Skipped: the model on your
computer did not answer"), in those plain words and not the error's own text,
which is written for you. It never falls back to Claude or to any other paid
provider on its own; that is a change only the user can ask for. Say this to
the user in one sentence when you hand the work over.

## When it does not work

- **`FAILED`.** Tell the user what `detail` says. Run `start` once more (with
  the same `--model <name>` if you named one) only when the reason is one a
  second try can fix, such as a download that dropped. Otherwise, or if it
  fails again, stop and say what they can do about it; do not keep retrying a
  download on their connection.
- **`verify` ends with `"is_verified": false`.** The record is left as it was,
  and `detail` says which part failed: the connection into this workspace,
  preparing the model for the window, or the test answer. Run it once more when
  the reason can pass by itself (the computer had gone to sleep, the model took
  too long to load); otherwise tell the user what it says in plain words and
  stop.
- **`UNAVAILABLE`.** The computer cannot run a model this way (too little
  memory, or a system Imbue Studio has no Ollama for). Tell the user the reason
  in `detail` and offer the workspace's usual provider. Do not try another way
  to install one.
- **`STUDIO_UNREACHABLE`.** Their computer is asleep or offline, or Imbue Studio
  is closed on it. Nothing can be asked for until it answers: ask them to open
  Imbue Studio on that computer, and check again when they have.
- **`STUDIO_TOO_OLD`.** Their Imbue Studio is from before it could do this; ask
  them to update it. A `400` to the request in step 2 saying the type is unknown
  means the same.
- **`STUDIO_ERROR`.** Imbue Studio answered something else, and `detail` says
  what. After a `start` it refused, run `check` to see where things stand. A
  chat that has only just started may not be known to it yet: do other work and
  check again, or run the wait of step 5, which ends once Imbue Studio gives a
  status. If it persists, tell the user in plain words and stop.

## What you never do

- Never install Ollama or download a model yourself: not through the
  `run-on-users-computer` skill, not by handing the user a script, and not
  through the connection `verify` opens. That connection reaches the model
  server whole; use it for completions only.
- Never send the user to Imbue Studio's settings or to a chat's provider menu to
  make this work, and never write anything under `~/.minds/accounts`.
- Never approve the request yourself.

## What the user sees

- **Progress.** The whole setup is one step in your timeline, named for the
  outcome: `tk create --step "Set up a model on your computer"`. It stays open
  across the turns the download takes, and you close it when `verify` passes,
  with what was set up ("qwen3:8b is ready on your Mac").
- **Words.** The user hears "a model on your computer", "Imbue Studio", the
  model's name, sizes in megabytes or gigabytes, and seconds. The request names
  Ollama, so name it too, with what it is: "Ollama, the program that runs the
  model". They do not hear the state names, the commands, or how the workspace
  reaches their computer.
- **Notifications.** At most one, as a turn ends, never to report progress. The
  turn that ends on the request ends with one ("I need your approval in Imbue
  Studio to set up a model on your computer"), and so does the turn in which the
  work they asked for is ready. The phases in between are chat messages only.

## From a background worker, and with several computers

A background worker has no chat to show the request in. Do the setup from the
chat, before or beside handing the build to a worker; the worker's code needs
only the helper.

Imbue Studio on the computer seen most recently answers by default. When the
user has several (`latchkey curl http://latchkey-self.invalid/devices`, the
`devices` skill), ask which one should run the model and name it everywhere:
`--device <device id>` on every command above, and
`-H 'X-Latchkey-Device: <device id>'` on the request. The permission, the model
and the connection all belong to that one computer. The helper cannot name a
computer: a refresh that has to reconnect asks whichever was seen most recently,
and skips the run when that one does not bring the model back.

A workspace has a single connection to the computer's model servers, and
`verify` points it at Ollama. If a chat in this workspace runs on a different
model server on that computer (LM Studio, llama.cpp), tell the user first: that
chat stops answering until its provider is chosen again.
