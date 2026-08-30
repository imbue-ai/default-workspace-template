// @vitest-environment jsdom
//
// Sanitizing untrusted markup is only meaningful against a real parser -- the
// whole point is that the browser's reading of the markup, not ours, is what
// decides what it does -- so this file runs in jsdom while the rest of the
// suite stays on vitest's node default. Every "an attacker writes X" case below
// is therefore parsed the way the workspace would parse it.

import { beforeEach, describe, expect, it, vi } from "vitest";

// The app list is the machine's, delivered over the WebSocket. Standing in for
// it keeps these tests about the icon rule and away from the socket.
const registry = vi.hoisted(() => ({ apps: [] as { name: string; url: string; label: string; icon?: string }[] }));
vi.mock("../models/AgentManager", () => ({ getApps: () => registry.apps }));

import { MAX_ICON_LENGTH, appIconMarkup, sanitizeIconMarkup, serviceIconMarkup } from "./appIcon";
import { icon } from "./icons";

const FALLBACK = '<svg class="generic-glyph"></svg>';

const SQUARE_ICON = '<svg viewBox="0 0 24 24" fill="none"><path d="M6 6h12v12H6z"/></svg>';

/** Parse sanitized markup back into an element, so assertions read attributes
 *  and elements rather than substrings of a serialization. */
function parsed(markup: string): SVGElement {
  const host = document.createElement("div");
  host.innerHTML = markup;
  const root = host.firstElementChild;
  if (root === null) throw new Error("sanitized markup held no element");
  return root as SVGElement;
}

