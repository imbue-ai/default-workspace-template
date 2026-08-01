The email-verification endpoints are no longer an unauthenticated oracle, and verification emails are rate-limited.

- `/auth/email/send-verification` and `/auth/email/is-verified` now require the caller's own bearer token (an unverified session is accepted -- that is exactly who needs them) and refuse an email that does not belong to the authenticated user. Previously anyone holding a user_id could trigger verification emails or probe verification status for arbitrary accounts.

- All verification-email sends (the signup send, the automatic resend on an unverified signin, and the explicit resend endpoint) go through a shared per-user cooldown, so no path can be used to spam a mailbox; the resend endpoint reports `sent: false` when suppressed.
