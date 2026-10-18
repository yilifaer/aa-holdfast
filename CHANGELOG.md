# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **Alliance Auth 5.2 on Python 3.11+ installs a django-sri that breaks Auth's
  own templates.** AA 5.2 leaves django-sri unpinned and 1.0 (September 2026)
  drops the `sri_static` tag its base template loads; AA pins `<1` from 5.4.
  This app now pins `django-sri<1` too, so an AA 5.2 install picking it up does
  not end up with every page failing. CI's 5.2 line installs the pin as well.

## [0.1.10] - 2026-10-18

### Added

- **The charlink entry is ticked by default**, so a member who links
  everything in one go links this too. What keeps that affordable is new as
  well: a den character whose sync finds nothing -- no dens, no operations, no
  notifications -- is not listed again for `HOLDFAST_DEN_IDLE_HOURS` (default
  6). Registering or claiming through the app still syncs at once. charlink
  stores each entry's default the first time it sees it, so the upgrade flips
  the stored one too; turn it back off in charlink's admin if you prefer, and
  it will stay off.

- **A Webhooks page.** Discord webhooks could only be made in Django admin,
  which the people running the alliance's alerts do not have. The page sits
  beside Tokens in every section's header, is open to any section manager
  (`sov_manage`, `skyhook_manage` or `den_manage`) and nobody else, and adds,
  edits and deletes webhooks -- name, URL, enabled, ping, ping groups, notes.
  **Send test** on a webhook posts one sample to that channel alone, with its
  own pings, down the same delivery step a real alert takes, and records
  nothing as sent. Which alerts go where is still chosen on each section's
  settings page; Django admin still works.
- **An anarchy bar for tactical operations**, on the den settings page.
  Operations count on the dashboard and the sidebar badge, and are announced
  to Discord, only on dens at this anarchy level or above -- 2 by default,
  since below that nobody runs them; 0 means every operation. One on a den
  below the bar is skipped rather than recorded as sent, so it is announced
  the day the den gets there.
- **Ignore, per tactical operation.** Each operation on the den dashboard's
  "Yours" list has an Ignore button that takes it off your own dashboard and
  badge. The timers page and the Discord channel are untouched: the channel
  was told once when the operation appeared.
- **The version beside the brand** on every Holdfast page, so nobody needs
  Django admin to answer "which one is this".

### Changed

- **Alliance Auth 5.2 or newer.** The floor was 4.6, which nobody has run this
  on since the first week; CI now installs 5.2, 5.4 and the latest release
  side by side, so what the floor promises is what gets tested.
- **`sov_basic`, `skyhook_basic` and `den_basic` are one permission,
  `basic_access`.** Three permissions meant to be granted together were not:
  one alliance handed out `den_basic` alone, and an outside install looked
  for the "Can access" every other Auth app declares and found nothing. The
  upgrade grants `basic_access` to every group, state and user holding any of
  the three, then deletes them. The one visible side effect: an install that
  had granted only `den_basic` now shows those members System Cost and Raid
  Targets too, both public ESI data. The den ladder's labels no longer say
  "member" for a rung that is gone.
- The "no webhooks yet" note on the routing table points at the Webhooks page
  instead of Django admin.
- A webhook URL has to be Discord's own webhook endpoint over https; the form
  refuses anything else. The page hands URL entry to section managers, and
  the server posts every alert to whatever is saved.

### Fixed

- The den badge no longer counts tactical operations for a member with basic
  access alone. Such a member lands on the information page, which does not
  list operations, so the light had no page behind it and no way to clear it.
  Operations count, and Ignore works, for the den list rung and up -- the
  people who can open the dashboard the button is on.

## [0.1.9] - 2026-10-01

### Added

- **A tracked alliance.** The install now knows whose sovereignty it follows:
  set from the first corporation registered (an upgrade picks the alliance
  most already-registered corporations are in), shown on the Tokens page,
  changeable on the SOV settings page. `HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS` still adds more.
  A corporation outside it is refused at registration -- on the Tokens page
  and through charlink alike -- with a message naming the corporation, its
  alliance, the tracked alliance and what to do about it.
- **Enable / Disable on the Tokens page.** Until now the only way to switch a
  corporation off was Django admin. A disabled corporation keeps its rows and
  comes back without a resync. Other people's sidebar badges catch up within
  a minute; the badge counts are cached that long per user.

### Changed

- **Tracked alliances are no longer derived from the registered corporations.**
  That derivation is what made one member's alt in another alliance track
  that alliance. A corporation whose alliance is outside the tracked set is
  not synced, and says so in its sync result.
