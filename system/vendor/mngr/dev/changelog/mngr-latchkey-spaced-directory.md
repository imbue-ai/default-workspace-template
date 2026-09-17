`filelock` is now a dependency of `mngr_latchkey`, which pins the root lockfile.

It provides the read/write file lock that `mngr latchkey forward` uses to own its latchkey directory. The read side is the part that matters: a probe can ask who owns a directory without contending with another probe, which an exclusive-only lock cannot do. The wheel is pure Python (`py3-none-any`), so it adds no per-platform artifact to the built `mngr` wheel.
