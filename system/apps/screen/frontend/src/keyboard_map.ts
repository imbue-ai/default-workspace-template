/** The keyboard layout neko maps keysyms with (docs/system/specs/screen/spec.md section 13): from the browser's
 *  language, ``us`` unless the language names a layout X knows. */

const LAYOUT_BY_LANGUAGE: Record<string, string> = {
  en: "us",
  de: "de",
  fr: "fr",
  es: "es",
  it: "it",
  pt: "pt",
  nl: "nl",
  sv: "se",
  da: "dk",
  nb: "no",
  nn: "no",
  fi: "fi",
  pl: "pl",
  cs: "cz",
  sk: "sk",
  hu: "hu",
  ru: "ru",
  uk: "ua",
  tr: "tr",
  el: "gr",
  ja: "jp",
  ko: "kr",
};

const VARIANT_BY_LOCALE: Record<string, string> = {
  "en-GB": "gb",
  "pt-BR": "br",
  "fr-CA": "ca",
  "es-419": "latam",
  "de-CH": "ch",
  "fr-CH": "ch",
};

export interface KeyboardMapChoice {
  layout: string;
  variant: string;
}

export function keyboardMapForLanguage(language: string): KeyboardMapChoice {
  const trimmed = language.trim();
  const regional = VARIANT_BY_LOCALE[trimmed];
  if (regional !== undefined) {
    // A regional spelling that is its own layout (gb, br, latam) is the layout, not a variant.
    return { layout: regional, variant: "" };
  }
  const base = trimmed.split("-")[0]?.toLowerCase() ?? "";
  return { layout: LAYOUT_BY_LANGUAGE[base] ?? "us", variant: "" };
}
