- The web chrome's owner-exec client (`frontend_web`) moved from the homegrown
  v1 envelope to the RFC 9421/9530 strict profile, and now verifies every
  owner-exec response and `/run` stream trailer against the workspace's pinned
  SSH host key (failing closed). It addresses the in-container exec service by
  its host-id-scoped `container:<host-id>` audience. The exec-envelope crypto
  vectors are now vendored from the `imbue-ai/owner-exec` repo;
  `generate_crypto_vectors.py` keeps only the secret-wrapping vectors.
