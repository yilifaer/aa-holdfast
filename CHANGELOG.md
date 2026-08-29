# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[semantic versioning](https://semver.org/).

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