- **A disabled corporation disappears everywhere.** Its hubs, skyhooks and den
  slots leave every page, every sidebar badge and every alert check while it
  is off; before, only the sync stopped, and the alerts kept firing on frozen
  data. The same rule now covers the two spots that read all skyhooks
  regardless of owner: the "a den is siphoning here" note on the SOV
  dashboard and the "ours" flag on the raid target list.
- Re-registering a token no longer switches on a corporation that was
  disabled on purpose; the message says it is still disabled.
- An alliance-less corporation registered by a member of the tracked alliance
  is now visible to that alliance's officers. Before, only a superuser or a
  user whose main was in the same corporation ever saw its structures.
- `holdfast_update --owner N` reaches a disabled or out-of-scope owner, with a
  warning, so a token can be checked without switching the owner on.
- Registering a corporation refreshes its Auth row from ESI first -- one
  cached public call -- so the alliance the guard judges is the one scope
  will read. Auth otherwise refreshes corporations every six hours, and a row
  made before the corporation joined an alliance said "no alliance" for that
  long.
- The "sov hub reinforced" check treats no active hub as nothing being ours.
  It used to fall back to every IHUB campaign in a tracked system, which the
  Disable button made a one-click state to reach.
- "On our ground" for a mercenary den now means on an active corporation's
  skyhook. Den slots are made for every skyhook on the install, a disabled or
  out-of-scope corporation's included, and a member's den on one of those
  used to count as siphoning our own workforce on their dashboard and badge.
- The Tokens page works out for itself why a corporation is not synced,
  instead of waiting for a sync that never runs to write it down. A
  corporation that fell outside the tracked alliance after a settings change
  used to keep the green result of its last sync beside the "not synced"
  badge.
- The tracked-alliance upgrade is two migrations: 0008 adds the column, 0009
  fills it. On MySQL a column is committed as soon as it is added, so a data
  step failing in the same migration would have left the column in place and
  the retry failing on a duplicate. A non-numeric entry in
  `HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS` is skipped with a warning rather than
  raised on, as it was harmless before.

### Fixed

- **Registering a corporation from another alliance pulled that alliance's
  whole sovereignty map down and alerted on it.** On one install that was 104
  "ADM below threshold" systems on the dashboard and in the sidebar badge, and
  one Discord message per system.
- Sovereignty rows lingered after nothing was tracked any more; the map update
  returned early instead of clearing them. The industry cost table did the
  same. Both are cleared now, and a map update that comes back unchanged still
  drops rows for alliances no longer tracked -- changing the tracked alliance
  used to leave the old one's systems in place until the map itself changed.
- Tracking a new alliance -- setting one where none was, or adding an extra
  in local.py -- fetches the map afresh at once. The map update remembers
  which tracked set its last fetch was for and forces one whenever that
  differs; before, a newly tracked alliance with no rows yet waited on the
  map's ETag, and the settings page's promise of an immediate refresh came
  back as "unchanged".

## [0.1.8] - 2026-09-12

### Added

- **A webhook can mention Auth groups.** Pick them on the webhook in Django
  admin (*Holdfast → Webhooks*); every post then carries `<@&role>` for each
  group that has a Discord role of the same name, which is what Auth's Discord
  service creates. A group without a role is logged and skipped, and the alert
  still goes out. Until now the only pings were @here and @everyone, and on an
  alliance Discord both reach far more people than care -- as the first
  outside install put it, "our skyhook team is the only one which needs a
  ping in the proper channel."

### Changed

- **Den permissions are one ladder, and `den_claim` is gone.** Applying for a
  den slot is part of `den_member` now, and every rung includes the ones below
  it, so a group needs one den permission rather than up to three. Upgrading
  keeps everyone's access: whoever held `den_claim` without `den_member`,
  `den_officer` or `den_manage` is given `den_member`, and then `den_claim` is
  removed. The labels now name their rung -- member, user, officer, manager --
  and say what each adds to the one below.
- Permissions have their own README section, so `#permissions` links to it.
- The app calls itself **Holdfast** in Django admin and the permission picker.
  It was still *SOV Monitor* there -- its name from before the skyhook and den
  sections existed -- which is why den permissions were filed under it.

### Fixed

- **The sidebar folder closed on every Holdfast page.** Auth highlights a menu
  entry by exact URL name or whole namespace, never by prefix, and a prefix is
  what these entries gave it -- so none was ever lit, and an admin who filed
  them under a sidebar folder watched it snap shut on every click. Each entry
  now lists its section's pages; the Tokens page keeps SOV Monitor lit.
