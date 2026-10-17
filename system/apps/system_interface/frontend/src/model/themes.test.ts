import { describe, expect, it } from "vitest";
import { themeCatalog, themeRecord } from "../testing/records";
import { STANDARD_CHROME, chromeOf, parseThemeCatalog, resolveDesktopTheme } from "./themes";

describe("parseThemeCatalog", () => {
  it("reads the shell's catalog, leaving out what is malformed rather than failing the read", () => {
    const catalog = parseThemeCatalog({
      default: "paper",
      themes: [
        {
          id: "paper",
          name: "Paper",
          description: "White",
          base: "standard",
          source: "workspace",
          available: true,
          problems: [],
          revision: "r1",
          chrome: { title_align: "center", leading: ["close", "title"], trailing: ["minimize", "maximize"] },
          icons: {
            format: "png",
            size: 32,
            rendering: "pixelated",
            background: "transparent",
            palette: ["#000000"],
            max_colors: null,
            derive: "monochrome",
            fallback_url: "/api/themes/paper/icons/app.png",
            apps: { files: "/api/themes/paper/icons/files.png", bad: 3 },
          },
        },
        { name: "no id" },
        { id: "odd", chrome: { title_align: "center", leading: ["close", "spinner"], trailing: [] } },
      ],
    });

    expect(catalog.default).toBe("paper");
    expect(catalog.themes.map((theme) => theme.id)).toEqual(["paper", "odd"]);
    const [paper, odd] = catalog.themes;
    expect(paper.source).toBe("workspace");
    expect(paper.chrome).toEqual({
      title_align: "center",
      leading: ["close", "title"],
      trailing: ["minimize", "maximize"],
    });
    expect(paper.icons?.apps).toEqual({ files: "/api/themes/paper/icons/files.png" });
    expect(paper.icons?.derive).toBe("monochrome");
    // A chrome naming a slot this build does not know is not trusted: the standard bar is drawn instead.
    expect(odd.chrome).toBeNull();
    expect(chromeOf(odd)).toEqual(STANDARD_CHROME);
    expect(odd.available).toBe(false);
  });

  it("reads an unavailable theme the shell sends with no chrome and no icons", () => {
    const catalog = parseThemeCatalog({
      default: null,
      themes: [{ id: "broken", name: "Broken", available: false, problems: ["no guide"], chrome: null, icons: null }],
    });

    const [broken] = catalog.themes;
    expect(broken.available).toBe(false);
    expect(broken.problems).toEqual(["no guide"]);
    expect(broken.icons).toBeNull();
    expect(chromeOf(broken)).toEqual(STANDARD_CHROME);
  });

  it("reads anything that is not a catalog as an empty one", () => {
    expect(parseThemeCatalog(null)).toEqual({ default: null, themes: [] });
    expect(parseThemeCatalog({ themes: "nope" })).toEqual({ default: null, themes: [] });
  });
});

describe("resolveDesktopTheme", () => {
  const catalog = {
    ...themeCatalog(themeRecord("paper"), themeRecord("ink"), themeRecord("broken", { available: false })),
    default: "ink",
  };

  it("wears the desktop's own theme, else the workspace's default", () => {
    expect(resolveDesktopTheme(catalog, "paper").id).toBe("paper");
    expect(resolveDesktopTheme(catalog, null).id).toBe("ink");
  });

  it("wears the standard look for a choice that is unavailable or gone, and for no default", () => {
    expect(resolveDesktopTheme(catalog, "broken").id).toBe("standard");
    expect(resolveDesktopTheme(catalog, "deleted").id).toBe("standard");
    expect(resolveDesktopTheme({ ...catalog, default: null }, null).id).toBe("standard");
    expect(resolveDesktopTheme({ default: null, themes: [] }, null).id).toBe("standard");
  });
});
