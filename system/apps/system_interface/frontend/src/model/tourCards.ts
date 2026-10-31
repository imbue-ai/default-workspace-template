/**
 * The show-around tour's cards (the new-onboarding spec, section 2.6): what each says, what it points at, and the
 * picture, link, or action it carries. A card points at something the shell draws itself (``target``), names a
 * control of the Imbue Studio chrome for the chrome to ring (``chromeAnchor``), or neither, in which case it pops
 * out of the agent's avatar. A card with an action does it as Next is pressed. The pictures are hosted under
 * content-addressed names; nothing is bundled.
 */

/** What the shell draws that a card can sit beside: the agent's avatar, the desktop's shortcuts, or the launcher. */
export type TourTarget = "avatar" | "shortcuts" | "launcher";

/** A control of the Imbue Studio chrome a card talks about, as the embed contract names it (minds:highlight-chrome). */
export type ChromeAnchor = "workspace-switcher" | "permissions" | "settings" | "share" | "help";

/** What the shell does as a card's Next is pressed: snap the window the avatar's pinned entry stands for to the
 *  right half of the desktop. The shell names no app; the avatar's window is the pinned one whose pin declares
 *  the avatar style (``reducers/desktopState.ts`` ``avatarPinnedWindow``). */
export type TourAction = "snap-avatar-window-right";

export interface TourImage {
  readonly url: string;
  readonly alt: string;
}

export interface TourLink {
  readonly label: string;
  readonly url: string;
}

export interface TourCard {
  readonly id: string;
  /** The card's words: one or more short paragraphs, separated by a blank line. */
  readonly text: string;
  /** Lines listed under the text, when the card has them. */
  readonly bullets: readonly string[] | null;
  readonly target: TourTarget | null;
  readonly chromeAnchor: ChromeAnchor | null;
  readonly image: TourImage | null;
  readonly link: TourLink | null;
  readonly action: TourAction | null;
  /** The label of the card's forward button; "Next" when null, "Done" on the last card. */
  readonly buttonLabel: string | null;
}

const ONBOARDING_MEDIA_BASE_URL = "https://onboarding-media.imbueminds.com";

function card(fields: Partial<TourCard> & Pick<TourCard, "id" | "text">): TourCard {
  return {
    bullets: null,
    target: null,
    chromeAnchor: null,
    image: null,
    link: null,
    action: null,
    buttonLabel: null,
    ...fields,
  };
}

export const TOUR_CARDS: readonly TourCard[] = [
  card({
    id: "intro",
    text: "While I work on that, let me show you around.\n\nI’ll let you know when I’m done.",
    target: "avatar",
  }),
  card({ id: "agent", text: "I’m your Studio agent.\n\nClick me anytime to open our chat.", target: "avatar" }),
  card({
    id: "workspace",
    text:
      "This is your workspace: a virtual computer where everything is customizable, from the apps you build to " +
      "the interface itself.",
    chromeAnchor: "workspace-switcher",
  }),
  card({
    id: "chat-window",
    text: "Our chat is a window like any other.\n\nLet’s make it smaller so you can see the rest of your desktop.",
    target: "avatar",
    action: "snap-avatar-window-right",
  }),
  card({
    id: "apps",
    text: "Your apps live here.\n\nUse them inside the workspace, or drag them onto your own desktop.",
    target: "shortcuts",
  }),
  card({
    id: "connect",
    text:
      "Bring in info from the apps and folders you already use.\n\n" +
      "Just ask me to connect them, and your logins stay secure.",
    bullets: ["Apps like Gmail, Slack, and 100’s more", "Folders on your computer, or in Google Drive, Dropbox, etc"],
    chromeAnchor: "permissions",
    image: {
      url: `${ONBOARDING_MEDIA_BASE_URL}/e8d3b6777dd115923888f1486459766765c0822a44a2b429a7db7b118b8b49f5.gif`,
      alt: "Logos of services a workspace can connect to, sliding by: Gmail, Slack, Google Calendar, Notion, GitHub, and more",
    },
  }),
  card({
    id: "security",
    text: "Your workspace is encrypted and backed up automatically.\n\nOnly people you share it with can see it.",
    chromeAnchor: "settings",
  }),
  card({
    id: "share",
    text: "Share your whole workspace, or any app you build, with your team, friends, or family.",
    chromeAnchor: "share",
  }),
  card({
    id: "devices",
    text: "Everything you make works on any device.",
    target: "avatar",
    image: {
      url: `${ONBOARDING_MEDIA_BASE_URL}/688427f2d430397eaba2072c7089bc4e94e5ed6f81e8dff00ed7f4f1579e8bcd.png`,
      alt: "The same workspace on a computer and on a phone",
    },
  }),
  card({
    id: "feedback",
    text:
      "Something not working? Tell me and I’ll let the team know.\n\n" +
      "You can also share feedback with us on Discord.",
    chromeAnchor: "help",
    link: { label: "Join our Discord", url: "https://discord.com/invite/GvK8MsCVgk" },
  }),
  card({
    id: "providers",
    text: "You can switch AI providers, even mid-chat.",
    target: "avatar",
    image: {
      url: `${ONBOARDING_MEDIA_BASE_URL}/343b56057e673a194cd4fa7271e3562c121ff0ae1fda10e528ec5e83d735cd9d.png`,
      alt: "The chat's provider menu",
    },
  }),
  card({
    id: "durable",
    text: "Apps built in Studio are made to last, with tests that keep them working over time.",
    target: "avatar",
  }),
  card({
    id: "done",
    text:
      "That’s the tour.\n\nIf you have questions about anything, just ask me in the chat.\n\n" +
      "You can take it again anytime from the launcher.",
    target: "launcher",
    buttonLabel: "Done",
  }),
];

/** What the exit badge says, pointing at the launcher, when the tour is left early. */
export const TOUR_EXIT_BADGE_TEXT =
  "You can take the tour again any time: open the launcher and choose Show me around.";
export const TOUR_EXIT_BADGE_BUTTON_LABEL = "Got it";

/** What the card after an interrupted one says: the agent answered while the tour was on, so it waits. */
export const TOUR_PAUSED_TEXT = "Looks like I’m done.\n\nLet’s go take a look, and we’ll finish the tour later.";
export const TOUR_PAUSED_BUTTON_LABEL = "OK";

/** The launcher row that runs the tour, and the words it matches. */
export const TOUR_LAUNCHER_LABEL = "Show me around";
export const TOUR_LAUNCHER_SEARCH_WORDS: readonly string[] = ["tour", "walkthrough"];

/** The selectors, in order of preference, of the shell's own markers a target is found at (contracts.md section 12). */
export function tourTargetSelectors(target: TourTarget): readonly string[] {
  switch (target) {
    case "avatar":
      return ['[data-floating-entries] [data-pinned-entry][data-entry-style="avatar"]', "[data-pinned-entry]"];
    case "shortcuts":
      return ["[data-shortcut]"];
    case "launcher":
      return ["[data-launcher-field]"];
  }
}
