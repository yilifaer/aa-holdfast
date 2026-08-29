"""Fill the cleanroom with invented data, so the README can show the app working.

Screenshots of a sovereignty tool are intelligence. Fuel expiry says which hub
runs dry and when; the den board names the people running them. None of that
belongs in a public README, and blurring it leaves an image that is mostly
grey.

So the systems, the corporation and the characters here are fictional, in the
shapes the real ones take. Reagent type ids are real because they are public
game data and the names have to look right.

Run with:
    DJANGO_SETTINGS_MODULE=cleanauth.settings.local python seed_demo.py
"""

import random
from datetime import timedelta

import django

django.setup()

from allianceauth.authentication.models import CharacterOwnership  # noqa: E402
from allianceauth.eveonline.models import (  # noqa: E402
    EveAllianceInfo,
    EveCharacter,
    EveCorporationInfo,
)
from django.contrib.auth.models import Permission, User  # noqa: E402
from django.utils import timezone  # noqa: E402
from eveuniverse.models import (  # noqa: E402
    EveCategory,
    EveConstellation,
    EveGroup,
    EvePlanet,
    EveRegion,
    EveSolarSystem,
    EveType,
)

from holdfast.models import (  # noqa: E402
    DenCharacter,
    DenSlot,
    HoldfastConfig,
    MercenaryDen,
    Owner,
    PowerState,
    ReagentThreshold,
    Skyhook,
    SkyhookReagent,
    SkyhookState,
    SovCampaign,
    SovHub,
    SovHubReagent,
    SovHubUpgrade,
    SovSystem,
    SystemCostIndex,
)

random.seed(20260829)
now = timezone.now()

ALLIANCE_ID = 99000001
CORP_ID = 98000001

# Nullsec names follow a shape. These are in that shape and belong to nobody.
SYSTEMS = ["T7-QX1", "NF-8VD", "QR-2LM", "9BX-KZ", "HV-40P", "Z3-JYN", "KD-77S", "RL-6CE"]

MAGMATIC, SUPERIONIC = 81143, 81144
PLANET_TYPES = {"temperate": 11, "ice": 12, "lava": 2015, "barren": 2016}


def scaffold():
    """The eveuniverse chain each object hangs off."""
    category, _ = EveCategory.objects.get_or_create(
        id=9001, defaults={"name": "Demo", "published": True}
    )
    group, _ = EveGroup.objects.get_or_create(
        id=9001, defaults={"name": "Demo", "eve_category": category, "published": True}
    )
    region, _ = EveRegion.objects.get_or_create(id=19001, defaults={"name": "Vestigial Reach"})
    constellation, _ = EveConstellation.objects.get_or_create(
        id=29001, defaults={"name": "K-K7A9", "eve_region": region}
    )

    types = {}
    for type_id, name in (
        (MAGMATIC, "Magmatic Gas"),
        (SUPERIONIC, "Superionic Ice"),
        (11, "Planet (Temperate)"),
        (12, "Planet (Ice)"),
        (2015, "Planet (Lava)"),
        (2016, "Planet (Barren)"),
        (81622, "Sovereignty Hub"),
        (81658, "Orbital Skyhook"),
        (85230, "Mercenary Den"),
        (2871, "Advanced Logistics Network"),
        (2872, "Cynosural Navigation"),
    ):
        types[type_id], _ = EveType.objects.get_or_create(
            id=type_id,
            defaults={"name": name, "description": "", "eve_group": group, "published": True},
        )

    systems = {}
    for index, name in enumerate(SYSTEMS):
        systems[name], _ = EveSolarSystem.objects.get_or_create(
            id=30009000 + index,
            defaults={
                "name": name,
                "eve_constellation": constellation,
                "security_status": round(random.uniform(-0.45, -0.15), 3),
            },
        )
    return types, systems


