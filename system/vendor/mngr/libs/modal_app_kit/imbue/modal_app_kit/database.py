"""Database-URL helpers shared by Modal apps that talk to Neon Postgres."""

import urllib.parse
from typing import Final

# Neon serves PgBouncer (transaction pooling) on hostnames whose first label
# ends with this suffix; the same hostname without it is the direct compute.
_POOLER_LABEL_SUFFIX: Final[str] = "-pooler"


def direct_database_url(database_url: str) -> str:
    """Return ``database_url`` with Neon's ``-pooler`` suffix stripped from the hostname.

    Schema migrations must run over a direct connection: Prisma's schema engine
    takes session-scoped advisory locks, which are unsafe through PgBouncer's
    transaction-mode pooling (Neon's ``-pooler`` endpoints). Non-Neon and
    already-direct URLs are returned unchanged.
    """
    parsed = urllib.parse.urlsplit(database_url)
    userinfo, at_sign, host_and_port = parsed.netloc.rpartition("@")
    host, colon, port = host_and_port.partition(":")
    first_label, dot, remaining_labels = host.partition(".")
    if not dot or not first_label.endswith(_POOLER_LABEL_SUFFIX):
        return database_url
    direct_host = first_label[: -len(_POOLER_LABEL_SUFFIX)] + dot + remaining_labels
    direct_netloc = f"{userinfo}{at_sign}{direct_host}{colon}{port}"
    return urllib.parse.urlunsplit(parsed._replace(netloc=direct_netloc))