- The charlink entry raised `TypeError` whenever charlink asked which users may
  use it: it was handed a permission object where charlink expects names.

## [0.1.7] - 2026-09-04

### Fixed

- **The token registration page had no link to it.** It existed at
  `/holdfast/owners/` and nowhere else; the README called it "the Owners page"
  as if that were somewhere you could click. Nobody here noticed because the
  URL was in muscle memory from day one. The first install outside this
  alliance, with full admin access, could not find it. It is now a **Tokens**
  tab beside Settings in every section, shown to anyone holding
  `manage_owners`, and the empty Hub Fuel page links to it.
- A structural test now fails for any URL that no template links to, so a page
  cannot ship unreachable again.

### Changed

- The README says why an aa-structures token cannot register a corporation:
  `esi-corporations.read_structures.v1` and `esi-structures.read_corporation.v1`
  are different scopes.

## [0.1.6] - 2026-08-31

### Fixed

- **The install command was wrong.** The README opened with
  `pip install -e /path/to/aa-holdfast`, which is how the author installs it
  from a working copy and is useless to everybody else. It is
  `pip install aa-holdfast`. Nobody caught it here because nobody here has ever
  run it -- this machine deploys over SSH from a script.
- The install is one block you can copy, rather than four separated by prose.

### Changed

- The README is two thirds shorter: 3,759 words down to 1,295. A maintainer
  reviewing it for the app directory called it "a whole wall of mostly useless
  text ... stating wrong/uninteresting or obvious things", which was fair. What
  survived is what you need to install it, configure it, and understand the two
  or three things about these ESI routes that will otherwise surprise you.

## [0.1.5] - 2026-08-30

Documentation only. No behaviour changes; upgrading is optional.

### Fixed

- Image captions used `<sub>` tags. GitHub and PyPI render raw HTML in a
  README; the Alliance Auth app directory does not, and printed the tags
  themselves. They are italic now, which says the same thing in all three.

### Added

- A badge linking to this app's entry in the Alliance Auth app directory, and
  an icon under `docs/images/`.

### Notes

- The app directory shows this package's licence as `?`. That is not a
  metadata error: `pyproject.toml` declares a PEP 639 licence expression
  (`GPL-3.0-or-later`), and setuptools refuses outright to also carry the
  superseded `License ::` classifier that older tooling reads. PyPI reports
  the expression correctly.

## [0.1.4] - 2026-08-30

Documentation only. No behaviour changes; upgrading is optional.

### Fixed

- Screenshots and the licence link now use absolute URLs. PyPI renders the
  README on its own, so `docs/images/sov-fuel.png` resolved against pypi.org
  and every image on the project page was a broken-image icon -- which is the
  first thing anyone deciding whether to install this would have seen.

## [0.1.3] - 2026-08-30

Documentation only. No behaviour changes; upgrading is optional.

### Changed

- Corrected which apps share the ESI rate-limit bucket. CCP keys it on
  application and character together, and the application is the EVE SSO
  client id -- of which an Alliance Auth install has exactly one, shared by
  every module on it. So the bucket is shared with `aa-structures` and
  `corptools` on your own site, and **not** with somebody else's Auth or a
  third-party tool the same character has authorised.

  The old wording said "any other app using the same character", which made a
  rate limit sound like weather. It is not: the cause is on your own server and
  you can turn it down.

## [0.1.2] - 2026-08-30

### Fixed

- Fuel alerts walked a hub up the severity ladder one run at a time. The bands
  were checked widest first and the loop stopped at the first one not already
  sent, so a hub discovered with twelve hours of fuel got `warning`, then
  `danger` ten minutes later, then `critical` ten minutes after that. Three
  messages for one situation, opening with the least alarming one -- and an
  officer who read the first would reasonably have put it off. It now reports
  the tightest band the hub currently sits in, once, and says nothing further
  until something changes.

### Changed

- The rate-limiting section of the README describes what the code does. It
  still said 300 requests where ESI counts tokens, 50 calls where the default
  is 60, and a fifth of the bucket where the worst case is 41%. A duplicate
  comment in `app_settings.py` still recommended the old default of 100.

## [0.1.1] - 2026-08-30

Six bugs from an outside review, all of which this alliance's own numbers hid.

### Fixed

- A hub could be starved of its detail budget for ever. The budget was split
  between hubs and skyhooks in proportion to their counts, and Python rounds a
  half to even -- so one hub among 199 skyhooks got `round(100 * 1/200)`, which
  is zero. Every run. Its fuel was never fetched and never alerted on. There is
  one queue now, ordered by whatever went longest without a refresh.