describe("sanitizeIconMarkup", () => {
  it("keeps an ordinary stroke icon's art", () => {
    const markup = sanitizeIconMarkup('<svg viewBox="0 0 24 24"><path d="M4 4h16v16H4z"/></svg>', 16);
    expect(markup).not.toBeNull();
    const root = parsed(markup as string);
    expect(root.tagName.toLowerCase()).toBe("svg");
    expect(root.querySelector("path")?.getAttribute("d")).toBe("M4 4h16v16H4z");
  });

  it("draws at the size the caller asked for, on the icon's own grid", () => {
    const root = parsed(sanitizeIconMarkup('<svg viewBox="0 0 32 32"><circle cx="16" cy="16" r="8"/></svg>', 14)!);
    expect(root.getAttribute("width")).toBe("14");
    expect(root.getAttribute("height")).toBe("14");
    expect(root.getAttribute("viewBox")).toBe("0 0 32 32");
  });

  it("scales an icon that gave a size instead of a viewBox", () => {
    const root = parsed(sanitizeIconMarkup('<svg width="48" height="48"><circle cx="24" cy="24" r="8"/></svg>', 16)!);
    expect(root.getAttribute("viewBox")).toBe("0 0 48 48");
    expect(root.getAttribute("width")).toBe("16");
  });

  it("refuses art with neither a viewBox nor a size to derive one from", () => {
    expect(sanitizeIconMarkup('<svg><circle cx="4" cy="4" r="2"/></svg>', 16)).toBeNull();
  });

  it("is decorative and unfocusable, like the built-in glyphs", () => {
    const root = parsed(sanitizeIconMarkup('<svg viewBox="0 0 24 24"><path d="M0 0h4v4H0z"/></svg>', 16)!);
    expect(root.getAttribute("aria-hidden")).toBe("true");
    expect(root.getAttribute("focusable")).toBe("false");
  });

  it("lets a monochrome icon inherit currentColor", () => {
    const root = parsed(sanitizeIconMarkup('<svg viewBox="0 0 24 24"><path d="M0 0h4v4H0z"/></svg>', 16)!);
    expect(root.getAttribute("fill")).toBe("currentColor");
  });

  it("leaves an icon that paints itself alone", () => {
    const root = parsed(
      sanitizeIconMarkup('<svg viewBox="0 0 24 24" fill="none"><path d="M0 0h4v4H0z" fill="#e11d48"/></svg>', 16)!,
    );
    expect(root.getAttribute("fill")).toBe("none");
    expect(root.querySelector("path")?.getAttribute("fill")).toBe("#e11d48");
  });

  it("strips a script element", () => {
    const markup = sanitizeIconMarkup(
      '<svg viewBox="0 0 24 24"><script>alert(1)</script><path d="M0 0h4v4H0z"/></svg>',
      16,
    );
    expect(markup).not.toBeNull();
    expect(parsed(markup as string).querySelector("script")).toBeNull();
    expect(markup).not.toContain("alert");
  });

  it("strips event handlers wherever they sit", () => {
    const root = parsed(
      sanitizeIconMarkup(
        '<svg viewBox="0 0 24 24" onload="alert(1)"><path d="M0 0h4v4H0z" onclick="alert(2)"/></svg>',
        16,
      )!,
    );
    expect(root.getAttribute("onload")).toBeNull();
    expect(root.querySelector("path")?.getAttribute("onclick")).toBeNull();
  });

  it("strips a javascript: URL", () => {
    const markup = sanitizeIconMarkup(
      '<svg viewBox="0 0 24 24"><use href="javascript:alert(1)"/><path d="M0 0h4v4H0z"/></svg>',
      16,
    );
    expect(markup).not.toBeNull();
    expect(markup?.toLowerCase()).not.toContain("javascript:");
  });

  it("strips a javascript: URL that hides behind whitespace", () => {
    const markup = sanitizeIconMarkup(
      '<svg viewBox="0 0 24 24"><use href="java\nscript:alert(1)"/><path d="M0 0h4v4H0z"/></svg>',
      16,
    );
    expect(markup).not.toBeNull();
    expect(markup?.toLowerCase().replace(/\s/g, "")).not.toContain("javascript:");
  });

  it("strips an element that would navigate", () => {
    const markup = sanitizeIconMarkup(
      '<svg viewBox="0 0 24 24"><a href="https://example.com"><path d="M0 0h4v4H0z"/></a></svg>',
      16,
    );
    expect(markup).not.toBeNull();
    expect(parsed(markup as string).querySelector("a")).toBeNull();
    expect(markup).not.toContain("example.com");
  });

  it("strips elements that would load a remote resource", () => {
    const markup = sanitizeIconMarkup(
      '<svg viewBox="0 0 24 24"><image href="https://tracker.example/pixel.png"/>' +
        '<iframe src="https://example.com"></iframe><path d="M0 0h4v4H0z"/></svg>',
      16,
    );
    expect(markup).not.toBeNull();
    expect(markup).not.toContain("tracker.example");
    expect(markup).not.toContain("iframe");
  });

  it("strips an external reference on an otherwise ordinary element", () => {
    const root = parsed(
      sanitizeIconMarkup('<svg viewBox="0 0 24 24"><use href="https://example.com/sprite.svg#icon"/></svg>', 16)!,
    );
    const use = root.querySelector("use");
    expect(use === null || use.getAttribute("href") === null).toBe(true);
  });

  it("strips a paint that points outside the icon", () => {
    const markup = sanitizeIconMarkup(
      '<svg viewBox="0 0 24 24"><path d="M0 0h4v4H0z" fill="url(https://example.com/p.svg#g)"/></svg>',
      16,
    );
    expect(markup).not.toBeNull();
    expect(markup).not.toContain("example.com");
  });

  it("strips style, which can fetch", () => {
    const markup = sanitizeIconMarkup(
      '<svg viewBox="0 0 24 24"><style>svg{background:url(https://example.com/p.png)}</style>' +
        '<path d="M0 0h4v4H0z" style="fill:url(https://example.com/p.png)"/></svg>',
      16,
    );
    expect(markup).not.toBeNull();
    expect(markup).not.toContain("example.com");
    expect(
      parsed(markup as string)
        .querySelector("path")
        ?.getAttribute("style"),
    ).toBeNull();
  });

  it("strips classes, which could only borrow the workspace's own rules", () => {
    const root = parsed(
      sanitizeIconMarkup('<svg viewBox="0 0 24 24" class="machine-sidebar"><path d="M0 0h4v4H0z"/></svg>', 16)!,
    );
    expect(root.getAttribute("class")).toBeNull();
  });

  it("strips foreignObject, which embeds arbitrary HTML", () => {
    const markup = sanitizeIconMarkup(
      '<svg viewBox="0 0 24 24"><foreignObject><div>hi</div></foreignObject><path d="M0 0h4v4H0z"/></svg>',
      16,
    );
    expect(markup).not.toBeNull();
    expect(markup?.toLowerCase()).not.toContain("foreignobject");
  });

  it("strips animation, which can retarget an attribute after the fact", () => {
    const markup = sanitizeIconMarkup(
      '<svg viewBox="0 0 24 24"><path d="M0 0h4v4H0z"><animate attributeName="href" to="javascript:alert(1)"/>' +
        "</path></svg>",
      16,
    );
    expect(markup).not.toBeNull();
    expect(markup?.toLowerCase()).not.toContain("animate");
    expect(markup?.toLowerCase()).not.toContain("javascript:");
  });

  it("keeps a same-document reference, so an icon can use its own gradient", () => {
    const root = parsed(
      sanitizeIconMarkup(
        '<svg viewBox="0 0 24 24"><defs><linearGradient id="g"><stop stop-color="#f00"/></linearGradient></defs>' +
          '<path d="M0 0h4v4H0z" fill="url(#g)"/></svg>',
        16,
      )!,
    );
    const gradientId = root.querySelector("linearGradient")?.getAttribute("id");
    expect(gradientId).toBeTruthy();
    expect(root.querySelector("path")?.getAttribute("fill")).toBe(`url(#${gradientId})`);
  });

  it("gives two icons that share an id name distinct ids", () => {
    const first = parsed(
      sanitizeIconMarkup(
        '<svg viewBox="0 0 24 24"><linearGradient id="g"><stop stop-color="#f00"/></linearGradient>' +
          '<path d="M0 0h4v4H0z" fill="url(#g)"/></svg>',
        16,
      )!,
    );
    const second = parsed(
      sanitizeIconMarkup(
        '<svg viewBox="0 0 24 24"><linearGradient id="g"><stop stop-color="#00f"/></linearGradient>' +
          '<path d="M0 0h8v8H0z" fill="url(#g)"/></svg>',
        16,
      )!,
    );
    const firstId = first.querySelector("linearGradient")?.getAttribute("id");
    const secondId = second.querySelector("linearGradient")?.getAttribute("id");
    expect(firstId).not.toBe("g");
    expect(firstId).not.toBe(secondId);
    expect(second.querySelector("path")?.getAttribute("fill")).toBe(`url(#${secondId})`);
  });

  it("draws the same icon identically wherever it is inlined", () => {
    const markup = '<svg viewBox="0 0 24 24"><linearGradient id="g"/><path d="M0 0h4v4H0z" fill="url(#g)"/></svg>';
    expect(sanitizeIconMarkup(markup, 16)).toBe(sanitizeIconMarkup(markup, 16));
  });

  it("refuses markup whose root is not an svg", () => {
    expect(sanitizeIconMarkup('<div><svg viewBox="0 0 24 24"></svg></div>', 16)).toBeNull();
    expect(sanitizeIconMarkup("<img src=x>", 16)).toBeNull();
  });

  it("refuses more than one root, and text beside the root", () => {
    expect(sanitizeIconMarkup('<svg viewBox="0 0 1 1"></svg><svg viewBox="0 0 1 1"></svg>', 16)).toBeNull();
    expect(sanitizeIconMarkup('hello<svg viewBox="0 0 1 1"></svg>', 16)).toBeNull();
  });

  it("refuses empty and oversized markup", () => {
    expect(sanitizeIconMarkup("", 16)).toBeNull();
    expect(sanitizeIconMarkup("   \n ", 16)).toBeNull();
    const huge = `<svg viewBox="0 0 24 24"><path d="${"M0 0h4v4H0z".repeat(MAX_ICON_LENGTH)}"/></svg>`;
    expect(sanitizeIconMarkup(huge, 16)).toBeNull();
  });

  it("refuses markup that is not markup at all", () => {
    expect(sanitizeIconMarkup("not an icon", 16)).toBeNull();
    expect(sanitizeIconMarkup("<svg", 16)).toBeNull();
  });
});

