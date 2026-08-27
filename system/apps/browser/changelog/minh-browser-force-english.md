# Force English in fleet browsers

New browsers were coming up with French Google pages. Two independent causes, two fixes.

**The browser never declared a language.** No `--lang`, and the image sets no `LANG`/`LC_ALL`,
so Chrome's language was whatever the base image happened to imply. Now `--lang=en-US` sets the
UI locale and `--accept-lang=en-US,en` sets the Accept-Language header outright -- the header
one matters most because it does not depend on the container locale at all. This fixes every
site that honours Accept-Language, which is nearly all of them.

**Google is not one of them.** It picks its UI language from IP geolocation, falling back to
Accept-Language only weakly. Our egress is an OVH range that is physically in Oregon but
carries French registration metadata: RIPE lists `51.81.0.0/16` as non-RIPE-managed with
country `EU`, and the org is OVH SAS in Roubaix. ipinfo already resolves it correctly to
Portland, Oregon, so the registry data is not uniformly wrong -- Google's database is what is
stale, and third parties cannot correct it.

So the default `BROWSER_HOME_URL` becomes `https://www.google.com/?hl=en`. `hl` is a Google URL
parameter and means nothing elsewhere, so this deliberately is NOT a general mechanism -- it
fixes the one page where it is worth it, because it is the one page we choose. Everything else
is covered by the Accept-Language flags above.

Not attempted, and why:

* **`ENV LANG` in the Dockerfile** -- unnecessary once `--accept-lang` sets the header
  directly, and it would force an image rebuild. This ships through `update-self` as is.
* **Appending `hl=en` in the CDP proxy or at profile creation** -- both would bake
  Google-specific special-casing into general machinery for a cosmetic consent page.
* **Correcting the geolocation at OVH** -- the real fix is an RFC 8805 geofeed, which only the
  resource holder (AS16276) can publish, and Google's ISP Portal only accepts it from them.
  Worth a support ticket, but it is known to revert, so it is not something to depend on.