def owner():
    alliance, _ = EveAllianceInfo.objects.get_or_create(
        alliance_id=ALLIANCE_ID,
        defaults={
            "alliance_name": "Example Alliance",
            "alliance_ticker": "EXA",
            "executor_corp_id": CORP_ID,
        },
    )
    corporation, _ = EveCorporationInfo.objects.get_or_create(
        corporation_id=CORP_ID,
        defaults={
            "corporation_name": "Example Holdings",
            "corporation_ticker": "EXH",
            "member_count": 84,
            "alliance": alliance,
        },
    )
    row, _ = Owner.objects.get_or_create(corporation=corporation, defaults={"is_enabled": True})
    row.last_sync_at = now - timedelta(minutes=4)
    row.last_sync_ok = True
    row.save()
    return row


def sov_hubs(row, types, systems):
    """Six hubs across the fuel bands, so the colours all appear."""
    plan = [
        ("T7-QX1", 0.6, 18_400, 3_100),    # critical
        ("NF-8VD", 2.4, 26_900, 9_800),    # danger
        ("QR-2LM", 5.1, 41_200, 12_400),   # warning
        ("9BX-KZ", 19.8, 88_000, 24_100),
        ("HV-40P", 31.2, 104_500, 30_600),
        ("Z3-JYN", 44.0, 96_300, 28_900),
    ]
    for index, (name, days, magmatic, superionic) in enumerate(plan):
        system = systems[name]
        hub, _ = SovHub.objects.update_or_create(
            hub_id=1_050_000_000_000 + index,
            defaults={
                "owner": row,
                "solar_system_id": system.id,
                "eve_solar_system": system,
                "power_available": random.randrange(30_000, 90_000, 500),
                "power_allocated": random.randrange(20_000, 60_000, 500),
                "workforce_available": random.randrange(8_000, 30_000, 100),
                "workforce_allocated": random.randrange(5_000, 20_000, 100),
                "fuel_expires_at": now + timedelta(days=days),
                "vulnerability_start": now + timedelta(hours=6 + index),
                "vulnerability_end": now + timedelta(hours=9 + index),
                "reagent_bay_updated_at": now - timedelta(minutes=12),
                "last_seen_at": now,
                "detail_updated_at": now - timedelta(minutes=random.randint(2, 50)),
            },
        )
        for type_id, amount in ((MAGMATIC, magmatic), (SUPERIONIC, superionic)):
            SovHubReagent.objects.update_or_create(
                hub=hub,
                type_id=type_id,
                defaults={
                    "eve_type": types[type_id],
                    "amount": amount,
                    "burning_per_hour": round(amount / max(days, 0.5) / 24, 2),
                },
            )
        for upgrade_id in (2871, 2872):
            SovHubUpgrade.objects.update_or_create(
                hub=hub,
                type_id=upgrade_id,
                defaults={
                    "eve_type": types[upgrade_id],
                    # One system short of power, to show the offline state.
                    "power_state": PowerState.LOW
                    if (name == "NF-8VD" and upgrade_id == 2872)
                    else PowerState.ONLINE,
                },
            )


