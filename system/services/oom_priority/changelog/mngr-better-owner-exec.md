- Gave the new one-shot `vm-exec-register` supervisord program an explicit OOM
  band (PROTECTED, like the other one-shots), so it is not left to the
  expendable user-service fallback.