describe("appIconMarkup", () => {
  it("draws the app's own icon when it registered one", () => {
    const markup = appIconMarkup('<svg viewBox="0 0 24 24"><path d="M4 4h16v16H4z"/></svg>', 16, FALLBACK);
    expect(markup).not.toBe(FALLBACK);
    expect(parsed(markup).querySelector("path")?.getAttribute("d")).toBe("M4 4h16v16H4z");
  });

  it("falls back to the caller's glyph when the app registered none", () => {
    expect(appIconMarkup(undefined, 16, FALLBACK)).toBe(FALLBACK);
    expect(appIconMarkup(null, 16, FALLBACK)).toBe(FALLBACK);
    expect(appIconMarkup("", 16, FALLBACK)).toBe(FALLBACK);
    expect(appIconMarkup("   ", 16, FALLBACK)).toBe(FALLBACK);
  });

  it("falls back to the caller's glyph when the icon is malformed", () => {
    expect(appIconMarkup("<svg", 16, FALLBACK)).toBe(FALLBACK);
    expect(appIconMarkup("<div>not an icon</div>", 16, FALLBACK)).toBe(FALLBACK);
    expect(appIconMarkup('<svg><circle r="2"/></svg>', 16, FALLBACK)).toBe(FALLBACK);
  });

  it("draws a sanitized icon rather than falling back when only part of it is unsafe", () => {
    const markup = appIconMarkup(
      '<svg viewBox="0 0 24 24" onload="alert(1)"><path d="M4 4h16v16H4z"/></svg>',
      16,
      FALLBACK,
    );
    expect(markup).not.toBe(FALLBACK);
    expect(parsed(markup).getAttribute("onload")).toBeNull();
  });
});

