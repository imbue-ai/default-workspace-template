---
finish_report_path: reports/report.md
---

# Get ready to build nodes of an app

You are one of three agents an orchestrating agent keeps for one build, one per
difficulty. Nodes of your difficulty will be sent to you as messages, one at a time,
for as long as the build runs. **This message is not one of them** -- it arrives before
the plan exists, so that the reading every node needs is done once, now, instead of
again at the start of each node.

Read these, in this order:

1. `.agents/skills/build-app/references/worker-node.md` -- how a node's worker works
   here and what a node's task file will ask of you.
2. `.agents/skills/build-app/references/app-building-guidance.md` -- how apps are built
   in this workspace: the package layout, the manifest, the supervisord program, the
   frontend conventions.
3. `.agents/shared/references/worker-reporting.md` -- the shape every report you write
   has to take.
4. `.agents/skills/build-app/scripts/scaffold_flask_lib.py` -- the scaffolder, which is
   what a node that creates the app package will run.

Then write `reports/report.md` with one line saying you are ready, and **stop**. Do not
look for work, do not touch the app, do not read the request: there is no plan yet and
nothing is yours until a node arrives.

Everything in the folder you are in is shared with the other two agents. Nothing is
merged here, so a file you write that is not yours overwrites their work rather than
raising a conflict -- which is why a node's task file names the files it owns, and why
you touch nothing else.
