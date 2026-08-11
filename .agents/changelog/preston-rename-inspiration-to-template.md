Inspirations are now called templates.

The four skills are `publish-template`, `use-template`,
`update-published-template`, and `update-installed-template`, and a published
repo carries `template.md`, `template.toml`, and `template.svg`. The GitHub
topic is `minds-template` and the README's copyable line is `/use-template`.

There is no `use-inspiration` alias: the old name is gone. Anything still
pasting `/use-inspiration` -- a machine on an older workspace template, or a
README published before the rename -- needs the machine updated first.

The copyable command leads with a space -- `` /use-template <url>`` -- in both
the generated README and the minds app. A pasted string starting with `/` can
be taken as a slash command by a chat input rather than as message text.

Repos published in the older v1 format keep the filenames they already have on
disk: `inspiration-<slug>.md` and `inspiration-<slug>.svg`. The rename applies
to what gets written from now on, not to files sitting in other people's
repos, so the v1 adopt path still names them the way it will find them.

The default workspace template is unchanged and still called that -- it is
simply the default template, where a published one is not.
