Published templates now carry a real license, and adopters are told what it
means.

Publishing asks you to license the template, proposing MIT and explaining it in
a sentence: anyone who gets the repo may use, change, and ship it, including
commercially, as long as they keep the copyright line. It is a question, not a
notice -- licensing your work is not the agent's call. Say yes and a real
`LICENSE` file is written from canonical MIT text with your name and the
current year, committed with the rest of the snapshot, and the README's new
License section says so. Say no and nothing is invented: the README states that
the work is all rights reserved, which is the honest description of a repo with
no license and worth knowing, since it leaves the template unusable to anyone
else.

This applies to private repos too, not just public ones. Anyone you later share
a private template with is in exactly the same position as a stranger finding a
public one.

Adopting a template now reports its license before you invest in adapting it.
`use-template` reads `LICENSE` from the repo and says what it permits: for MIT,
that the only obligation is keeping the copyright line and license text with
any copy you distribute; for anything else, a summary of the real terms rather
than an assumption that it is permissive. A repo with no `LICENSE` is called
out as granting no reuse rights at all -- being able to clone something is not
permission to build on it -- so you can decide whether to ask the author
instead of finding out later. The agent never infers a license or adds one on
the author's behalf.

The welcome skill a published template ships does the same on first boot, so a
mind created FROM a template opens by saying what it is allowed to do with it.

A publish cannot ship with the question unanswered: the README's License
section is generated with a placeholder, and `validate_template.py` fails while
it is still there.
