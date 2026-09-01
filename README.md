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
INSTALLED_APPS += ["eveuniverse", "holdfast"]

from holdfast.schedule import CELERYBEAT_SCHEDULE as HOLDFAST_SCHEDULE
CELERYBEAT_SCHEDULE.update(HOLDFAST_SCHEDULE)
```

Then:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
supervisorctl restart myauth:
```

That is the whole install. Three things worth knowing:

- **`eveuniverse` must be in `INSTALLED_APPS`.** This app has foreign keys into
  its models. Leave it out and Auth will not start. Adding it twice is harmless
  — several other apps add it too.
- **No `eveuniverse` preload.** Types, systems and planets are fetched as they
  are first seen.
- **11 scheduled tasks** come from `holdfast.schedule`. Merging the dict beats
  pasting eleven entries: a missing one does not error, it silently removes a
  feature. Retune one afterwards with
  `CELERYBEAT_SCHEDULE["holdfast_update_all_owners"]["schedule"] = crontab(...)`,
  or read [`holdfast/schedule.py`](https://github.com/yilifaer/aa-holdfast/blob/main/holdfast/schedule.py) for what each does.

Requires Alliance Auth 4.6+, django-esi 9.10+ (for compatibility dates),
django-eveuniverse, dhooks-lite, PyYAML.

## Setup

**1. Register a corporation.** Owners page → add. These are *corporation*
endpoints: the character needs the in-game **Station Manager** role, and an
executor's token does not reach member corps. Every corp holding hubs or
skyhooks registers its own.

**2. Add a Discord webhook** in Django admin (*Holdfast → Webhooks*), then pick
which alerts go where on each section's settings page.

**3. Grant permissions.** Three ladders; a higher rung implies the lower ones.

| Section | Member | Officer | Manager |
|---|---|---|---|
| Sovereignty | `sov_basic` | `sov_officer` | `sov_manage` |
| Skyhooks | `skyhook_basic` | `skyhook_officer` | `skyhook_manage` |
| Dens | `den_basic`, then `den_member` | `den_officer` | `den_manage` |

Plus `den_claim` to apply for a den site and `manage_owners` to register tokens.

Attach the three `*_basic` permissions to your **Member state** rather than a
group — they cover only the pages any member should have, and a state follows
membership so nobody has to be pruned later.

> **`den_claim` needs `den_member` beside it.** `den_claim` permits the action;
> the Den List page where the button lives is gated by `den_member`.

**Single alliance only.** Pages filter by alliance, but settings, thresholds and
Discord channels are one set per install and the alert checks sweep every owner.
Two alliances sharing an Auth would get each other's warnings. Run a second Auth.

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
anything, so the slot says *revoked, den still up* until it comes down.

## Alerts

![A siphon alert in Discord](https://raw.githubusercontent.com/yilifaer/aa-holdfast/main/docs/images/discord-alert.png)

Every moment appears three times: how far away it is, the reader's local time,
and EVE time. Discord renders the first two per viewer — which is why they are
in Chinese above and will not be for you — and the third is the same for
everyone.

Per-category routing, so fuel warnings and "something is being shot" can go to
different channels. Thresholds and switches live on each section's settings
page, not in `local.py`.

```bash
python manage.py holdfast_test_alerts             # one sample of every alert
python manage.py holdfast_test_webhook
python manage.py holdfast_update                  # sync now, in the foreground
python manage.py holdfast_import_dens dens.csv    # load a den census
```

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
the hour CCP caches these routes anyway. Structures awaiting a first detail pull
show as *queued*.

## Settings

All optional; most tuning belongs on the in-app settings pages instead.

```python
HOLDFAST_ESI_COMPATIBILITY_DATE = "2026-08-18"   # must be >= 2026-05-19
HOLDFAST_DETAIL_CALLS_PER_RUN = 60               # per owner, per run
HOLDFAST_DEN_DETAIL_CALLS_PER_RUN = 10
HOLDFAST_SKYHOOK_MIN_UNSECURED = 100             # fallback; see per-reagent bars
HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS = []
HOLDFAST_OWNER_SYNC_JITTER_SECONDS = 120
HOLDFAST_PLANET_RESOLVE_PER_RUN = 60
HOLDFAST_DEN_NOTIFICATION_MAX_AGE_HOURS = 24
HOLDFAST_DEN_FIRST_SYNC_GRACE_MINUTES = 90
HOLDFAST_RAIDABLE_CACHE_SECONDS = 300
HOLDFAST_STALE_PRUNE_DAYS = 14
```

## Notes

- These routes need an `X-Compatibility-Date` of 2026-05-19 or later. Without
  one ESI answers 404 rather than saying why, and the old
  `/latest/swagger.json` spec is gone — which is why so little is built on them.
  `django-esi` 9.10+ sends the header; this app pins its own date.
- A tactical operation's `dungeon_type_id` indexes dungeons, not inventory
  types, so it is shown as a number. Nothing names a dungeon.
- `aa-structures` parses skyhook *notifications* and builds timers from them. It
  does not read the structure routes, so it cannot see fuel, reagent stock or
  theft windows. The two complement each other.

## Development

```bash
python runtests.py          # sqlite, no Auth project needed
ruff check holdfast tests
```

[`docs/README.md`](https://github.com/yilifaer/aa-holdfast/blob/main/docs/README.md) covers the demo seeder, the screenshot
script, and `docs/mutate_structure.py`, which breaks each structural rule on
purpose to check the test guarding it actually fails.
