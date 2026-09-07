This is the merge of `gabriel/tactful-swift` (the inner workspace-updates work) into current `main`; see that branch's `gabriel-tactful-swift.md` entry for what actually changed.

The one conflict resolved here is in the frontend stylesheet, where `main`'s transcript custom-scrollbar rules and the update-staleness banner rules were both appended at the end of the file. Both are kept, so the transcript scrollbar and the update-staleness banner each render as their own branch intended.
