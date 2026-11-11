---
name: run-on-users-computer
description: Use only when the user explicitly asks for something to run on their own physical computer, outside this workspace -- "on my laptop", "on my Mac", "on my local machine", "on my desktop computer", "not in the workspace" -- or when a plan would have them run a command there. Everything else runs in this workspace, so a bare "my computer", "my machine", or "here" means the workspace, as do "the desktop" (the workspace's windowed desktop) and "the screen" (the workspace's Linux desktop). Covers the alternatives to try first and the one sanctioned way to run a command on their computer.
compatibility: Requires the latchkey gateway env mngr injects into every agent (LATCHKEY_GATEWAY, LATCHKEY_GATEWAY_PASSWORD) and the devices skill.
metadata:
  author: imbue
---

# Running something on the user's computer

Imbue Studio can let you run a command on one of the user's computers, as the
user. It is the most dangerous thing you can be allowed to do: the command runs
with everything the user can reach, and the user cannot see it before it runs.
So it is a last resort, it is off until the user turns it on and approves this
workspace, and this skill is as much about not using it as about using it.

## 0. Is this even about their computer?

Everything runs in this workspace unless the user explicitly says otherwise.
The workspace is a computer too, and it is the one the user is looking at: it
has a terminal, a Linux desktop (the Screen, with a browser), and a windowed
desktop of its own, so "run it on my computer", "install it on my machine",
"open it on my desktop", or "do it here" all mean the workspace. Only an
explicit pointer at their own hardware ("on my laptop", "on my Mac", "on my
local machine", "on the computer I'm typing on", "not in the workspace") sends
you to the rest of this skill. When it is genuinely unclear, do it in the
workspace and say that is where you did it; the user will correct you if they
meant their laptop, and nothing dangerous has happened.

## 1. Prefer something else

Most "run it on my computer" requests are really "I need the result", and the
result can usually be produced without running anything there:

- **Their files.** Read and write them through the `file-sharing` skill, and
  ask for a synchronized copy when you need a folder while their computer is
  asleep.
- **Their accounts and services.** Go through the `connect-external-service`
  skill: it reaches what the user is signed in to without touching their
  computer at all.
- **A result you can compute here.** Produce it in the workspace and hand it
  over as a file or a page.

Say which of these you are taking, in the user's terms, and only move on when
none of them can do the job.

## 2. A one-off script the user runs is fine

When something genuinely has to run on their computer and the user is willing
to run it, write one self-contained script that does one thing, put it where
they can see it (a shared folder, or a file you link in chat), and tell them in
plain words what it does before they run it. That is theirs to run, once, with
their eyes on it.

## 3. This route is the only way for you to run something there yourself

Never build anything that gives you, or any agent, a standing way to run
commands on the user's computer: no daemon, no cron or launchd entry, no
listener, no reverse tunnel, no script that polls for instructions, nothing
that stays behind after the one thing it was for. The user's own switch in
Imbue Studio is the only thing allowed to stand that capability up, and the
route below is the only thing allowed to use it. A script that would leave
such a thing behind is not a one-off script.

## 4. How to ask

Pick the computer with the `devices` skill; every call below names it in both
the path and the `X-Latchkey-Device` header. Then try the command (section 5).
Two refusals mean two different things:

- A `403` whose JSON body carries `"code": "remote_execution_disabled"` means
  the user has not turned the switch on for that computer. Tell them once where
  it is (Settings > This computer in Imbue Studio, "Allow agents to run
  commands on this computer") and why you need it, then do something else or
  wait. Do not ask again in the same turn.
- Latchkey's `Request not permitted by the user.` means the switch may be on
  but this workspace has not been approved for that computer, or not for that
  program. File the request below, in a tool call of its own with nothing else
  in it and its output untouched, then end your turn and wait for the user's
  answer. Do not route around a refusal by running the same thing through a
  program you are allowed.

**Ask for the narrowest program that does the job.** A request names either
any command (`"payload": {}`) or one program (`"payload": {"program": "..."}`),
and the user sees the difference: one program is a far easier thing to say yes
to. The program is what you will put in the `program` query parameter when you
run it (section 5), exactly as you will send it:

- a bare name the user's shell would find (`rsync`, `gh`, `ffmpeg`): Imbue
  Studio resolves it on their PATH and shows the user the file it found, or
  that it is not installed;
- a `~`-rooted or absolute path you already know, such as a script you wrote
  through the `file-sharing` skill. By convention put such scripts under
  `~/.studio/custom-scripts/`. You may ask before the file exists; the user
  can approve it anyway and it runs once you have written it.

You cannot list what is installed on their computer; ask the user, or name
the common tool and let the request show whether it exists. A shell or a
language runtime (`bash`, `python3`, `node`, `uv`, `npx`, ...) is shown to
the user with the same warning as asking for any command, because it is one;
ask for it only when you genuinely need everything.

Writing a short script and asking to run just that script is a good way to
get a narrow grant: the user can read it, and once it works they can take
your write access to it away and keep the run grant. The script is still
bound by section 3.

```bash
latchkey curl -H "X-Latchkey-Device: <device id>" -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "remote-execution", "payload": {"program": "~/.studio/custom-scripts/export-photos.sh"}, "rationale": "<one paragraph: what the program does on the user'"'"'s computer and why nothing else will do>"}'
```

For any command, send `"payload": {}` instead and say in the rationale why
one program is not enough. A grant is per computer and per program, so ask
again for each new program; the user sees each as its own row they can turn
off. The header names one computer, never `*`: a request sent to every one
would have to be approved on each. A `400` from the gateway saying the type
is unknown means the user's Imbue Studio predates this; tell them it needs
updating.

## 5. How to run

```bash
latchkey curl -N -H "X-Latchkey-Device: <device id>" -H "X-Imbue-Studio-Agent: ${MINDS_CHAT_ID:-$MNGR_AGENT_ID}" -XPOST \
  "http://latchkey-self.invalid/minds-api-proxy/api/v1/remote-execution/<device id>?program=bash" \
  -H 'Content-Type: application/json' \
  -d '{"args": ["-lc", "uname -a && df -h ~"], "timeout_seconds": 60}'
```

- The program goes in the URL's `program` query parameter, percent-encoded
  (`python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' '~/.studio/custom-scripts/x.sh'`
  prints the encoded form; a bare name such as `rsync` needs no encoding),
  given exactly once. It is resolved on the user's login-shell PATH when it is
  a bare name, taken as a file when it is a `~`-rooted or absolute path, and
  must be exactly the program a program grant names: the gateway matches the
  URL, so a different program is refused before it reaches the user's
  computer. Under an any-command grant you may name `bash` and pass a shell
  command in `args` as `["-lc", "..."]`.
- `args` is the list of arguments after the program, passed directly, not a
  shell line; it may be left out for a program that takes none.
- `cwd` is optional, absolute or `~`-rooted, and defaults to their home
  directory. `timeout_seconds` defaults to 600.
- Send `X-Imbue-Studio-Agent` on every call: it is how the user's record of
  commands says which workspace ran what.
- The answer streams as NDJSON, one JSON object per line: `{"type":
  "stdout", "data": "..."}` and `{"type": "stderr", "data": "..."}` lines as
  the command prints them, `{"type": "keepalive"}` while the command is silent (ignore it,
  and any other type you do not know), and one final `{"type": "exit",
  "code": <int or null>, "timed_out": <bool>}`. Keep `-N` so you see output
  as it happens.
- There is no stdin. A command runs to completion or to its timeout, and a
  connection that drops before `exit` means the command was killed, not left
  running.
- A command that cannot be started is reported on the stream (its error on
  `stderr`, exit code 127 or 126), never as an HTTP error.
- Redirect binary output to a file and fetch it through file sharing rather
  than streaming it.

## 6. Conduct

- Tell the user what you ran and why, in plain language, every time.
- Never run something destructive without saying so first, and prefer commands
  that read over commands that change.
- Remember that the user can revoke this at any time, from the workspace's
  Permissions tab or by turning the switch off, and that every command is
  recorded on their computer.
- Never say "remote execution", "route", "gateway", or "endpoint" to the user.
  Say "run a command on your computer".
