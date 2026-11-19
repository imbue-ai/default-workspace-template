`models/request-error.ts` exports `sendFailureKindOf`, which reads a kind the chat app reported as a `SendFailureKind` (anything unrecognised is `"unknown"`). `describeRequestErrorKind` now uses it.
