import json
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

from pydantic import Field

from skills_app.online_search import IndexedSkill
from skills_app.online_search import SkillIndexSearcherInterface


def indexed_skill(source: str, name: str, installs: int = 1000) -> IndexedSkill:
    """A match as skills.sh answers it, its page at owner/repo/name."""
    return IndexedSkill(name=name, source=source, installs=installs, page_id=f"{source}/{name}")


class FakeSkillIndexSearcher(SkillIndexSearcherInterface):
    """Answers every search with the same matches, or as an unreachable index; remembers what it was asked."""

    matches: tuple[IndexedSkill, ...] = Field(default=(), description="What every search answers")
    is_unreachable: bool = Field(default=False, description="Whether every search fails as the network would")
    queries: list[str] = Field(default_factory=list, description="Every query searched, in order")

    def search(self, query: str) -> tuple[IndexedSkill, ...] | None:
        self.queries.append(query)
        return None if self.is_unreachable else self.matches


class LocalSkillIndex:
    """A stand-in for skills.sh on a free local port: answers its search path with ``answer`` and ``status``."""

    def __init__(self) -> None:
        self.answer: object = {"skills": []}
        self.status = 200
        self.requested_paths: list[str] = []
        index = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                index.requested_paths.append(self.path)
                body = json.dumps(index.answer).encode()
                self.send_response(index.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
