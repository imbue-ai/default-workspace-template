- After a test command is shed, the harden worker judges whether memory is recovering from the free memory the guard acts on (`memory_candidates.py --free-memory`) instead of `/proc/meminfo`, which under a memory cap reads far more free memory than the guard sees.

- The memory-freeing guides describe a candidate's memory as its processes' own memory, roughly what stopping it frees.
