from pydantic import Field

from screen.cdp_proxy import ProxyServer
from screen.cli_sessions import AttachedCliSession
from screen.cli_sessions import CliSessionsInterface


class RecordingCliSessions(CliSessionsInterface):
    """The daemons a test says are running; ending one records it and takes it out of the list."""

    running: list[AttachedCliSession] = Field(default_factory=list, description="The daemons running")
    ended: list[str] = Field(default_factory=list, description="The session names ended, in order")

    def attached_through(self, proxy_server: ProxyServer) -> list[AttachedCliSession]:
        return list(self.running)

    def end(self, session: AttachedCliSession) -> bool:
        self.ended.append(session.session_name)
        self.running = [running for running in self.running if running.pid != session.pid]
        return True