- The rate-limit budget counted calls and called them tokens. ESI charges two
  tokens for a successful response, so a default run spent about two thirds of
  the 300-token bucket while the comments claimed one third. The default drops
  to 60 calls, and the sync jitter to two minutes so two runs of a fifteen
  minute schedule cannot land inside one sliding window.
- Eight settings were write-only: the page saved them and nothing read them.
  The theft lead time and the ADM threshold came from `local.py` instead, and
  four notification switches did nothing at all. **An install that turned
  something off on the settings page was still being alerted about it.**
- Three den write paths looked rows up by primary key with no scope, so anyone
  who could guess an id could claim, approve, revoke or overwrite a site
  outside their own alliance. The list pages had always filtered; the writes
  had not.
- A rate limit part way through the tactical operation sync deleted every
  operation it had not reached yet, even though they were still in the listing.
  The listing is the truth about what exists; the details only decorate it.
- `HOLDFAST_OWNER_SYNC_SECONDS` and `HOLDFAST_FUEL_ALERT_THRESHOLDS` were
  defined and documented and read by nothing. Removed.

### Changed

- The README no longer says one install can serve several alliances. Pages
  filter by alliance but settings, thresholds and Discord channels are global
  and the alert checks sweep every owner, so two alliances sharing an Auth
  would get each other's warnings. It says that now instead.
- The upgrade-offline alert can name a siphoning den as the likely cause, and
  the siphon alerts can say a sovereignty upgrade went dark with the workforce.
  Both are what their settings switches always claimed to do.

## [0.1.0] - 2026-08-29

First release.

### Added

- Sovereignty section: hub fuel with configurable bands, ADM board, a
  three-state timer board (safe / attackable / reinforced), and industry cost
  indices for every system the alliance holds.
- Skyhook section: stealable skyhooks only, per-reagent theft thresholds,
  reinforcement timers, and the public raid target list with real planet names.
- Mercenary den section: den sites derived from temperate-planet skyhooks, a
  claim and approval workflow, an anonymous availability list, an admin view
  with contact details, and tactical operation tracking.
- Hand-written den records for any den ESI cannot show, friendly or hostile.
  Most dens on an alliance's own ground are run by its own members, and knowing
  who runs one does not require them to have registered a token here. A record
  is superseded automatically the day its operator does register: a den the app
  can read outranks a note somebody typed.
- `holdfast_import_dens`, which loads a den census from CSV. Alliances tend to
  already keep one in a spreadsheet, and typing it back one slot at a time
  through the web form is a poor use of an evening. Rows whose planet does not
  match a slot are reported rather than guessed at.
- An operator's own dashboard now carries their tactical operations and, when
  one of their dens has reached anarchy 2, the workforce it is taking off the
  ground it sits on. Both also count towards the sidebar badge.
- Attention feed timestamps show how far away a moment is alongside the EVE
  time, rather than the absolute time alone.
- Workforce siphon detection. An untouched skyhook reports a workforce figure
  that is a round multiple of ten; a den takes a percentage and leaves one that
  usually is not. This needs no history, so it catches dens that were already
  in place before the app was installed.
- Discord alerting with per-category routing, so fuel warnings and "something
  is being shot" can go to different channels.
- Alliance Auth notifications for anything addressed to one person: a decided
  den claim, your own den being attacked or reinforced.
- Sidebar badges showing how many things in each section want attention.
- charlink integration, alongside the built-in token registration page.

### Notes

- Requires ESI compatibility date 2026-05-19 or later. The sovereignty hub and
  skyhook routes do not exist before it, and ESI answers 404 rather than
  telling you why.
- `eveuniverse` must be in `INSTALLED_APPS`. This app has foreign keys into
  its models, so it is a Django app that has to be registered, not just a
  library. Several other Auth apps add it too, which is what makes it easy to
  miss.
- A tactical operation's `dungeon_type_id` indexes dungeons, not inventory
  types. Looking it up in the type tables returns whatever item shares the
  number -- 12367 comes back as the skill *Explosive Shield Compensation* --
  and no route names a dungeon, so it is shown as a number.
- Siphon detection reports how sure it is. `measured` means the workforce
  figure is not a round multiple of ten, which no undisturbed skyhook ever
  reports; `inferred` means it fell to exactly a known rate off a peak this
  app recorded itself, which trusts that peak to have been clean; `suspected`
  means it is below its peak and no rate explains the figure.
- Both `Available` and `Started` tactical operations count as live. Started is
  not finished, and it expires on a clock either way.
