"""Lanes: the (AI provider + harness) pairings a user can sign in to, and how each signs in.

The UI calls these "providers". Internally they are lanes, because "provider" is already
three other things in this tree -- a compute provider in mngr, an AI vendor, and one of pi's
own provider ids -- and because a lane is what a chat picks and then stays in.

Several lanes can share a harness: Opencode Go and bring-your-own-key both run on pi. So a
lane is not a harness with a nicer name; it is a *destination*, and the harness is how you
get there.

Each lane lists its sign-in methods, primary first. That mirrors what the Claude modal
already does -- one recommended path with the alternates behind a disclosure -- generalised
so every lane can have its own alternates.

Every value in the table below was measured against the real CLIs, not read off
documentation, which was wrong about several of them. Notably: codex's `--device-auth` is
absent from `codex login --help` but is what bare `codex login` tells you to use on a
headless box, and it inverts the usual shape -- the code comes OUT and nothing is pasted
back.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.pty_auth import DEFAULT_PTY_COLUMNS


class LaneNotFoundError(LookupError):
    """No such lane, or no such method on it.

    LookupError rather than KeyError: `KeyError.__str__` is `repr(args[0])`, so a message
    written for a person came back through the 404 body wrapped in its own quotes --
    `"'no such lane: bogus'"`. Nothing that formats one of these should have to know that.
    """


class DrainUntil(StrEnum):
    """When to stop reading the PTY while hunting for a scraped value.

    The two real cases differ: a URL is drained until it can be extracted (the CLI keeps
    animating afterwards, so there is no quiet gap to wait for), while a minted token is
    drained to process exit because the CLI prints it and leaves.
    """

    VALUE_EXTRACTABLE = "value_extractable"
    EOF = "eof"


class EofPolicy(StrEnum):
    """What end-of-stream means for a flow.

    Not a detail: for claude, EOF without a value is a failure, while for codex's device
    flow the process exiting IS the success signal. Opposite readings of the same event.
    """

    SUCCESS = "success"
    FAILURE = "failure"


class Submit(StrEnum):
    """What the user sends back after approving in the browser.

    `OPTIONAL` is not hedging: `claude setup-token` completes on its own polling *and*
    accepts a pasted code, both live against the same session.
    """

    CODE = "code"
    NONE = "none"
    OPTIONAL = "optional"


class PasteSink(StrEnum):
    """Where a pasted credential is written. Each is a different mechanism, not a format."""

    # The `env` block of the account's settings.json.
    CLAUDE_ENV = "claude_env"
    # A 0600 JSON map keyed by pi's provider id.
    PI_AUTH_JSON = "pi_auth_json"


class Scrape(FrozenModel):
    """How to recover one value from a rendered PTY screen.

    Three patterns, not one, because they do genuinely different jobs: `trigger` is loose
    and only wakes `pexpect.expect` (it can match mid-render-frame, so what it matched is
    often a fragment); `strict` defines where the real value starts; `continuation` is the
    charset that decides whether the next screen row is more of the same value or something
    else entirely.

    Patterns are strings rather than compiled objects because the model forbids arbitrary
    types; `re` caches compilation, so resolving them at use costs nothing.
    """

    trigger: str
    strict: str
    continuation: str
    # A shorter extraction is a wrapped fragment, not the value -- keep draining. Only the
    # setup-token path needs this (real tokens are ~110 chars).
    min_length: int | None = None
    drain_until: DrainUntil = DrainUntil.VALUE_EXTRACTABLE


class PtyMethod(FrozenModel):
    """A sign-in driven by typing at a CLI on a pseudo-terminal."""

    id: str
    label: str
    description: str

    argv: tuple[str, ...] = ()
    # A pattern the screen MUST match before any key is sent. Without it a keystroke script
    # is blind: a reordered menu row or a first-run consent screen would make the same keys
    # select a different login method, no failure pattern would fire, and the user would be
    # silently signed in the wrong way.
    expect_before_keys: str | None = None
    settle_s: float = 3.0
    # Sent ONE AT A TIME with a gap. A burst is read as a paste by an Ink input, and a later
    # key can land on a screen that has not rendered yet.
    keys: tuple[str, ...] = ()
    key_gap_s: float = 0.6
    pty_columns: int = DEFAULT_PTY_COLUMNS

    # When the sign-in URL is fixed, there is nothing to scrape for it and `scrape` names
    # the one-time CODE instead.
    static_url: str | None = None
    scrape: Scrape
    # Some CLIs print a success line; agy and codex do not, and fall back to the probe.
    success: str | None = None
    # (pattern, user-facing copy). "{1}" interpolates the pattern's first group, so a CLI
    # that explains itself can have its own words shown.
    failures: tuple[tuple[str, str], ...] = ()
    eof_policy: EofPolicy = EofPolicy.FAILURE
    submit: Submit = Submit.CODE

    # For the one method where the PTY output IS the credential rather than a step toward
    # it: `claude setup-token` prints the token it just minted.
    result_scrape: Scrape | None = None
    result_sink: PasteSink | None = None

    # Ink's synchronized-update marker. None means the CLI emits no frame boundaries, so
    # the replay collapses to a single final-screen snapshot -- a real loss of the
    # longest-wins protection, safe only when an OSC 8 hyperlink carries the value anyway.
    frame_marker: str | None = "\x1b[?2026l"
    scrape_timeout_s: float = 30.0
    # A wall clock for the whole flow. Nothing else bounds it: the machinery only advances
    # when a client polls, so a closed tab would otherwise leave a CLI waiting forever.
    flow_deadline_s: float = 900.0


class PasteMethod(FrozenModel):
    """A sign-in that is a file write. No terminal, no scraping, no keystrokes."""

    id: str
    label: str
    description: str
    sink: PasteSink
    # Where to go to get a key in the first place. A lane whose provider you have to subscribe
    # to before a key exists needs this; one you already have an account with does not.
    signup_url: str = ""
    # As on `PtyMethod`, and for the same reason: nothing else bounds a flow, and the service
    # is single-flight, so an abandoned one is in the way of the next. Shorter than a browser
    # method's, because there is no round trip to wait out -- the field is already on screen.
    flow_deadline_s: float = 600.0


class KeyProvider(FrozenModel):
    """One provider a bring-your-own-key sign-in can target.

    `provider_id` is pi's own id and becomes the key in its auth.json; `display` is what the
    account is then called, so two keys read "OpenRouter (Pi)" and "Groq (Pi)" rather than
    two indistinguishable "API key (Pi)" rows.
    """

    provider_id: str
    display: str
    env_var: str
    hint: str


class Lane(FrozenModel):
    id: str
    provider_name: str
    subtitle: str
    harness: HarnessType
    # Primary first; the rest render under "Other ways to sign in".
    methods: tuple[PtyMethod | PasteMethod, ...]
    # Only the bring-your-own-key lane populates this.
    key_providers: tuple[KeyProvider, ...] = ()


# The word a LANE's chat tabs count under, where the harness's own word would be ambiguous.
#
# Tab names come from the harness (`AUTO_NAME_WORD_BY_HARNESS`), which is right until two lanes
# share one: Opencode Go and OpenRouter both run on pi, so both minted "Pi 1", "Pi 2", and a
# glance at the tab strip could not tell you which provider a chat was spending. Only the lanes
# that collide are listed; everything else keeps the harness's word.
AUTO_NAME_WORD_BY_LANE: Final[dict[str, str]] = {
    "opencode-go": "Opencode",
    "openrouter": "OpenRouter",
}


# The harness name shown in parentheses after a provider. Distinct from
# `AUTO_NAME_WORD_BY_HARNESS` and the table above, which name chat TABS -- different strings for
# different surfaces, so they are separate tables rather than one pretending to serve both.
HARNESS_LABEL: Final[dict[HarnessType, str]] = {
    HarnessType.CLAUDE: "Claude Code",
    HarnessType.CODEX: "Codex",
    HarnessType.PI_CODING: "Pi",
    HarnessType.ANTIGRAVITY: "Antigravity CLI",
    HarnessType.OPENCODE: "OpenCode",
}

# --- claude -----------------------------------------------------------------------------
# The one CLI with real one-shot auth subcommands, which is why its flows were the first to
# work and why the rest of this file is shaped the way it is.

_CLAUDE_URL_CHARSET: Final = r"[A-Za-z0-9%&=?_.~/:+#-]"
_CLAUDE_URL_SCRAPE = Scrape(
    trigger=r"https://\S*oauth/authorize\S*",
    strict=rf"https://{_CLAUDE_URL_CHARSET}*oauth/authorize{_CLAUDE_URL_CHARSET}*",
    continuation=rf"^{_CLAUDE_URL_CHARSET}+$",
)
_CLAUDE_TOKEN_SCRAPE = Scrape(
    trigger=r"sk-ant-oat01-[A-Za-z0-9_-]+",
    strict=r"sk-ant-oat01-[A-Za-z0-9_-]*",
    continuation=r"^[A-Za-z0-9_-]+$",
    min_length=60,
    drain_until=DrainUntil.EOF,
)
# Two failure classes with different copy: an OAuth error parks the CLI on a retry prompt
# and needs a restart, while a login failure explains itself and is worth echoing.
_CLAUDE_FAILURES: Final = (
    (r"OAuth error", "Anthropic rejected the code. Start over to get a fresh link."),
    (r"Login failed: ?([^\r\n]*)", "{1}"),
)

LANE_ANTHROPIC = Lane(
    id="anthropic",
    provider_name="Anthropic",
    # Every row carries a subtitle, and what it says is what you GET: a subscription you may
    # already pay for, a free tier, a price, a scope. The provider name alone does not tell
    # someone deciding between these whether their existing plan is usable here.
    subtitle="Use your Claude Pro or Max subscription, or pay per token.",
    harness=HarnessType.CLAUDE,
    methods=(
        PtyMethod(
            id="subscription",
            label="Continue with Claude subscription",
            description="Sign in with your Claude Pro or Max account.",
            argv=("auth", "login", "--claudeai"),
            scrape=_CLAUDE_URL_SCRAPE,
            success=r"Login successful",
            failures=_CLAUDE_FAILURES,
            eof_policy=EofPolicy.FAILURE,
        ),
        PasteMethod(
            id="api_key",
            label="Use an API key",
            description="Paste a raw sk-ant-... API key.",
            sink=PasteSink.CLAUDE_ENV,
        ),
        PtyMethod(
            id="setup_token",
            label="Get a long-lived token",
            description="Mint a 1-year subscription token.",
            argv=("setup-token",),
            scrape=_CLAUDE_URL_SCRAPE,
            failures=_CLAUDE_FAILURES,
            submit=Submit.OPTIONAL,
            result_scrape=_CLAUDE_TOKEN_SCRAPE,
            result_sink=PasteSink.CLAUDE_ENV,
        ),
        PtyMethod(
            id="console",
            label="Anthropic Console (API billing)",
            description="Sign in with a Console account to pay per token.",
            argv=("auth", "login", "--console"),
            scrape=_CLAUDE_URL_SCRAPE,
            success=r"Login successful",
            failures=_CLAUDE_FAILURES,
            eof_policy=EofPolicy.FAILURE,
        ),
    ),
)

# --- codex ------------------------------------------------------------------------------
# Inverted from every other lane: the URL is fixed and the CODE is what gets scraped, the
# user types it into the browser, and nothing comes back to the terminal. The CLI polls and
# exits 0 on its own, so process exit is the success signal.

LANE_OPENAI = Lane(
    id="openai",
    provider_name="OpenAI",
    subtitle="Use your ChatGPT Plus or Pro subscription. Free accounts get limited coding usage.",
    harness=HarnessType.CODEX,
    methods=(
        PtyMethod(
            id="device",
            label="Continue with ChatGPT",
            description="Enter a one-time code on another device.",
            argv=("login", "--device-auth"),
            static_url="https://auth.openai.com/codex/device",
            scrape=Scrape(
                trigger=r"[A-Z0-9]{4}-[A-Z0-9]{4,6}",
                strict=r"[A-Z0-9]{4}-[A-Z0-9]{4,6}",
                continuation=r"^[A-Z0-9-]+$",
            ),
            submit=Submit.NONE,
            eof_policy=EofPolicy.SUCCESS,
            # codex renders plainly, without Ink's synchronized updates.
            frame_marker=None,
        ),
    ),
)

# --- antigravity ------------------------------------------------------------------------
# No auth subcommand at all: bare `agy` prompts on first launch. The menu is a blind
# keystroke script, which is exactly why `expect_before_keys` exists.

_AGY_URL_CHARSET: Final = r"[A-Za-z0-9%&=?_.~/:+#-]"
_AGY_URL_SCRAPE = Scrape(
    trigger=r"https://accounts\.google\.com/o/oauth2/auth\S*",
    strict=rf"https://accounts\.google\.com/o/oauth2/auth{_AGY_URL_CHARSET}*",
    continuation=rf"^{_AGY_URL_CHARSET}+$",
    # The real URL is ~700 characters. Without a floor the strict pattern happily matches
    # the first row of a wrapped one -- which is a valid-looking URL missing response_type,
    # so Google answers 401 rather than anything that reads as truncation.
    min_length=400,
)
_AGY_FAILURES: Final = ((r"Got an error: ([^\r\n]*)", "{1}"),)
_AGY_MENU = r"Select login method:"
_AGY_PTY_COLUMNS: Final = 1000

LANE_GOOGLE = Lane(
    id="google",
    provider_name="Google",
    subtitle="Use your Google AI subscription. All Google accounts have some limited free usage.",
    harness=HarnessType.ANTIGRAVITY,
    methods=(
        PtyMethod(
            id="oauth",
            label="Continue with Google",
            description="Sign in with your Google account.",
            expect_before_keys=_AGY_MENU,
            # The cursor already sits on "1. Google OAuth", so Enter selects it.
            keys=("\r",),
            pty_columns=_AGY_PTY_COLUMNS,
            scrape=_AGY_URL_SCRAPE,
            failures=_AGY_FAILURES,
            # agy drops straight into its chat TUI on success and prints no success line.
            frame_marker=None,
        ),
        PtyMethod(
            id="gcloud",
            label="Use a Google Cloud project",
            description="Sign in through a Google Cloud project instead.",
            expect_before_keys=_AGY_MENU,
            # Down to "2. Use a Google Cloud project", Enter, then Enter again on the
            # "Continue with Google Cloud" row of the second menu. Measured against the
            # real CLI: the menu says "Use arrow keys to navigate, Enter to select" and
            # typing the row's digit does nothing, so this cannot be shortened to ("2",).
            keys=("\x1b[B", "\r", "\r"),
            pty_columns=_AGY_PTY_COLUMNS,
            scrape=_AGY_URL_SCRAPE,
            failures=_AGY_FAILURES,
            frame_marker=None,
        ),
    ),
)

# --- pi ---------------------------------------------------------------------------------
# Both pi lanes are plain file writes. pi's auth.json is a map keyed by provider id, so
# "one provider per account folder" is our rule, not pi's -- we simply never write two.

# Every provider pi can be handed a plain API key for, taken from its own registry rather
# than from a list we curated: `pi-ai/dist/providers/<id>.models.js` names each provider and
# the module beside it names the environment variable its key is read from. The id is what
# matters -- `auth.json` is keyed by it, so a display name we invent is cosmetic but an id we
# invent is a credential pi will never find.
#
# Deliberately absent, and why:
#   amazon-bedrock, cloudflare-*   need cloud credentials, not one key
#   azure-openai-responses         needs an endpoint and a deployment alongside the key
#   google-vertex                  needs a project and a location
#   github-copilot, openai-codex   OAuth only (and codex is its own lane)
#   opencode-go                    has its own lane, so listing it here would duplicate it
#
# Sorted by display name: this is a list you scan for a name you already have in mind.
_PI_KEY_PROVIDERS: Final = tuple(
    sorted(
        (
            KeyProvider(provider_id="ant-ling", display="Ant Ling", env_var="ANT_LING_API_KEY", hint=""),
            KeyProvider(provider_id="anthropic", display="Anthropic", env_var="ANTHROPIC_API_KEY", hint="sk-ant-..."),
            KeyProvider(provider_id="cerebras", display="Cerebras", env_var="CEREBRAS_API_KEY", hint="csk-..."),
            KeyProvider(provider_id="deepseek", display="DeepSeek", env_var="DEEPSEEK_API_KEY", hint="sk-..."),
            KeyProvider(provider_id="fireworks", display="Fireworks", env_var="FIREWORKS_API_KEY", hint="fw_..."),
            KeyProvider(provider_id="google", display="Google Gemini", env_var="GEMINI_API_KEY", hint="AIza..."),
            KeyProvider(provider_id="groq", display="Groq", env_var="GROQ_API_KEY", hint="gsk_..."),
            KeyProvider(provider_id="kimi-coding", display="Kimi For Coding", env_var="KIMI_API_KEY", hint="sk-..."),
            KeyProvider(provider_id="minimax", display="MiniMax", env_var="MINIMAX_API_KEY", hint="eyJ..."),
            KeyProvider(provider_id="minimax-cn", display="MiniMax (China)", env_var="MINIMAX_CN_API_KEY", hint="eyJ..."),
            KeyProvider(provider_id="mistral", display="Mistral", env_var="MISTRAL_API_KEY", hint=""),
            KeyProvider(provider_id="moonshotai", display="Moonshot AI", env_var="MOONSHOT_API_KEY", hint="sk-..."),
            KeyProvider(provider_id="moonshotai-cn", display="Moonshot AI (China)", env_var="MOONSHOT_API_KEY", hint="sk-..."),
            KeyProvider(provider_id="nvidia", display="NVIDIA NIM", env_var="NVIDIA_API_KEY", hint="nvapi-..."),
            KeyProvider(provider_id="openai", display="OpenAI", env_var="OPENAI_API_KEY", hint="sk-..."),
            KeyProvider(provider_id="opencode", display="OpenCode Zen", env_var="OPENCODE_API_KEY", hint=""),
            KeyProvider(provider_id="openrouter", display="OpenRouter", env_var="OPENROUTER_API_KEY", hint="sk-or-..."),
            KeyProvider(provider_id="qwen-token-plan", display="Qwen Token Plan", env_var="QWEN_TOKEN_PLAN_API_KEY", hint=""),
            KeyProvider(provider_id="qwen-token-plan-cn", display="Qwen Token Plan (China)", env_var="QWEN_TOKEN_PLAN_CN_API_KEY", hint=""),
            KeyProvider(provider_id="together", display="Together AI", env_var="TOGETHER_API_KEY", hint=""),
            KeyProvider(provider_id="vercel-ai-gateway", display="Vercel AI Gateway", env_var="AI_GATEWAY_API_KEY", hint=""),
            KeyProvider(provider_id="xai", display="xAI", env_var="XAI_API_KEY", hint="xai-..."),
            KeyProvider(provider_id="xiaomi", display="Xiaomi MiMo", env_var="XIAOMI_API_KEY", hint=""),
            KeyProvider(provider_id="xiaomi-token-plan-ams", display="Xiaomi MiMo Token Plan (Amsterdam)", env_var="XIAOMI_TOKEN_PLAN_AMS_API_KEY", hint=""),
            KeyProvider(provider_id="xiaomi-token-plan-cn", display="Xiaomi MiMo Token Plan (China)", env_var="XIAOMI_TOKEN_PLAN_CN_API_KEY", hint=""),
            KeyProvider(provider_id="xiaomi-token-plan-sgp", display="Xiaomi MiMo Token Plan (Singapore)", env_var="XIAOMI_TOKEN_PLAN_SGP_API_KEY", hint=""),
            KeyProvider(provider_id="zai", display="ZAI Coding Plan (Global)", env_var="ZAI_API_KEY", hint=""),
            KeyProvider(provider_id="zai-coding-cn", display="ZAI Coding Plan (China)", env_var="ZAI_CODING_CN_API_KEY", hint=""),
        ),
        key=lambda provider: provider.display.lower(),
    )
)

LANE_OPENCODE_GO = Lane(
    id="opencode-go",
    provider_name="Opencode Go",
    subtitle="Generous usage on the latest and greatest open models for $10/mo.",
    harness=HarnessType.PI_CODING,
    methods=(
        PasteMethod(
            id="api_key",
            label="Paste your Opencode Go key",
            description="A $10/mo subscription, then one key for every model on it.",
            sink=PasteSink.PI_AUTH_JSON,
            signup_url="https://opencode.ai/go",
        ),
    ),
    key_providers=(
        KeyProvider(provider_id="opencode-go", display="Opencode Go", env_var="OPENCODE_API_KEY", hint=""),
    ),
)

# Same shape as Opencode Go, because it is the same thing: a named provider that runs on pi
# and signs in by pasting a key. Its own row rather than an entry in the generic API-key list
# so it can carry a subtitle saying what the account gets you -- the generic row cannot, since
# it speaks for twenty-eight providers at once.
LANE_OPENROUTER = Lane(
    id="openrouter",
    provider_name="OpenRouter",
    subtitle="One account, any model. Pay for only what you use, not subscription.",
    harness=HarnessType.PI_CODING,
    methods=(
        PasteMethod(
            id="api_key",
            label="Paste your OpenRouter key",
            description="From openrouter.ai/keys.",
            sink=PasteSink.PI_AUTH_JSON,
        ),
    ),
    key_providers=(
        KeyProvider(
            provider_id="openrouter", display="OpenRouter", env_var="OPENROUTER_API_KEY", hint="sk-or-..."
        ),
    ),
)

LANE_API_KEY = Lane(
    id="api-key",
    provider_name="API key",
    subtitle="Paste any provider's key directly",
    harness=HarnessType.PI_CODING,
    methods=(
        PasteMethod(
            id="api_key",
            label="Paste an API key",
            description="Pick the provider, then paste its key.",
            sink=PasteSink.PI_AUTH_JSON,
        ),
    ),
    key_providers=_PI_KEY_PROVIDERS,
)


# Display order in the provider chooser.
LANES: Final[tuple[Lane, ...]] = (
    LANE_ANTHROPIC,
    LANE_OPENAI,
    LANE_GOOGLE,
    LANE_OPENCODE_GO,
    LANE_OPENROUTER,
    LANE_API_KEY,
)

_LANES_BY_ID: Final[dict[str, Lane]] = {lane.id: lane for lane in LANES}


def get_lane(lane_id: str) -> Lane:
    lane = _LANES_BY_ID.get(lane_id)
    if lane is None:
        raise LaneNotFoundError(f"no such lane: {lane_id}")
    return lane


def get_method(lane_id: str, method_id: str) -> PtyMethod | PasteMethod:
    lane = get_lane(lane_id)
    for method in lane.methods:
        if method.id == method_id:
            return method
    raise LaneNotFoundError(f"lane {lane_id} has no method {method_id}")


def account_label(provider_display: str, harness: HarnessType, seq: int) -> str:
    """"Anthropic (Claude Code)", and "Anthropic (Claude Code) 2" for the second one."""
    suffix = "" if seq <= 1 else f" {seq}"
    return f"{provider_display} ({HARNESS_LABEL[harness]}){suffix}"
