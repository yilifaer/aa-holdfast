"""Pick the tracked alliance from what is already registered.

Nobody should have to configure anything on upgrade, so this fills the column
0008 added from the earliest registered corporation that is in an alliance,
exactly as a fresh install's first registration would. Alliances already
listed in HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS are passed over: making one of
those home would then refuse the real one. An install with no registered
corporation is left unset, and its first registration decides.

Data only, no schema change, so a failure here leaves nothing half-applied
and ``migrate`` can simply be run again.

The loop below is a copy of ``core.scope.bootstrap_home_alliance``, which the
settings page still uses, and it stays a copy on purpose. A migration is
frozen and the code it would import is not: the next rename of that helper
must not break ``migrate`` on every fresh install.
"""

import logging

from django.conf import settings
from django.db import migrations

logger = logging.getLogger(__name__)


def _extra_alliance_ids():
    # A copy of core.scope.alliance_ids_from: an entry that is not a number
    # is skipped, not raised on. Before 0.1.9 the list was used uncast, so a
    # local.py with such an entry upgraded fine, and it still must.
    ids = set()
    for value in getattr(settings, "HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS", []):
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring %r in HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS: not an alliance id",
                value,
            )
    return ids


def bootstrap(apps, schema_editor):
    Owner = apps.get_model("holdfast", "Owner")
    HoldfastConfig = apps.get_model("holdfast", "HoldfastConfig")

    current = (
        HoldfastConfig.objects.filter(pk=1)
        .values_list("home_alliance_id", flat=True)
        .first()
    )
    if current is not None:
        return

    extras = _extra_alliance_ids()
    # The alliance most enabled owners are in; a tie goes to the one
    # registered first, by primary key rather than the model's alphabetical
    # ordering. Majority rather than earliest because an install may already
    # hold a foreign corporation among its own, and nobody knows which was
    # registered first. The corporation's alliance foreign key is read first;
    # when Auth has not filled that in yet the token character's alliance
    # stands in.
    owners = (
        Owner.objects.filter(is_enabled=True)
        .order_by("pk")
        .select_related("corporation__alliance", "character_ownership__character")
    )
    votes = {}  # alliance id -> [owners in it, pk of the first of them]
    for owner in owners:
        alliance = owner.corporation.alliance
        candidate = alliance.alliance_id if alliance is not None else None
        if candidate is None and owner.character_ownership is not None:
            candidate = owner.character_ownership.character.alliance_id
        if candidate is None or candidate in extras:
            continue
        votes.setdefault(candidate, [0, owner.pk])[0] += 1
    if not votes:
        # The row is only created when there is something to put in it; an
        # empty install keeps none until a settings page asks for one.
        return
    chosen = max(votes, key=lambda alliance_id: (votes[alliance_id][0], -votes[alliance_id][1]))
    config, _created = HoldfastConfig.objects.get_or_create(pk=1)
    config.home_alliance_id = chosen
    config.save(update_fields=["home_alliance_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("holdfast", "0008_home_alliance"),
        ("eveonline", "0025_remove_evecharacter_last_updated_and_more"),
        ("authentication", "0026_alter_characterownership_user_and_more"),
    ]

    operations = [
        migrations.RunPython(bootstrap, migrations.RunPython.noop),
    ]
