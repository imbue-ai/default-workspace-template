import os
import signal
import subprocess
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager

from files_app.serving import PROCESS_EXITED_STATUS
from files_app.serving import SIGNAL_EXIT_CODE_BASE
from files_app.serving import wait_for_shutdown_signal


@contextmanager
def _idle_process() -> Generator[subprocess.Popen[bytes], None, None]:
    process = subprocess.Popen([sys.executable, "-c", "import threading; threading.Event().wait()"])
    try:
        yield process
    finally:
        process.kill()
        process.wait()


@contextmanager
def _restored_stop_handlers() -> Generator[None, None, None]:
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def test_the_shutdown_wait_ends_when_dufs_exits() -> None:
    with _idle_process() as dufs, _restored_stop_handlers():
        killer = threading.Timer(0.3, dufs.kill)
        killer.start()
        try:
            assert wait_for_shutdown_signal(dufs) == PROCESS_EXITED_STATUS
        finally:
            killer.cancel()


def test_the_shutdown_wait_returns_128_plus_the_signal_while_dufs_runs() -> None:
    with _idle_process() as dufs, _restored_stop_handlers():
        previous_handler = signal.getsignal(signal.SIGTERM)
        installed = threading.Event()

        def send_sigterm_once_the_wait_listens() -> None:
            # Only a handler the wait installed may receive the signal: the default disposition would end the test
            # process.
            while signal.getsignal(signal.SIGTERM) is previous_handler:
                if installed.wait(0.02):
                    return
            os.kill(os.getpid(), signal.SIGTERM)

        sender = threading.Thread(target=send_sigterm_once_the_wait_listens)
        sender.start()
        try:
            assert wait_for_shutdown_signal(dufs) == SIGNAL_EXIT_CODE_BASE + signal.SIGTERM
            assert dufs.poll() is None
        finally:
            installed.set()
            sender.join(timeout=5)
