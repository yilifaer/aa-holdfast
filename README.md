# aa-holdfast

[![CI](https://github.com/yilifaer/aa-holdfast/actions/workflows/ci.yml/badge.svg)](https://github.com/yilifaer/aa-holdfast/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/aa-holdfast)](https://pypi.org/project/aa-holdfast/)
[![Python](https://img.shields.io/pypi/pyversions/aa-holdfast)](https://pypi.org/project/aa-holdfast/)
[![License](https://img.shields.io/badge/license-GPLv3-blue)](https://github.com/yilifaer/aa-holdfast/blob/main/LICENSE)
[![Alliance Auth Apps](https://img.shields.io/badge/Alliance%20Auth-app%20directory-2C3E50)](https://apps.allianceauth.org/apps/detail/aa-holdfast)

Sovereignty hub fuel, orbital skyhook theft windows and mercenary den sites for
[Alliance Auth](https://gitlab.com/allianceauth/allianceauth), on the Equinox
ESI routes. Warns Discord before a hub runs dry or a skyhook becomes lootable,
and finds dens siphoning your workforce that ESI will not show you directly.

![Sovereignty hub fuel](https://raw.githubusercontent.com/yilifaer/aa-holdfast/main/docs/images/sov-fuel.png)

*Screenshots use a demo install with invented systems and characters. Regenerate
with `docs/demo_seed.py`.*

## Install

```bash
pip install aa-holdfast
```

Add to the **end** of `myauth/settings/local.py`:

```python
if not any(app == "eveuniverse" or app.startswith("eveuniverse.") for app in INSTALLED_APPS):
    INSTALLED_APPS += ["eveuniverse"]
INSTALLED_APPS += ["holdfast"]

from holdfast.schedule import CELERYBEAT_SCHEDULE as HOLDFAST_SCHEDULE
CELERYBEAT_SCHEDULE.update(HOLDFAST_SCHEDULE)
```

Then:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
supervisorctl restart myauth:
```

That is the whole install. The version running shows beside the page title on
every Holdfast page, so nobody needs Django admin to answer "which one is
this". Three things worth knowing:

- **`eveuniverse` must be in `INSTALLED_APPS`, exactly once.** This app has
  foreign keys into its models, so leaving it out stops Auth from starting --
  and so does listing it twice: Django refuses duplicate apps. aa-structures
  and several other apps add it too, some by its AppConfig path
  (`eveuniverse.apps.EveUniverseConfig`), hence the check above.
- **No `eveuniverse` preload.** Types, systems and planets are fetched as they
  are first seen.
- **11 scheduled tasks** come from `holdfast.schedule`. Merging the dict beats
  pasting eleven entries: a missing one does not error, it silently removes a
  feature. Retune one afterwards with
  `CELERYBEAT_SCHEDULE["holdfast_update_all_owners"]["schedule"] = crontab(...)`,
  or read [`holdfast/schedule.py`](https://github.com/yilifaer/aa-holdfast/blob/main/holdfast/schedule.py) for what each does.

Requires Alliance Auth 5.2 or later 5.x (CI runs 5.2, 5.4 and the latest
release), django-esi 9.10 or 10.x (9.10 is the oldest CI tests), django-eveuniverse
2.0+, dhooks-lite, PyYAML.

**Your EVE developer application** (developers.eveonline.com, the one Auth's
`ESI_SSO_CLIENT_ID` belongs to) must have these four scopes enabled, or the
SSO step fails:

| Scope | Used for |
|---|---|
| `esi-structures.read_corporation.v1` | sovereignty hubs and skyhooks (corporation token) |
| `esi-structures.read_character.v1` | a member's own mercenary dens |
| `esi-activities.read_character.v1` | tactical operations on those dens |
| `esi-characters.read_notifications.v1` | "your den is being shot" |

## Setup

**1. Register a corporation.** Grant yourself `manage_owners`; a **Tokens**
tab then appears beside Settings in every section, and **Add corporation**
there takes you through EVE SSO. These are *corporation* endpoints: the
character needs the in-game **Station Manager** role, and an executor's token
does not reach member corps. Every corp holding hubs or skyhooks registers its
own. The first registration also decides the tracked alliance (below). The
Tokens page shows what is tracked, and an **Enable / Disable** button on each
corporation switches it off without deleting anything: its structures leave
every page, badge and alert while it is off and come back when it is on.

> **An aa-structures token will not do.** That app holds
> `esi-corporations.read_structures.v1`; these routes need
> `esi-structures.read_corporation.v1`. Similar names, different scopes — the
> Add button mints the right one.

**2. Add a Discord webhook** on the **Webhooks** page — beside Tokens in every
section's header, open to any section manager; Django admin uses the same
form — then pick which alerts go where on each section's settings page. **Send test**
on a webhook posts one sample to that channel alone, pings included.

**3. Grant permissions** — see [Permissions](#permissions).

**One tracked alliance per install.** The first corporation registered decides
it (an upgrade picks the alliance most registered corporations are in); the Tokens page shows it, and a SOV manager can change it on the SOV
settings page. Corporations outside it cannot be registered and are never
synced — a member with a Station Manager alt in another alliance cannot pull
that alliance's whole sovereignty map onto your dashboard. A corporation with
no alliance may be registered by a member of the tracked alliance. To follow a
second alliance's sovereignty alongside, list it in
`HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS`; its corporations may then be registered
too. Settings, thresholds and Discord channels are still one set per install,
and Django admin bypasses the registration guard. Scope follows the
corporation's alliance as Auth records it: registration refreshes that record
from ESI first, and Auth refreshes it on its own schedule after that. Two
alliances wanting separate warnings should run a second Auth.

## Permissions

One bottom rung for the whole app, then a ladder per section where every rung
includes the ones below it — grant one per section, never two.

| Section | Member | User | Officer | Manager |
|---|---|---|---|---|
| All sections | `basic_access` | — | — | — |
| Sovereignty | — | — | `sov_officer` | `sov_manage` |
| Skyhooks | — | — | `skyhook_officer` | `skyhook_manage` |
| Dens | — | `den_member` | `den_officer` | `den_manage` |

`basic_access` opens the member-facing page of each section: System Cost, Raid
Targets, and your own dens and timers. A den **user** may also apply for a den
slot — permission to anchor in your space. `manage_owners` registers
and switches corporation tokens and sits outside the ladders; someone who holds
only that gets the SOV Monitor entry, which leads to the Tokens page. Den
characters need none of it: each player registers their own.

Holdfast declares only these nine. Its models carry none of Django's
add/change/delete/view permissions, so Django admin is for superusers.

Put `basic_access` on your **Member state** rather than a group: it covers only
what any member should see, and a state follows membership, so nobody has to
be pruned later. Officers see the whole tracked alliance, not one corporation;
see [Design notes](#design-notes). A typical split:

| Who | Grant |
|---|---|
| Member state | `basic_access` |
| Den operators | `den_member` |
| Den officers | `den_officer` |
| Sov fuel team | `sov_officer` |
| Skyhook team | `skyhook_officer` |
| Leadership | `sov_manage`, `skyhook_manage`, `den_manage`, `manage_owners` |

## charlink

With aa-charlink installed, Holdfast adds one entry to its page, **ticked by
default**, asking for all four scopes above. Linking a character registers:

| What | When |
|---|---|
| a den character | for anyone with den access (`basic_access` or a den permission); one with nothing to sync is re-checked every `HOLDFAST_DEN_IDLE_HOURS` |
| its corporation, as on the Tokens page | only for a `manage_owners` holder, only inside the tracked alliance, and only if the corporation is not registered yet |

A registered corporation keeps its token character; switching it is done on the
Tokens page. charlink stores an entry's default the first time it sees it, so
migration 0013 (0.1.10) flipped the stored default to ticked once. Untick it in
charlink's admin and it stays off.

## What it does

Three sidebar entries sharing one sync layer, one rate-limit budget and one set
of tokens.

### SOV Monitor

| Page | Who sees it |
|---|---|
| Dashboard, Hub Fuel, ADM, Timers | officer |
| System Cost | any member |
| Settings | manager |

![Sovereignty timers](https://raw.githubusercontent.com/yilifaer/aa-holdfast/main/docs/images/sov-timers.png)

The hub detail route carries no state field, so a running Entosis defence event
is the only public evidence a hub is actually under attack. Green is safe, amber
is inside its daily window, red has a campaign against it.

### Skyhook Monitor

![Skyhook dashboard](https://raw.githubusercontent.com/yilifaer/aa-holdfast/main/docs/images/skyhook-dashboard.png)

Theft windows inside a configurable horizon, amber where a reagent is over its
own bar. Only skyhooks holding reagents appear — workforce and power flow
straight to the hub, so there is nothing on the others to steal. On a
413-skyhook alliance that is 51 rows rather than 413.

Each reagent gets its own threshold, because they are not worth the same trip:
one alliance's magmatic gas median was 30,780 against 3,840 for superionic ice.

### Den Monitor

![Den admin](https://raw.githubusercontent.com/yilifaer/aa-holdfast/main/docs/images/den-admin.png)

Every den route in ESI is character scoped — no corporation or public
equivalent, and no notification for "somebody anchored a den next to your
skyhook". So:

- **Your own dens** sync from each operator's token.
- **Everyone else's** are recorded by hand, friendly or hostile. A hand record
  is superseded automatically the day its operator registers a token.
- **Dens taking your workforce** are found two ways. An untouched skyhook always
  reports a workforce figure that is a round multiple of ten; a percentage off
  it usually is not, which needs no history at all (`measured`). When the base
  was a multiple of a hundred that tell is absent, and the ratio against a peak
  this app recorded itself gives it away instead (`inferred`).

Den sites are the temperate-planet skyhooks you hold; slots appear on their own.
A member joins a group, claims a free slot through EVE SSO, a manager approves,
and the den syncs once anchored. Managers can revoke — which does not unanchor
anything, so the slot says *revoked, den still up* until it comes down. Claims
are decided on the den admin page, which tells the member; Django admin shows
a claim's status but will not change it.

The den admin page lists the people to contact: whoever runs a den or holds a
pending or approved claim on the ground that officer sees — not every character
registered, which with charlink is most of the alliance's alts. Someone who
loses den access (leaves, or drops to a state without it) leaves that list and
their characters stop being polled; their records stay, and they pick up again
if access comes back.

Tactical operations on your own dens appear on the den dashboard and the
sidebar badge, and are announced to Discord, once the den is at the anarchy
level set on the den settings page — 2 by default, since below that nobody
runs them; 0 counts every operation. An operation on a den below the bar is
announced the day the den reaches it. **Ignore** on a dashboard row takes that
one operation off your own dashboard and badge and nothing else. Both the
rows and the badge count need the dashboard (`den_member` or above); with
basic access alone your operations show on the timers page only.

## Alerts

![A siphon alert in Discord](https://raw.githubusercontent.com/yilifaer/aa-holdfast/main/docs/images/discord-alert.png)

Every moment appears three times: how far away it is, the reader's local time,
and EVE time. Discord renders the first two per viewer — which is why they are
in Chinese above and will not be for you — and the third is the same for
everyone.

Per-category routing, so fuel warnings and "something is being shot" can go to
different channels. A category nobody has routed goes to every enabled webhook,
so a fresh install alerts somewhere. Once channels are ticked for it, it goes
only there: if they are all deleted or switched off it goes nowhere rather than
to every channel, and the settings and Webhooks pages list it until it is
routed again. A webhook Discord answers 404 (deleted) or 401 (token reset) is
switched off automatically with the reason shown on the Webhooks page; ticking
Enabled again clears it. A 429 or 5xx is retried on the next run. Thresholds and switches live on each section's settings
page, not in `local.py`. A webhook can mention Auth groups instead of @here —
pick them on the Webhooks page; each needs a Discord role of the same name,
which Auth's Discord service creates. A webhook URL must be Discord's own
(`https://discord.com/api/webhooks/...`, or the `discordapp.com`, canary and
PTB spellings); the form refuses anything else, here and in Django admin,
since the server posts every alert to whatever is saved there. The URL is the
channel's password, so once saved it is only ever shown masked
(`https://discord.com/api/webhooks/1234…/…abcd`); leave the field empty when
editing to keep it, or paste a new one to replace it.

```bash
python manage.py holdfast_test_alerts             # one sample of every alert
python manage.py holdfast_test_webhook
python manage.py holdfast_update                  # sync now, in the foreground
python manage.py holdfast_import_dens dens.csv    # load a den census
```

`holdfast_update` runs every scheduled job once, in the schedule's order:
owners, den characters (idle ones too), den slots, workforce tracking, planet
names, cost indices, campaigns, the sov map, raidable skyhooks, then alerts. A
step that fails is reported and the rest still run. `--owner PK` syncs one
owner, even a disabled or out-of-scope one; `--skip-dens`, `--skip-public` (the
four public routes) and `--skip-alerts` leave those parts out; `--budget` sets
the detail calls per owner. Only the alert-log prune is left to beat.

`holdfast_import_dens` takes `planet,owner,corporation,hostile` and records a
den on each matching slot. Rows whose planet does not match are reported rather
than guessed at — planet names are full of characters a survey sheet gets wrong
(`0` for `O`, `O` for `Q`). Add `--clear-missing` for a full census, `--dry-run`
to preview.

## Rate limiting

ESI meters in tokens, not requests: a `2xx` costs two, a `304` costs one. The
`corp-structure` bucket holds 300 tokens per 15 minutes, keyed on application
and character together — and an Auth install has one ESI application, so that
bucket is shared with `aa-structures` and `corptools` **on your own site**, not
with anyone else's.

Each run pulls both listings (they decide what exists) and spends
`HOLDFAST_DETAIL_CALLS_PER_RUN` detail calls on whatever has gone longest
without one. At the default 60 that is `60 x 2 + 2 x 2 = 124` tokens, 41% of the
bucket, and a 150-structure corp refreshes fully in about 40 minutes — inside
the hour CCP caches these routes anyway. That rotation is why the owner sync
runs every 15 minutes: the listings change at most hourly, and most runs read
the same two again for 4 tokens. Structures awaiting a first detail pull show
as *queued*.

## Settings

All optional; most tuning belongs on the in-app settings pages instead.

```python
HOLDFAST_ESI_COMPATIBILITY_DATE = "2026-08-18"   # must be >= 2026-05-19
HOLDFAST_DETAIL_CALLS_PER_RUN = 60               # per owner, per run
HOLDFAST_DEN_DETAIL_CALLS_PER_RUN = 10
HOLDFAST_SKYHOOK_MIN_UNSECURED = 100             # fallback; see per-reagent bars
HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS = []           # tracked alongside the alliance on the SOV settings page
HOLDFAST_OWNER_SYNC_JITTER_SECONDS = 120
HOLDFAST_PLANET_RESOLVE_PER_RUN = 60
HOLDFAST_DEN_NOTIFICATION_MAX_AGE_HOURS = 24      # an older den attack is neither announced nor retried
HOLDFAST_DEN_FIRST_SYNC_GRACE_MINUTES = 90
HOLDFAST_DEN_IDLE_HOURS = 6                      # re-check a character with no dens this often
HOLDFAST_RAIDABLE_CACHE_SECONDS = 1800           # keep above the 10-minute refresh
HOLDFAST_STALE_PRUNE_DAYS = 14                   # alert log rows that can stop nothing
HOLDFAST_DEN_EVENT_RETENTION_DAYS = 30           # den notification text
```

## Notes

- Logs go through Auth's extension logger, so they land in `log/extensions.log`
  beside every other app's, named by module (`holdfast.core.alerts`, ...).
- These routes need an `X-Compatibility-Date` of 2026-05-19 or later. Without
  one ESI answers 404 rather than saying why, and the old
  `/latest/swagger.json` spec is gone — which is why so little is built on them.
  django-esi sends the header; this app pins its own date.
- A tactical operation's `dungeon_type_id` indexes dungeons, not inventory
  types, so it is shown as a number. Nothing names a dungeon.
- `aa-structures` parses skyhook *notifications* and builds timers from them. It
  does not read the structure routes, so it cannot see fuel, reagent stock or
  theft windows. The two complement each other.

## Data protection

Beyond ESI structure data, Holdfast keeps:

| Data | About | Kept |
|---|---|---|
| Den notification text: attacker character, corporation and alliance ids | anyone, members or not | `HOLDFAST_DEN_EVENT_RETENTION_DAYS` (30), pruned daily |
| Hand-recorded den owners and corporations (den slots, CSV import) | anyone, as typed by a manager | until edited, or the slot goes |
| Timezone and contact note | members, entered by themselves | until the user is deleted |
| Den claims and decisions | members | as history, also after the slot goes |

Den officers see the timezone, note, alts and Discord name (read from Auth's
Discord service, not stored here) of the people running a den or holding a
claim on their ground. Den attack alerts post the attacker's corporation or
alliance name to Discord. The alert log holds only keys, pruned after
`HOLDFAST_STALE_PRUNE_DAYS` once they can no longer stop an alert repeating.

## Upgrade

```bash
pip install -U aa-holdfast
python manage.py migrate
python manage.py collectstatic --noinput
supervisorctl restart myauth:
```

Read the [CHANGELOG](https://github.com/yilifaer/aa-holdfast/blob/main/CHANGELOG.md)
first. The schedule comes from the package, so new or retuned beat entries
arrive with the restart.

## Uninstall

In this order — beat keeps its schedule in the database, so the rows go
before the tables or it keeps running tasks against tables that are gone;
`migrate holdfast zero` needs the app still installed; and `local.py` imports
`holdfast.schedule`, so settings stop loading if the package goes first:

1. Delete the two `holdfast.schedule` lines from `local.py`, and any line that
   retunes a `holdfast_` entry, so a restart does not put the schedule back
   (a leftover retune would fail to load without them).
2. In `python manage.py shell`, stop the schedule:

   ```python
   from django_celery_beat.models import PeriodicTask
   PeriodicTask.objects.filter(name__startswith="holdfast_").delete()
   ```

3. `supervisorctl restart myauth:`, and give the workers a few minutes to
   finish any Holdfast task already queued.
4. `python manage.py migrate holdfast zero`, and step 5 straight after: until
   that restart, the sidebar's Holdfast badges have no tables to count.
5. Delete `INSTALLED_APPS += ["holdfast"]` (keep `eveuniverse` if another app
   uses it), then `supervisorctl restart myauth:`.
6. In `python manage.py shell`, remove what Auth keeps by name:

   ```python
   from django.apps import apps
   from django.contrib.contenttypes.models import ContentType
   from django.core.cache import cache

   ContentType.objects.filter(app_label="holdfast").delete()  # permissions and grants go with them
   cache.delete("holdfast:sov_map_tracked")
   if apps.is_installed("charlink"):
       apps.get_model("charlink", "AppSettings").objects.filter(app_name="holdfast_tools").delete()
   ```

   Not `remove_stale_contenttypes --include-stale-apps`: that removes every
   stale app's content types, not only Holdfast's.
7. `pip uninstall aa-holdfast`, and restart once more.

ESI tokens stay with django-esi, where other apps may use them.

## Design notes

- **One alliance per install, no corporation tier.** Officers and managers see
  every registered corporation of the tracked alliance: fuel, timers and dens
  are run across corporations, so a corporation view would hide what the job
  needs. Separate audiences need separate Auths.
- **ESI is fetched here, not read from corptools.** corptools needs a
  corporation audit per corporation, syncs on its own cadence, and holds no
  skyhooks, raidable list, sov map or campaigns, which would have to be fetched
  here anyway.
- **Django admin is for superusers.** It applies the Webhooks page's URL rules
  and shows a claim's status read-only. It still skips the Tokens page's
  alliance check — an owner outside the tracked alliance just sits unsynced —
  and lets you edit synced rows, which the next sync overwrites.
- **A corporation token outlives its registrant's permission.** An owner keeps
  syncing with the token it was registered with after that user loses
  `manage_owners`, until a manager disables it on the Tokens page: the
  permission is checked when registering, not on every sync.
- **CI runs sqlite.** MariaDB, MySQL and PostgreSQL are not tested.

## Development

```bash
python runtests.py          # sqlite, no Auth project needed
ruff check holdfast tests
```

[`docs/README.md`](https://github.com/yilifaer/aa-holdfast/blob/main/docs/README.md) covers the demo seeder, the screenshot
script, and `docs/mutate_structure.py`, which breaks each structural rule on
purpose to check the test guarding it actually fails.
