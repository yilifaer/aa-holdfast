# aa-holdfast

[![CI](https://github.com/yilifaer/aa-holdfast/actions/workflows/ci.yml/badge.svg)](https://github.com/yilifaer/aa-holdfast/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/aa-holdfast)](https://pypi.org/project/aa-holdfast/)
[![Python](https://img.shields.io/pypi/pyversions/aa-holdfast)](https://pypi.org/project/aa-holdfast/)
[![License](https://img.shields.io/badge/license-GPLv3-blue)](https://github.com/yilifaer/aa-holdfast/blob/main/LICENSE)


Sovereignty Hub fuel and Orbital Skyhook monitoring for [Alliance Auth](https://gitlab.com/allianceauth/allianceauth), built on the Equinox ESI routes.

![Sovereignty hub fuel](https://raw.githubusercontent.com/yilifaer/aa-holdfast/main/docs/images/sov-fuel.png)

<sub>Every screenshot here comes from a demo install seeded with invented
systems, corporations and characters. Fuel timers and den holders are
intelligence, and a real one does not belong in a public README.</sub>


## Why this exists

CCP shipped ESI routes for sovereignty hubs and skyhooks on **2026-05-19**, but almost nobody built on them, because they are invisible unless you ask for them correctly:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://esi.evetech.net/skyhooks/raidable
```

That returns **404**. Add `-H "X-Compatibility-Date: 2026-05-19"` and it returns 200. The old `https://esi.evetech.net/latest/swagger.json` spec is gone (also 404), so anyone still generating a client from it sees none of this.

`django-esi` 9.10+ handles the header for you and defaults to compatibility date `2026-08-18`, which is late enough. This app pins its own date in `holdfast/__init__.py`.

## What it does

One app, three doors. The sidebar carries **SOV Monitor**, **Skyhook Monitor** and **Den Monitor** as separate entries, because to the people using them they are three different jobs -- but they share a sync layer, a rate-limit budget and a set of tokens, which is exactly why they are not three apps.

### SOV Monitor

| Page | Source | Who sees it |
|---|---|---|
| Dashboard | everything below, worst first | officer |
| Hub Fuel | `/corporations/{id}/structures/sovereignty-hubs{,/{id}}` | officer |
| ADM | `/sovereignty/systems` | officer |
| Timers | vulnerability windows, **only while one is running** | officer |
| System Cost | `/industry/systems` | **any member** |
| Settings | live thresholds and notification switches | manager |

![Sovereignty timers](https://raw.githubusercontent.com/yilifaer/aa-holdfast/main/docs/images/sov-timers.png)

<sub>The hub detail route carries no state field, so a running Entosis defence
event is the only public evidence that one is actually being attacked. Green is
safe, amber is inside its daily window, red has a campaign against it.</sub>

### Skyhook Monitor

![Skyhook dashboard](https://raw.githubusercontent.com/yilifaer/aa-holdfast/main/docs/images/skyhook-dashboard.png)

<sub>Theft windows inside the configured horizon, amber where a reagent is
over its bar. The feed underneath is the same data ordered by what happens
next.</sub>

| Page | Source | Who sees it |
|---|---|---|
| Dashboard | everything stealable in the next 24 hours | officer |
| Skyhooks | `/corporations/{id}/structures/skyhooks{,/{id}}`, **stealable only** | officer |
| Timers | reinforcement, **only while one is running** | officer |
| Raid Targets | `/skyhooks/raidable`, with real planet names | **any member** |
| Settings | per-reagent bars, lead time, Discord switch | manager |

Workforce and power skyhooks are left out of the list on purpose. They hold no stock and ESI gives them no theft window, so on a real 413-skyhook alliance including them means 362 rows of noise around the 51 that matter.

### Den Monitor

![Den admin](https://raw.githubusercontent.com/yilifaer/aa-holdfast/main/docs/images/den-admin.png)

<sub>Two dens the app can see and two free sites. `auto` came from a token,
`manual` from somebody typing it. `measured` and `inferred` are the two
siphon detectors, and the difference between them is how sure they are.</sub>

| Page | Shows | Who sees it |
|---|---|---|
| Dashboard | your dens; an officer also sees the alliance picture | member |
| My Dens | your own dens, plus your contact details | **any member** |
| Timers | your den clocks and recent events | **any member** |
| Den List | which sites are free, and which are being siphoned -- **no holder names** | member |
| Den Admin | holders, contacts, claim approvals, manual records | officer |
| Settings | siphon sensitivity and notification switches | manager |

The den list is deliberately anonymous. What every member needs is whether the ground is free and whether whatever sits on it is costing the alliance workforce; a directory of who farms where is a different thing, and it lives behind the officer tier.

Every list filters instantly: a search box plus a dropdown per marked column, built from the values actually present. No dependency -- the biggest table here is a couple of hundred rows, and vendoring DataTables would only mean fighting the copies other Alliance Auth apps already ship.

### Fuel maths

The hub detail route returns, per reagent, `amount` and `burning_per_hour`, plus a `reagent_bay.last_updated` timestamp. Hours of fuel left is `amount / burning_per_hour`, counted **from `last_updated`**, not from when you made the request — the bay contents are a snapshot CCP caches for an hour. The hub's dry-out time is the earliest across all its reagents.

`upgrades[].power_state` is the other half of the picture: `Low` means an upgrade is starved of fuel, power or workforce right now, which is a harder signal than a countdown.

Three bands -- amber, red, critical -- are set in days in Django admin (**Holdfast -> Configuration**, default 7 / 3 / 1). They colour the dashboard *and* fire the Discord alerts from the same numbers, so the board and the channel can never disagree about what counts as urgent.

## Requirements

- Alliance Auth 4.6+
- django-esi 9.10+ (needed for compatibility-date support)
- django-eveuniverse, dhooks-lite, PyYAML

## Install

```bash
pip install -e /path/to/aa-holdfast
```

Add to `INSTALLED_APPS` in `myauth/settings/local.py`:

```python
INSTALLED_APPS += ["eveuniverse", "holdfast"]
```

`eveuniverse` is a separate Django app, not just a library: this app has
foreign keys into its models, so leaving it out stops Alliance Auth from
starting at all. Several other Auth apps also add it, which is exactly why it
is easy to forget -- it is often already there for some other reason. Adding it
twice is harmless.

No `eveuniverse` preload is needed. Types, systems and planets are fetched the
first time they are seen and kept, so there is no hour-long
`eveuniverse_load_data` step before the app is usable.

Then:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
supervisorctl restart myauth:
```

### Scheduled tasks

Eleven of them. Merge the shipped schedule into `local.py` rather than pasting
the entries by hand -- leaving one out does not fail loudly, it quietly removes
a feature:

```python
from holdfast.schedule import CELERYBEAT_SCHEDULE as HOLDFAST_SCHEDULE

CELERYBEAT_SCHEDULE.update(HOLDFAST_SCHEDULE)
```

Retune any single entry afterwards if you want to:

```python
CELERYBEAT_SCHEDULE["holdfast_update_all_owners"]["schedule"] = crontab(minute="*/30")
```

The minute offsets in the shipped schedule are deliberate. The owner task runs
every 15 minutes and each run pulls two listings plus
`HOLDFAST_DETAIL_CALLS_PER_RUN` structure details, oldest first, so structures
rotate through refresh rather than all being pulled at once -- see
[Rate limiting](#rate-limiting) for why that matters. `track_workforce` follows
five minutes behind it so it reads numbers that were just refreshed, and the
rest are spread across the hour so they do not all reach for the same rate-limit
bucket at once.

| Task | Runs | What stops working without it |
|---|---|---|
| `update_all_owners` | `7,22,37,52` | Everything. Hubs and skyhooks never sync |
| `track_workforce` | `12,27,42,57` | No siphon detection against a skyhook's own peak |
| `resolve_planets` | `9,24,39,54` | Every list shows bare planet ids |
| `update_sov_map` | `*/15` | The ADM board stays empty |
| `update_system_costs` | `:35` | The system cost page stays empty |
| `refresh_raidable` | `*/10` | The raid target page waits on ESI, or stays empty |
| `update_campaigns` | `*/5` | The timer board never shows a reinforced hub |
| `update_all_den_characters` | `3,23,43` | Dens, operations and attack notifications never arrive |
| `sync_den_slots` | `:15` | New temperate-planet skyhooks never become claimable slots |
| `run_alerts` | `*/10` | Nothing is ever posted to Discord |
| `prune_alert_log` | `04:40` | The alert log grows forever |

## Rate limiting

Each hub and each skyhook needs its own detail call, and ESI meters those in
tokens rather than requests: a `2xx` costs **two**, a `304` costs **one**. The
`corp-structure` bucket holds **300 tokens per 15 minutes**, and CCP keys it on
**application and character together**.

The application is the EVE SSO application, and an Alliance Auth install has
exactly one of those — the `ESI_SSO_CLIENT_ID` in `local.py`, which every
module on the site authenticates through. So the bucket is shared with every
*other Auth module on your own install* that touches the same character's
token: `aa-structures`, `aa-moonmining`, `corptools`. It is **not** shared with
somebody else's Auth, or with a third-party tool the same character has
authorised — those have their own client ids and their own buckets.

Which is the useful half: when this app reports being rate limited, the cause
is inside your install and you can do something about it.

Counting calls and calling them tokens is how the first release ended up
spending two thirds of the bucket while its own comments claimed one third.

A real alliance corporation can hold 150+ structures, so refreshing everything every run exhausts the bucket and leaves rows stale with no way to tell which ones. Instead:

1. Both listings are pulled every run (2 calls). They are complete, so they decide what exists — creating rows for new structures and deleting departed ones.
2. A fixed budget of detail calls is spent on whatever has gone longest without one, hub and skyhook alike.

At the default 60 calls per run on a 15-minute schedule, a 150-structure
corporation refreshes fully in about 40 minutes — inside the hour CCP caches
these routes for anyway. The worst case is `60 x 2 + 2 x 2 = 124` tokens, or
**41%** of the bucket, leaving the rest for the other modules on your site.
Structures awaiting their first detail pull show as *queued* in the UI.

Both listings and the details come out of one queue ordered by staleness. An
earlier version split the budget between them in proportion to their counts,
which starved the smaller list: one hub among 199 skyhooks was allotted
`round(100 * 1/200)`, and Python rounds a half to even, so it got nothing at
all — every run, with no error anywhere.

If `ESIBucketLimitException` is raised mid-run anyway (because another app drained the bucket), the run stops cleanly and the owner is marked with a note; the next run continues from where it left off.

## Registering corporations

**These are corporation endpoints, not alliance endpoints.** A token only ever covers the corporation its character belongs to, and the character must hold the in-game **Station Manager** role. An alliance executor's token does not reach member corporations — every member corp that holds hubs or skyhooks needs to register its own token on the Owners page.

The public ADM page is the exception: it covers every system your alliance holds whether or not that corporation has registered.

## Permissions

Each section has its own ladder, and holding a higher rung implies the lower ones -- a manager never needs the officer permission granted alongside.

| Section | Member | Officer | Manager |
|---|---|---|---|
| Sovereignty | `sov_basic` | `sov_officer` | `sov_manage` |
| Skyhooks | `skyhook_basic` | `skyhook_officer` | `skyhook_manage` |
| Dens | `den_basic`, then `den_member` | `den_officer` | `den_manage` |

Plus two that cut across: `den_claim` to apply for a den site, and `manage_owners` to register tokens.

Scope is separate from tier. An officer sees the registered corporations in
**their own alliance**, and the den pages will not let anyone read or write a
site outside it.

**This is one alliance's tool, though.** The pages filter by alliance, but the
settings, the alert thresholds and the Discord channels are one set for the
whole install, and the alert checks sweep every owner. Two alliances sharing an
Auth would get each other's fuel warnings in the same channel and would be
editing each other's thresholds. If that is your situation, run a second Auth --
nothing here is built to keep two of you apart.

### Give the member tier to a State, not a group

The three `*_basic` permissions cover the pages any member should have: system
cost, raid targets, and their own dens. Attach them to your **Member state**
(Django admin -> Authentication -> States) rather than to a group. A state
follows alliance or corporation membership, so people get these the moment they
join and lose them the moment they leave -- nobody requests anything and nobody
has to be pruned later.

Leave the `Blue` and `Guest` states without any of them: everything above the
member tier is your own sovereignty data.

A suggested layout for the rest:

| Group | Permissions |
|---|---|
| Alliance leadership | `sov_manage`, `skyhook_manage`, `den_manage`, `den_claim`, `manage_owners` |
| Officers / FCs | `sov_officer`, `skyhook_officer`, `den_officer`, `den_claim` |
| Den operators (request to join) | `den_basic`, `den_member`, `den_claim` |

**`den_claim` needs `den_member` beside it.** `den_claim` only permits the
action; the Den List is where the claim button lives, and that page is gated by
`den_member`. A group holding one without the other looks like it can claim a
site and cannot reach the page to do it.

### Settings live in the app, not in Django admin

Each section has its own settings page behind its `*_manage` permission, so an alliance's SOV manager can retune fuel bands without a Django superuser account. Django admin still works for anything else.

## Mercenary dens

**Every den route in ESI is character scoped.** There is no corporation or public equivalent, and no notification type for "somebody anchored a den next to your skyhook". That shapes the whole feature:

- **Our own dens** fill in automatically, but only from each operator's own token.
- **Every other den** is invisible to ESI and gets recorded by hand -- and most of them are friendly. An alliance usually knows exactly who runs a den long before that person registers a token here, so a hand record says who it is and whether they are one of ours. A record is superseded the day its operator does register: a den the app can read outranks a note somebody typed.
- **Two automatic tells** that a den is taking workforce, since from anarchy level 2 it siphons the skyhook it sits on:
  - An untouched skyhook always reports a workforce that is a round multiple of ten. A percentage off it usually is not, so an un-round figure means a den, with no history needed at all. Reported as `measured`.
  - That is blind when the base was a multiple of a hundred (9200 -> 8280 is still round). Against a peak the app recorded itself, the ratio then lands exactly on a known rate. Reported as `inferred`, because unlike the arithmetic it trusts the peak to have been clean.
  - A drop no rate explains is reported as `suspected` and named as such.

### Slots and claims

Dens anchor within 10 km of a skyhook, and only on **temperate** planets, so the set of possible den sites is exactly the set of temperate-planet skyhooks you hold. Those become slots automatically; nobody maintains a list.

A slot moves through **free -> claim pending -> assigned -> anchored**. It reads **recorded** when a den is on it that only a person has told us about, and **hostile** when that den belongs to someone outside the alliance.

1. A member joins the *Den Operators* group through Alliance Auth's normal group request.
2. They claim a free slot. The claim goes through EVE SSO up front, so an approved claim is live immediately rather than waiting on someone to come back and authorise.
3. A den manager approves. Approving one applicant automatically rejects everyone else queued on that slot.
4. Once anchored, the den's development level, anarchy level, state and reinforcement timer appear on the Dens page and the dashboard.

An approved claim with nothing anchored after `den_anchor_grace_days` is flagged in the UI. It is never revoked automatically -- that is a manager's call, and there is a button for it. Revoking cannot unanchor anything, so the slot keeps saying *revoked, den still up* until the operator takes it down. An applicant whose claim was turned down can clear it off their own page; the row stays for anyone reviewing the history.

### Den events

Three things can happen to a den, and each has exactly one authoritative source. They are deliberately not cross-wired, or every event would fire twice:

| Event | Source | Why |
|---|---|---|
| Under attack now | `MercenaryDenAttacked` notification | The den route carries no HP; this is the only signal |
| Reinforced | den route, `state == Paused` | Survives notification roll-off; the notification is recorded silently |
| New tactical operation | MTO route, `state` is `Available` or `Started` | Carries the expiry the notification lacks. Both live states count: started is not finished, and it still runs against a clock |

EVE stores notifications server-side, so nothing is missed while an operator is logged out. Operators authorise three scopes at claim time -- `esi-structures.read_character.v1`, `esi-activities.read_character.v1` and `esi-characters.read_notifications.v1` -- all at once, because adding a scope later means making every operator re-authorise.

## Alerts

![A siphon alert in Discord](https://raw.githubusercontent.com/yilifaer/aa-holdfast/main/docs/images/discord-alert.png)

<sub>Every moment appears three times: how far away it is, the reader's own
local time, and EVE time. Discord renders the first two in each viewer's
language and timezone -- which is why they are in Chinese above and will not be
for you -- while the third is the same for everyone, and is the one you paste
into a fleet ping. <b>How we know</b> is there because the two siphon detectors
are not equally sure of themselves.</sub>

Create a webhook in Django admin (*Holdfast → Webhooks*) with a Discord channel webhook URL, then:

```bash
python manage.py holdfast_test_webhook
python manage.py holdfast_test_alerts             # one sample of every alert
python manage.py holdfast_import_dens dens.csv    # load a hand-kept den census
```

`holdfast_import_dens` takes a CSV of `planet,owner,corporation,hostile` and
records a den on each matching slot. Most alliances already keep this in a
spreadsheet. Rows whose planet does not match a slot are reported rather than
guessed at -- planet names are full of characters a survey sheet gets wrong
(`0` for `O`, `O` for `Q`), and attaching a record to the wrong planet is worse
than not importing it. Add `--clear-missing` when the file is a full census,
and `--dry-run` to see what would change.

Five checks run on the alert task:

- **Sov hub fuel** — fires as a hub crosses each configured band (default 7 / 3 / 1 days). Refuelling moves the predicted dry-out time, which re-arms every band.
- **Unpowered upgrades** — edge-triggered when any upgrade goes `Low`, re-armed once it recovers.
- **Skyhook theft window** — fires `HOLDFAST_SKYHOOK_THEFT_LEAD_MINUTES` before an owned skyhook becomes lootable, but only once some reagent on it clears that reagent's own bar (see below).
- **Skyhook under attack** — edge-triggered on entering a reinforced state.
- **Low ADM** — edge-triggered below `HOLDFAST_ADM_ALERT_THRESHOLD`.
- **Den under attack** — from the notification, the only source for "being shot right now".
- **Den reinforced** — edge-triggered from the den route.
- **Tactical operation available** — with its expiry.
- **Workforce shortfall** — a skyhook sitting below its own peak for longer than the grace period, with no den of ours on it. Suppressed when we already know what is there.

Every alert is deduplicated through the `AlertLog` table, so a repeating sync never re-sends a warning you have already seen. Nothing is recorded as sent unless a webhook actually accepted it — otherwise a backlog raised before any webhook existed would be swallowed and never reappear.

### Per-reagent theft thresholds

Reagents are not worth the same trip. On a real alliance's skyhooks the two differ by an order of magnitude:

| Reagent | Skyhooks holding unsecured stock | Max | Median |
|---|---|---|---|
| Magmatic Gas | 23 | 175,644 | 30,780 |
| Superionic Ice | 7 | 10,296 | 3,840 |

One global number cannot serve both, so each reagent gets its own bar, editable in Django admin under **Holdfast → Reagent alert thresholds** (`/admin/holdfast/reagentthreshold/`). The list view is editable in place — change the number, hit Save.

A row is created automatically the first time a reagent is seen on a skyhook, seeded from `HOLDFAST_SKYHOOK_MIN_UNSECURED`. Unchecking *is enabled* silences that reagent entirely. Reagents with no row fall back to the setting.

## Settings

Most of what an alliance retunes lives on the three in-app settings pages, not
here: fuel bands, per-reagent theft bars, the theft-window horizon, siphon
thresholds, the claim grace period, and which Discord channel each alert
category goes to. The values below are the ones that shape how the app talks to
ESI, and they belong in `local.py`. All optional; defaults shown.

```python
HOLDFAST_ESI_COMPATIBILITY_DATE = "2026-08-18"   # must be >= 2026-05-19
HOLDFAST_SKYHOOK_THEFT_LEAD_MINUTES = 45
HOLDFAST_SKYHOOK_MIN_UNSECURED = 100              # fallback only; see per-reagent bars
HOLDFAST_DETAIL_CALLS_PER_RUN = 60               # per owner, per sync run
HOLDFAST_DEN_DETAIL_CALLS_PER_RUN = 10           # per den operator, per sync run
HOLDFAST_DEN_NOTIFICATION_MAX_AGE_HOURS = 24     # older than this is backfill
HOLDFAST_DEN_FIRST_SYNC_GRACE_MINUTES = 90       # on an operator's first sync
HOLDFAST_ADM_ALERT_THRESHOLD = 3.0               # None disables
HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS = []           # extra alliances for the ADM page
HOLDFAST_RAIDABLE_CACHE_SECONDS = 300
HOLDFAST_OWNER_SYNC_JITTER_SECONDS = 120
HOLDFAST_STALE_PRUNE_DAYS = 14
```

## Commands

```bash
python manage.py holdfast_update              # full sync in the foreground
python manage.py holdfast_update --owner 1    # one corporation
python manage.py holdfast_update --skip-alerts --skip-public
python manage.py holdfast_test_webhook
```

## Notes on behaviour

- `django-esi` raises `HTTPNotModified` when an ETag matches but its response cache has expired. That is normal and every call site here treats it as "nothing changed", not as an error.
- Planets are not bulk-imported by django-eveuniverse, so each new skyhook costs one extra ESI call to resolve its planet name. The result is stored permanently, so it happens once per skyhook, ever.
- Only systems held by a tracked alliance are stored from the sovereignty map. Keeping all 5485 would mean pointless disk churn.
- The raidable list lives in Redis rather than the database — ~200 rows that turn over every few minutes.
- A tactical operation's `dungeon_type_id` indexes dungeons, not inventory types. Looking it up in the type tables returns whatever item shares the number (12367 comes back as the skill *Explosive Shield Compensation*), and no route names a dungeon, so it is shown as a number.

## Relationship to aa-structures

`aa-structures` parses in-game Skyhook *notifications* (deployed / lost shields / destroyed) to build timers. It does not use the ESI structure routes, so it cannot see fuel levels, reagent stock or theft windows. The two apps complement each other: aa-structures tells you a hook was attacked, this one tells you it is about to run dry or be looted.