def skyhooks(row, types, systems):
    """A mix that puts something in every column of every skyhook page."""
    plan = [
        # (system, roman, planet kind, workforce, reagent, unsecured, theft in hours)
        ("T7-QX1", "III", "lava", 9_240, MAGMATIC, 167_132, 0.0),
        ("T7-QX1", "VII", "ice", 8_100, SUPERIONIC, 11_466, 3.2),
        ("NF-8VD", "II", "lava", 7_820, MAGMATIC, 105_799, 6.8),
        ("NF-8VD", "V", "ice", 9_970, SUPERIONIC, 4_740, 14.1),
        ("QR-2LM", "IV", "lava", 8_450, MAGMATIC, 28_046, 21.5),
        ("9BX-KZ", "VI", "ice", 6_210, SUPERIONIC, 8_120, 30.0),
        ("HV-40P", "I", "lava", 9_150, MAGMATIC, 154_584, 40.4),
        ("Z3-JYN", "VIII", "ice", 3_970, SUPERIONIC, 1_520, 47.2),
        # Temperate planets: these become den slots. No reagents, so they never
        # appear on the theft pages -- which is the real behaviour.
        ("KD-77S", "IV", "temperate", 8_700, None, 0, None),
        ("KD-77S", "IX", "temperate", 8_316, None, 0, None),   # siphoned, measured
        ("RL-6CE", "III", "temperate", 8_280, None, 0, None),  # siphoned, inferred
        ("RL-6CE", "XI", "temperate", 9_450, None, 0, None),
    ]
    made = []
    for index, (system_name, roman, kind, workforce, reagent, unsecured, theft) in enumerate(plan):
        system = systems[system_name]
        planet_name = f"{system_name} {roman}"
        planet, _ = EvePlanet.objects.get_or_create(
            id=40_900_000 + index,
            defaults={
                "name": planet_name,
                "eve_solar_system": system,
                "eve_type": types[PLANET_TYPES[kind]],
            },
        )
        theft_start = now + timedelta(hours=theft) if theft is not None else None
        skyhook, _ = Skyhook.objects.update_or_create(
            skyhook_id=1_045_000_000_000 + index,
            defaults={
                "owner": row,
                "planet_id": planet.id,
                "eve_planet": planet,
                "eve_solar_system": system,
                "is_active": True,
                "effective_workforce": workforce,
                "state": SkyhookState.SHIELD_VULNERABLE,
                "theft_start": theft_start,
                "theft_end": theft_start + timedelta(hours=2) if theft_start else None,
                "last_seen_at": now,
                "detail_updated_at": now - timedelta(minutes=random.randint(2, 45)),
            },
        )
        if reagent:
            SkyhookReagent.objects.update_or_create(
                skyhook=skyhook,
                type_id=reagent,
                defaults={
                    "eve_type": types[reagent],
                    "secured_stock": random.randrange(2_000, 40_000, 100),
                    "unsecured_stock": unsecured,
                    "last_cycle": now - timedelta(hours=random.randint(1, 6)),
                },
            )
        made.append((planet_name, skyhook))

    # The two detectors, side by side. 8316 is not a multiple of ten, which no
    # undisturbed skyhook ever reports -- that one is measured. 8280 is round,
    # so only its own recorded peak of 9200 gives it away: inferred.
    measured = dict(made)["KD-77S IX"]
    measured.workforce_siphon_percent = 10.0
    measured.workforce_base = 9_240
    measured.workforce_high_water = 9_240
    measured.save()

    inferred = dict(made)["RL-6CE III"]
    inferred.workforce_high_water = 9_200
    inferred.workforce_dropped_at = now - timedelta(hours=31)
    inferred.save()
    return dict(made)


def dens(built):
    """One den we can read, one recorded by hand, two slots free."""
    DenSlot.objects.all().delete()
    slots = {}
    for name in ("KD-77S IV", "KD-77S IX", "RL-6CE III", "RL-6CE XI"):
        slots[name] = DenSlot.objects.create(skyhook=built[name])

    user, _ = User.objects.get_or_create(
        username="demo.operator", defaults={"email": "demo@example.invalid"}
    )
    character, _ = EveCharacter.objects.get_or_create(
        character_id=91_000_001,
        defaults={
            "character_name": "Astrid Vale",
            "corporation_id": CORP_ID,
            "corporation_name": "Example Holdings",
            "corporation_ticker": "EXH",
            "alliance_id": ALLIANCE_ID,
            "alliance_name": "Example Alliance",
        },
    )
    ownership, _ = CharacterOwnership.objects.get_or_create(
        character=character, defaults={"user": user, "owner_hash": "demo-operator"}
    )
    den_character, _ = DenCharacter.objects.get_or_create(character_ownership=ownership)
    den_character.last_sync_at = now - timedelta(minutes=9)
    den_character.last_sync_ok = True
    den_character.timezone = "AUTZ"
    den_character.save()

    slot = slots["KD-77S IX"]
    MercenaryDen.objects.update_or_create(
        den_id=1_053_000_000_001,
        defaults={
            "den_character": den_character,
            "eve_character": character,
            "planet_id": slot.skyhook.planet_id,
            "eve_planet": slot.skyhook.eve_planet,
            "eve_solar_system": slot.skyhook.eve_solar_system,
            "skyhook_id": slot.skyhook.skyhook_id,
            "slot": slot,
            "state": MercenaryDen.State.RUNNING,
            "development_level": "Level4",
            "development_amount": 100,
            "anarchy_level": "Level2",
            "anarchy_amount": 45,
            "infomorphs": 170_000,
            "last_seen_at": now,
            "detail_updated_at": now - timedelta(minutes=9),
        },
    )

    hand = slots["RL-6CE III"]
    hand.recorded_den = True
    hand.recorded_owner_note = "Juno Reyes"
    hand.recorded_corporation_note = "Second Example Corp"
    hand.recorded_at = now - timedelta(days=3)
    hand.save()


