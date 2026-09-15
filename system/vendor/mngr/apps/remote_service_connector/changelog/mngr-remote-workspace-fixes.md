The connector's `api` web function is pinned to Modal's `us` region so desktop requests (auth, leasing, sharing) are no longer served from containers scheduled in Europe or elsewhere. The cron and supervisor functions stay unpinned.

`POST /hosts/claim` (browser creates) now takes its template tag from the release feed's `<channel>-web.json` for the channel the request names (`channel`, the web user's choice in the chrome's Settings; default and unknown values mean `stable`), read on every claim and cached about a minute per channel. The deploy-time `MINDS_WEB_TEMPLATE_REF` pin is now the fallback, used on tiers with no update feed (`MINDS_UPDATE_FEED_BASE_URL` unset) and whenever the feed cannot be read. Deploying a connector therefore no longer moves the web pin on production.

The hosted web chrome's Settings page gains a release channel selector (stable, beta, alpha), stored in a cookie on the chrome origin and sent on the claim body.
