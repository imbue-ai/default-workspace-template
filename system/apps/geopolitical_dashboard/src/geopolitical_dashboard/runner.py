"""Five-topic geopolitical news dashboard.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/geopolitical-dashboard/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/geopolitical-dashboard/`` but
  honors the ``GEOPOLITICAL_DASHBOARD_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``GEOPOLITICAL_DASHBOARD_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://geopolitical-dashboard.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import os
from pathlib import Path
from typing import TypedDict

from flask import Flask, Response
from werkzeug.serving import run_simple

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/geopolitical-dashboard/`` but is overridable via the ``GEOPOLITICAL_DASHBOARD_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/geopolitical-dashboard/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("GEOPOLITICAL_DASHBOARD_DATA_DIR", "data/.apps/geopolitical-dashboard"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``GEOPOLITICAL_DASHBOARD_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("GEOPOLITICAL_DASHBOARD_PORT", "8080"))

app = Flask("geopolitical_dashboard", static_folder=None)


class Topic(TypedDict):
    region: str
    title: str
    status: str
    tone: str
    summary: str
    signals: tuple[str, ...]
    sources: tuple[tuple[str, str], ...]


TOPICS: tuple[Topic, ...] = (
    {
        "region": "Europe",
        "title": "Ukraine war",
        "status": "HIGH RISK",
        "tone": "red",
        "summary": "Military pressure, long-range strikes, and support commitments remain the main variables to watch.",
        "signals": ("Front-line movement", "Air defence", "Western support"),
        "sources": (
            ("Reuters", "https://www.reuters.com/world/europe/"),
            ("BBC News", "https://www.bbc.com/news/world/europe"),
            ("Institute for the Study of War", "https://www.understandingwar.org/"),
        ),
    },
    {
        "region": "Middle East",
        "title": "Gaza & regional escalation",
        "status": "CRITICAL",
        "tone": "amber",
        "summary": "Ceasefire diplomacy, humanitarian access, and spillover risk across the region are tightly linked.",
        "signals": ("Ceasefire talks", "Aid access", "Regional actors"),
        "sources": (
            ("Reuters", "https://www.reuters.com/world/middle-east/"),
            ("UN News", "https://news.un.org/en/"),
            ("International Crisis Group", "https://www.crisisgroup.org/middle-east-north-africa"),
        ),
    },
    {
        "region": "Indo-Pacific",
        "title": "China, Taiwan & the South China Sea",
        "status": "ELEVATED",
        "tone": "blue",
        "summary": "Military signalling, export controls, and maritime encounters keep this a structural global flashpoint.",
        "signals": ("Military activity", "Chip controls", "Maritime incidents"),
        "sources": (
            ("Reuters", "https://www.reuters.com/world/china/"),
            ("CSIS ChinaPower", "https://chinapower.csis.org/"),
            ("Taiwan Ministry of National Defense", "https://www.mnd.gov.tw/"),
        ),
    },
    {
        "region": "Middle East",
        "title": "Iran nuclear file",
        "status": "HIGH RISK",
        "tone": "red",
        "summary": "Nuclear verification, sanctions enforcement, and the chance of direct confrontation shape the outlook.",
        "signals": ("IAEA reporting", "Sanctions", "Diplomatic channels"),
        "sources": (
            ("IAEA", "https://www.iaea.org/newscenter"),
            ("Reuters", "https://www.reuters.com/world/middle-east/"),
            ("Arms Control Association", "https://www.armscontrol.org/"),
        ),
    },
    {
        "region": "Global economy",
        "title": "Trade routes & supply shocks",
        "status": "WATCH",
        "tone": "green",
        "summary": "Shipping security and protectionist measures can rapidly affect energy, food, and industrial supply chains.",
        "signals": ("Red Sea transit", "Tariffs", "Energy prices"),
        "sources": (
            ("Reuters Markets", "https://www.reuters.com/markets/"),
            ("UNCTAD", "https://unctad.org/news"),
            ("International Energy Agency", "https://www.iea.org/news"),
        ),
    },
)


def topic_card(topic: Topic) -> str:
    signals = "".join(f"<span>{signal}</span>" for signal in topic["signals"])
    sources = "".join(
        f'<a href="{url}" target="_blank" rel="noreferrer">{name}<b>Open</b></a>'
        for name, url in topic["sources"]
    )
    return f"""
        <article class="topic-card {topic['tone']}">
          <div class="card-topline"><span>{topic['region']}</span><strong>{topic['status']}</strong></div>
          <h2>{topic['title']}</h2>
          <p>{topic['summary']}</p>
          <div class="signals">{signals}</div>
          <div class="sources"><small>Three sources</small>{sources}</div>
        </article>"""


@app.route("/")
def index() -> Response:
    cards = "".join(topic_card(topic) for topic in TOPICS)
    return Response(
        f"""<!doctype html>
        <html lang="en">
        <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
        <title>World Briefing</title>
        <style>
          :root {{ color-scheme: dark; --ink:#f4f1e9; --muted:#a8a79f; --line:#2b302f; --panel:#171b1b; --bg:#0d1010; --lime:#c3dd61; }}
          * {{ box-sizing:border-box }} body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 Arial, Helvetica, sans-serif; }}
          .shell {{ width:min(1180px,calc(100% - 36px)); margin:auto; }}
          header {{ min-height:274px; padding:32px 0; border-bottom:1px solid var(--line); background:radial-gradient(ellipse 78% 112% at 94% 6%,#2e423d 0%,transparent 54%), radial-gradient(ellipse 60% 100% at 15% 85%,#2d3121 0%,transparent 55%); }}
          .top {{ display:flex; justify-content:space-between; align-items:center; color:var(--muted); font-size:12px; letter-spacing:.11em; text-transform:uppercase; }}
          .live {{ color:var(--lime); display:flex; align-items:center; gap:7px; }} .dot {{ width:7px; height:7px; background:var(--lime); border-radius:50%; box-shadow:0 0 13px var(--lime) }}
          h1 {{ max-width:740px; margin:47px 0 13px; font:500 clamp(40px,7vw,79px)/.94 Georgia, serif; letter-spacing:-.055em; }}
          .intro {{ color:#d0d0c8; max-width:550px; margin:0; font-size:17px; }}
          main {{ padding:28px 0 50px; }} .section-head {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:17px; }}
          .section-head h2 {{ font-size:13px; letter-spacing:.12em; text-transform:uppercase; margin:0; }} .section-head p {{ color:var(--muted); margin:0; font-size:13px; }}
          .grid {{ display:grid; grid-template-columns:repeat(6,1fr); gap:12px; }}
          .topic-card {{ grid-column:span 2; min-height:292px; padding:19px; border:1px solid var(--line); border-top:3px solid var(--accent); background:linear-gradient(145deg,#1b2020,#141818); display:flex; flex-direction:column; }}
          .topic-card:nth-child(4),.topic-card:nth-child(5) {{ grid-column:span 3; min-height:265px; }}
          .red {{ --accent:#eb705f }} .amber {{ --accent:#e6b65b }} .blue {{ --accent:#7ba7d9 }} .green {{ --accent:#85b876 }}
          .card-topline {{ display:flex; justify-content:space-between; color:var(--muted); font-size:11px; letter-spacing:.1em; text-transform:uppercase; }} .card-topline strong {{ color:var(--accent); font-size:10px; }}
          .topic-card h2 {{ margin:29px 0 10px; font:500 28px/1.04 Georgia,serif; letter-spacing:-.025em; }} .topic-card p {{ color:#c8c8c1; max-width:440px; margin:0; }}
          .signals {{ display:flex; flex-wrap:wrap; gap:6px; margin:20px 0; }} .signals span {{ color:#bfc0b9; padding:4px 7px; border:1px solid #3c4240; font-size:11px; }}
          .sources {{ display:grid; grid-template-columns:repeat(3,1fr); gap:6px; margin-top:auto; }} .sources small {{ grid-column:1/-1; color:var(--muted); text-transform:uppercase; letter-spacing:.1em; font-size:10px; }}
          .sources a {{ color:var(--ink); background:#202524; padding:8px; text-decoration:none; font-size:12px; display:flex; justify-content:space-between; }} .sources a:hover {{ background:#303736; color:var(--lime); }} .sources b {{ font-size:13px; font-weight:400; }}
          footer {{ color:var(--muted); border-top:1px solid var(--line); padding-top:15px; margin-top:25px; font-size:12px; }}
          @media(max-width:720px) {{ .shell {{ width:min(100% - 24px,600px) }} header {{ min-height:230px }} h1 {{ margin-top:34px }} .grid {{ grid-template-columns:1fr }} .topic-card,.topic-card:nth-child(4),.topic-card:nth-child(5) {{ grid-column:1; min-height:0 }} .sources {{ grid-template-columns:1fr }} .section-head p {{ display:none }} }}
        </style></head>
        <body><header><div class="shell"><div class="top"><span>World Briefing / Prototype</span><span class="live"><i class="dot"></i> Monitoring five theatres</span></div><h1>What can change the world this week?</h1><p class="intro">A decision-oriented briefing on the geopolitical pressures that deserve attention now.</p></div></header>
        <main class="shell"><div class="section-head"><h2>Five hot topics</h2><p>Curated sources open in a new tab</p></div><section class="grid">{cards}</section><footer>Prototype view. Topic notes are illustrative; the next version will use current reporting and dated updates.</footer></main></body></html>""",
        mimetype="text/html",
    )


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