def public_pages(types, systems):
    """The ADM board, the cost page, and one reinforced hub for the timer board."""
    for index, name in enumerate(SYSTEMS):
        system = systems[name]
        SovSystem.objects.update_or_create(
            solar_system_id=system.id,
            defaults={
                "eve_solar_system": system,
                "alliance_id": ALLIANCE_ID,
                "corporation_id": CORP_ID,
                "activity_defense_multiplier": round(random.uniform(1.4, 5.8), 2),
                "military_level": random.randint(0, 5),
                "industrial_level": random.randint(0, 5),
                "strategic_level": random.randint(0, 3),
                "vulnerability_start": now + timedelta(hours=5 + index),
                "vulnerability_end": now + timedelta(hours=8 + index),
                "updated_at": now,
            },
        )
        SystemCostIndex.objects.update_or_create(
            solar_system_id=system.id,
            defaults={
                "eve_solar_system": system,
                "alliance_id": ALLIANCE_ID,
                "manufacturing": round(random.uniform(0.0004, 0.041), 6),
                "reaction": round(random.uniform(0.0002, 0.028), 6),
                "copying": round(random.uniform(0.0001, 0.019), 6),
                "invention": round(random.uniform(0.0001, 0.014), 6),
                "researching_time_efficiency": round(random.uniform(0.0001, 0.011), 6),
                "researching_material_efficiency": round(random.uniform(0.0001, 0.011), 6),
                "updated_at": now,
            },
        )

    SovCampaign.objects.update_or_create(
        campaign_id=880001,
        defaults={
            "structure_id": 1_050_000_000_001,
            "solar_system_id": systems["NF-8VD"].id,
            "eve_solar_system": systems["NF-8VD"],
            "constellation_id": 29001,
            "event_type": SovCampaign.EventType.IHUB,
            "defender_id": ALLIANCE_ID,
            "defender_score": 0.42,
            "attackers_score": 0.58,
            "start_time": now - timedelta(minutes=25),
            "updated_at": now,
        },
    )


def viewer():
    """Somebody to look at all of it, holding every permission."""
    user, created = User.objects.get_or_create(
        username="demo", defaults={"email": "demo@example.invalid", "is_staff": True}
    )
    user.user_permissions.set(
        Permission.objects.filter(content_type__app_label="holdfast")
    )
    character, _ = EveCharacter.objects.get_or_create(
        character_id=91_000_002,
        defaults={
            "character_name": "Wren Calloway",
            "corporation_id": CORP_ID,
            "corporation_name": "Example Holdings",
            "corporation_ticker": "EXH",
            "alliance_id": ALLIANCE_ID,
            "alliance_name": "Example Alliance",
        },
    )
    CharacterOwnership.objects.get_or_create(
        character=character, defaults={"user": user, "owner_hash": "demo-viewer"}
    )
    profile = user.profile
    profile.main_character = character
    profile.save()
    return user, created


def main():
    types, systems = scaffold()
    row = owner()
    sov_hubs(row, types, systems)
    built = skyhooks(row, types, systems)
    dens(built)
    public_pages(types, systems)

    for type_id, bar in ((MAGMATIC, 100_000), (SUPERIONIC, 5_000)):
        ReagentThreshold.objects.update_or_create(
            type_id=type_id,
            defaults={"eve_type": types[type_id], "min_unsecured": bar, "is_enabled": True},
        )

    config = HoldfastConfig.get_solo()
    config.skyhook_theft_horizon_hours = 48
    config.save()

    user, created = viewer()
    print(f"seeded: {SovHub.objects.count()} hubs, {Skyhook.objects.count()} skyhooks, "
          f"{DenSlot.objects.count()} den slots, {SovSystem.objects.count()} systems")
    print(f"viewer: {user.username} ({'new' if created else 'existing'})")


if __name__ == "__main__":
    main()
