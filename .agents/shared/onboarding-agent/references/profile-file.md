# The profile file

A plain file the user can open, edit or delete: a short portrait, not a form. It records what you learned so a later conversation does not start from nothing. It is the only place you record anything about them.

## Where

`data/profile/about-you.md`. Create the folder if needed. Do not write it to the memory folder.

## When you write it

Only after the user has seen a draft and chosen. Never silently. Write it once, after the starter is built.

## What it is

A few short paragraphs of plain prose, in the second person ("You coordinate volunteer shifts for a food bank..."), the way a thoughtful person would describe someone they have just met: warm and personal, keeping every fact. A picture of them, not a form: no tags, no heading for each kind of fact, no bullet per fact.

Cover three things, in your own flowing words, as the sources allow:
- **Who they are.** The person, not only the job: what they do and what they are like at work, their background, what they care about or enjoy, as far as what they said, their apps and the web page they confirmed show.
- **Their daily work.** What a typical day or week involves, the tools they live in and how they use them, where the time goes, and what they are comfortable with in code and tools, described by what they do and never as a label.
- **What they might want to automate.** The repetitive, scattered or tedious parts of that work that a tool could take over: the pain they named, plus the obvious neighbors of it that the sources support. This is where the ideas list below comes from.

Also anything they asked you never to touch. Where the sources say little about one of these, say less; do not invent a routine, a habit or a feeling to fill the space.

- **Use every source you have.** What they told you, what you read from the apps they shared, and what they confirmed from the web. Weave them together instead of listing them. A fact from the web is followed by its link, as a markdown link in the sentence.
- **You may interpret, honestly.** What you inferred rather than were told reads as inference ("seems to", "looks like", "my read is"), because they approve it at the review.
- **Every sentence traces to a source.** Something they said, an app they shared, or a web fact they confirmed. Interpreting means reading what those show, not adding to them: do not give them traits, tastes or habits that none of them supports ("you favor plain, low-ceremony tools" with nothing behind it).
- **Describe, never label, never contrast.** No skill level or type of user. Say what they are and do, not what they are not: write "you work in Excel and Google Sheets", never "you haven't written code" or "rather than building software". Phrase an off-limits wish as a positive request ("please leave my photos untouched").
- **Only about them, always in the second person.** Do not mention the starter you built, this chat, or yourself.
- **Keep it readable:** about 250 words for the portrait and no more than about 450 for the whole file. It is the file that can be this full; the chat shows a condensed version (see The review).

### Ideas to build

After the portrait, an `## Ideas to build` list of five to eight one-line ideas worth building for them, each with a reason in a few words that comes from a source: their pain, their role, a tool they use, their stack. The first is the starter you built in this setup, marked "(built in this setup)". The rest are things a later agent can offer when they ask what to build or give no direction. They come from the same picture as your suggestion: specific to this person, positive in wording, nothing sensitive, nothing about other people. No more than eight.

## The review

Before you show anything, write the full draft and pipe it to the checker on standard input, so nothing is written to disk: `uv run .agents/shared/onboarding-agent/scripts/profile_check.py -` with the draft as input. Fix every problem it reports, then check again.

The file can be as full as the format below allows, but the chat must stay short. Show the condensed version in the chat, in plain prose and the second person, about 80 to 100 words in all:
- three short lines, one each for who they are, what their days involve and what they might want to automate (one sentence each; say less where the sources say little);
- an "Ideas to build" line with the idea names only, separated by semicolons, no reasons and no bullets.

The condensed version makes no claim that the full draft lacks, and the full draft adds no claim beyond the three lines and the idea names: it only fills them out with detail they gave you or confirmed. It adds no trait, habit, motive or guess about what they would like next ("you start new projects often", "the same instinct would extend to..."); those do not become ideas either unless a source backs them. Every idea in the file is one of the names you showed, and no idea appears that you did not show. Say in one sentence that the saved file is a fuller write-up of these same points.

Then say in one sentence that a saved profile is used by their later chats so those chats start knowing them, and that this means it is sent to the AI service each time, like anything else they type. Then ask:

> Here's what I'd keep about you. Save it, tell me what to change, read the full write-up first, keep only what you told me, or don't save?

- **Save it:** write the full draft you checked. Do not paste the file into the chat; tell them where it is.
- **Tell me what to change:** change it, check it again and show the condensed version again.
- **Read the full write-up first:** show the full draft exactly as it would be saved, then ask the same question again.
- **Keep only what you told me:** rewrite it from their own words only, with nothing from their apps or the web (keep the ideas list, drawn from their own words), check it, show its condensed version, and ask in one line whether to save that version. Save only after they say yes.
- **Don't save:** write nothing. Do not mention it again. The starter stands.

## The format

```markdown
# About you
Written by your onboarding agent on <date>. It is a plain file: open, edit or delete it any time. Anyone you give access to your whole workspace could see it.

<the portrait: a few short paragraphs of prose>

## Ideas to build
- <the starter you built> (built in this setup), because <reason from a source>
- <another idea>, because <reason from a source>

Where this came from: <only the kinds of source you actually used: what you told me, the apps you chose to share, public pages you confirmed. Name the kinds, never the apps or the sites>.
```

## Rules

- Write only what they have seen and approved, or the full write-up of exactly what they saw. A guess they did not approve, or something found online they did not confirm, is not written.
- Never write: other people's details, sensitive topics (health, mood, money, family, politics, religion), credentials, account contents, an address, a phone number or an email address. Draw no conclusion from an app about those topics.
- Name the notable apps that carry a point. Never copy out the whole list.
- What they dropped in or linked is for you to use, not to record. Keep only what they say about it and confirm.
- Tell them where the file is, and that anyone given access to the whole workspace could see it.

## More questions

Not before the close. The review is the last step of setup: after it you close, and the chat carries on as an ordinary one. If they want to add something, they can edit the file or say it afterwards.

## Checking the file

Run `uv run .agents/shared/onboarding-agent/scripts/profile_check.py data/profile/about-you.md` after writing. It prints `ok` or the problems; fix any problem and write again before you tell the user the file is saved.
