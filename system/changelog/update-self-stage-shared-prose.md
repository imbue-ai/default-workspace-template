`system/test_skills.py` checks that every reference `update-self`'s prose reaches, through any number of links, lies in a path the flow stages, so a staged file cannot send the flow to a file read from the workspace's own tree.

It also fails when a file under `.agents/shared/` links a file inside a skill other than its `SKILL.md`: the shared layer may name a skill, and what several skills need lives in the shared layer.
