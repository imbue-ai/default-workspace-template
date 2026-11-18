The root project overrides paramiko to 5.0.1, the version mngr pins for CVE-2026-44405. pyinfra, which mngr also depends on, caps paramiko below 5 until its 3.11.0 release clears the two-week cooldown, so without the override no mngr commit from 2026-11-12 on could be locked and the twice-daily pin bump failed.

The mngr pin moves to the public mirror of mngr `main` at a0b3ec6c6e, the first pin that needs the override. paramiko goes from 3.5.1 to 5.0.1 in the lock.