describe("serviceIconMarkup", () => {
  beforeEach(() => {
    registry.apps = [
      {
        name: "notes",
        url: "http://localhost:9001",
        label: "notes-ab12",
        icon: '<svg viewBox="0 0 24 24" ' + 'fill="none"><path d="M6 6h12v12H6z"/></svg>',
      },
      { name: "plain", url: "http://localhost:9002", label: "plain-cd34" },
      { name: "broken", url: "http://localhost:9003", label: "broken-ef56", icon: "<svg" },
    ];
  });

  it("draws the registered app's icon", () => {
    const markup = serviceIconMarkup("notes", 16, FALLBACK);
    expect(markup).not.toBe(FALLBACK);
    expect(parsed(markup).querySelector("path")?.getAttribute("d")).toBe("M6 6h12v12H6z");
  });

  it("monograms a registered app whose icon is missing or malformed", () => {
    // A registered app always gets something of its own rather than the shared
    // glyph: an unnamed app is the common case, and a list of them all wearing
    // the caller's fallback tells the reader nothing.
    for (const name of ["plain", "broken"]) {
      const markup = serviceIconMarkup(name, 16, FALLBACK);
      expect(markup).not.toBe(FALLBACK);
      expect(parsed(markup).textContent?.trim()).toBe(name.charAt(0).toUpperCase());
    }
  });

  it("draws the monogram in the house icon style, not in colour", () => {
    // Monochrome currentColor strokes on a transparent background, like every
    // glyph in icons.ts. Colour is the projects' identity language, not the
    // apps' -- an app told apart by its letter needs no palette of its own.
    const root = parsed(serviceIconMarkup("plain", 16, FALLBACK));
    expect(root.getAttribute("stroke")).toBe("currentColor");
    expect(root.getAttribute("fill")).toBe("none");
    expect(root.getAttribute("viewBox")).toBe("0 0 24 24");
    expect(root.querySelector("rect")?.getAttribute("fill")).toBeNull();
    expect(root.querySelector("text")?.getAttribute("fill")).toBe("currentColor");
  });

  it("is stable: the same app monograms identically every time", () => {
    expect(serviceIconMarkup("plain", 16, FALLBACK)).toBe(serviceIconMarkup("plain", 16, FALLBACK));
  });

  it("keeps the caller's glyph for a name the machine does not register", () => {
    // Nothing to monogram, and inventing one would dress a dead ref up as a
    // real app.
    expect(serviceIconMarkup("never-registered", 16, FALLBACK)).toBe(FALLBACK);
  });

  it("falls back when the row addresses no service at all", () => {
    expect(serviceIconMarkup(null, 16, FALLBACK)).toBe(FALLBACK);
  });

  it("draws the History primitive with the shell's own clock, not the registry's icon", () => {
    registry.apps = [
      { name: "versioning", url: "http://localhost:8082", label: "versioning-gh78", icon: SQUARE_ICON },
    ];
    const markup = serviceIconMarkup("versioning", 16, FALLBACK);
    expect(markup).toBe(icon("history", { size: 16 }));
    expect(markup).not.toContain("M6 6h12v12H6z");
    expect(serviceIconMarkup("versioning", 14, FALLBACK)).toBe(icon("history", { size: 14 }));
  });

  it("draws the History primitive's clock even with no registry row for it", () => {
    registry.apps = [];
    expect(serviceIconMarkup("versioning", 16, FALLBACK)).toBe(icon("history", { size: 16 }));
  });
});

// Everything above checks one rule at a time against the sanitizer's own
// output string. This checks the whole path the way a hostile app would use
// it: the icon comes off the registry, goes through the call the rail and the
// tab bar actually make, and lands in the document -- and the assertions are
// made against the mounted DOM, after the browser has re-parsed the
// serialization. That re-parse is where mutation-XSS lives: markup that reads
// one way when the sanitizer looks at it and another way once it is written
// out and read back. Sweeping for residue rather than for one named vector is
// also what catches a payload nobody thought to write a case for.
describe("a hostile icon, taken all the way into the document", () => {
  const HOSTILE_ICONS: readonly { name: string; icon: string }[] = [
    { name: "an inline script", icon: '<svg viewBox="0 0 24 24"><script>parent.alert(1)</script></svg>' },
    {
      name: "handlers on the root and on a shape",
      icon: '<svg viewBox="0 0 24 24" onload="parent.alert(1)"><path d="M0 0h4v4H0z" onclick="parent.alert(2)"/></svg>',
    },
    {
      name: "a link carrying a javascript: URL",
      icon: '<svg viewBox="0 0 24 24"><a href="javascript:parent.alert(1)"><path d="M0 0h4v4H0z"/></a></svg>',
    },
    {
      name: "HTML smuggled through foreignObject",
      icon: '<svg viewBox="0 0 24 24"><foreignObject><img src="x" onerror="parent.alert(1)"></foreignObject></svg>',
    },
    {
      name: "a remote image",
      icon: '<svg viewBox="0 0 24 24"><image href="https://evil.example/pixel.png"/></svg>',
    },
    {
      name: "a remote sprite reached through xlink",
      icon: '<svg viewBox="0 0 24 24"><use xlink:href="https://evil.example/sprite.svg#i"/></svg>',
    },
    {
      name: "a data: URI",
      icon: '<svg viewBox="0 0 24 24"><image href="data:image/svg+xml;base64,PHN2Zy8+"/></svg>',
    },
    {
      name: "a filter fetched from another origin",
      icon: '<svg viewBox="0 0 24 24"><path d="M0 0h4v4H0z" filter="url(https://evil.example/f.svg#f)"/></svg>',
    },
    {
      name: "an animation that retargets an attribute after the fact",
      icon: '<svg viewBox="0 0 24 24"><path d="M0 0h4v4H0z"><set attributeName="onload" to="parent.alert(1)"/></path></svg>',
    },
    {
      name: "a comment that tries to close the svg early",
      icon: '<svg viewBox="0 0 24 24"><!--</svg><img src="x" onerror="parent.alert(1)">--><path d="M0 0h4v4H0z"/></svg>',
    },
    {
      name: "a style element that tries to break out of itself",
      icon:
        '<svg viewBox="0 0 24 24"><style><!--</style><img src="x" onerror="parent.alert(1)">--></style>' +
        '<path d="M0 0h4v4H0z"/></svg>',
    },
    {
      name: "CDATA that tries to close its own element",
      icon: '<svg viewBox="0 0 24 24"><desc><![CDATA[</desc><script>parent.alert(1)</script>]]></desc></svg>',
    },
  ];

  // What must not survive anywhere in the mounted subtree, whatever shape the
  // payload arrived in.
  const FORBIDDEN_SELECTOR = "script, style, iframe, foreignObject, image, a, animate, animateTransform, set, handler";

  for (const { name, icon: hostileIcon } of HOSTILE_ICONS) {
    it(`leaves no executable residue: ${name}`, () => {
      registry.apps = [{ name: "hostile", url: "http://localhost:9004", label: "hostile-9z8y", icon: hostileIcon }];
      const host = document.createElement("div");
      document.body.appendChild(host);
      try {
        // Exactly the call the sidebar row and the dock tab make.
        host.innerHTML = serviceIconMarkup("hostile", 16, FALLBACK);
        expect(host.querySelectorAll(FORBIDDEN_SELECTOR)).toHaveLength(0);
        for (const element of [host, ...Array.from(host.querySelectorAll("*"))]) {
          for (const attribute of Array.from(element.attributes)) {
            expect(attribute.name.toLowerCase().startsWith("on")).toBe(false);
            const value = attribute.value.toLowerCase().replace(/[^!-~]/g, "");
            expect(value).not.toContain("javascript:");
            expect(value).not.toContain("evil.example");
            expect(value).not.toContain("data:");
          }
        }
        // Refusing the icon outright is a fine answer -- the fallback is an
        // svg too -- but whatever is drawn is one svg and nothing beside it,
        // never loose HTML that the payload smuggled out of the root.
        expect(host.childNodes).toHaveLength(1);
        expect(host.firstElementChild?.tagName.toLowerCase()).toBe("svg");
      } finally {
        host.remove();
      }
    });
  }
});
