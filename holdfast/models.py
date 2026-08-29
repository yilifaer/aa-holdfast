"""Data model: sovereignty hubs, skyhooks, the public sov map, and dens."""

from datetime import timedelta

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils import timezone as timezone_module
from esi.models import Token
from eveuniverse.models import EvePlanet, EveSolarSystem, EveType

from .app_settings import HOLDFAST_ESI_SCOPES

# Mercenary dens can only be anchored beside a skyhook on a temperate planet.
# Match on the type ID rather than the name, which eveuniverse translates.
TEMPERATE_PLANET_TYPE_ID = 11

# The timezone bands EVE alliances actually organise around, not the 400-entry
# IANA list. Labelled the way fleet pings label them so nobody has to translate
# between "Australia/Adelaide" and "AUTZ", and wide enough to cover an
# international membership once this is public.
TIMEZONE_CHOICES = (
    ("AUTZ", "AUTZ - Australia / New Zealand (UTC+10)"),
    ("CNTZ", "CNTZ - China / East Asia (UTC+8)"),
    ("SEATZ", "SEATZ - Southeast Asia (UTC+7)"),
    ("RUTZ", "RUTZ - Russia / CIS (UTC+3)"),
    ("EUTZ", "EUTZ - Continental Europe (UTC+1)"),
    ("UKTZ", "UKTZ - UK / Ireland / Portugal (UTC+0)"),
    ("SATZ", "SATZ - South America (UTC-3)"),
    ("USETZ", "USETZ - US East (UTC-5)"),
    ("USCTZ", "USCTZ - US Central (UTC-6)"),
    ("USWTZ", "USWTZ - US West (UTC-8)"),
)
TIMEZONE_ZONES = {
    "AUTZ": "Australia/Sydney",
    "CNTZ": "Asia/Shanghai",
    "SEATZ": "Asia/Bangkok",
    "RUTZ": "Europe/Moscow",
    "EUTZ": "Europe/Berlin",
    "UKTZ": "Europe/London",
    "SATZ": "America/Sao_Paulo",
    "USETZ": "America/New_York",
    "USCTZ": "America/Chicago",
    "USWTZ": "America/Los_Angeles",
}


class General(models.Model):  # noqa: DJ008 -- never instantiated, see below
    """Permission anchor. Not a real table.

    Alliance Auth's convention for declaring an app's permissions: an unmanaged
    model with no fields, whose Meta carries the permission list. Nothing is
    ever instantiated, so it has no ``__str__`` to write.
    """

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            # --- Sovereignty ---
            ("sov_basic", "SOV: can see the system cost page"),
            ("sov_officer", "SOV: can see every sovereignty page"),
            ("sov_manage", "SOV: can change sovereignty settings"),
            # --- Skyhooks ---
            ("skyhook_basic", "Skyhook: can see the raid target page"),
            ("skyhook_officer", "Skyhook: can see every skyhook page"),
            ("skyhook_manage", "Skyhook: can change skyhook settings"),
            # --- Mercenary dens ---
            ("den_basic", "Den: can see their own dens and timers"),
            ("den_member", "Den: can also see the den list and their dashboard"),
            ("den_officer", "Den: can see every den page"),
            ("den_manage", "Den: can approve claims and change den settings"),
            ("den_claim", "Den: can apply for a den slot"),
            # --- Cross-cutting ---
            ("manage_owners", "Can register corporation and character tokens"),
        )


class Owner(models.Model):
    """A corporation whose hubs and skyhooks we pull, plus the token we use."""

    corporation = models.OneToOneField(
        EveCorporationInfo, on_delete=models.CASCADE, related_name="holdfast_owner"
    )
    character_ownership = models.ForeignKey(
        CharacterOwnership,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Character supplying the token. Needs the Station Manager role.",
    )
    is_enabled = models.BooleanField(default=True, db_index=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_ok = models.BooleanField(default=False)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["corporation__corporation_name"]

    def __str__(self) -> str:
        return str(self.corporation)

    @property
    def alliance_id(self):
        alliance = self.corporation.alliance
        return alliance.alliance_id if alliance else None

    def fetch_token(self) -> Token:
        """Return a valid token for this owner, or raise Token.DoesNotExist."""
        if not self.character_ownership:
            raise Token.DoesNotExist(f"{self}: no character registered")
        token = (
            Token.objects.filter(
                character_id=self.character_ownership.character.character_id
            )
            .require_scopes(HOLDFAST_ESI_SCOPES)
            .require_valid()
            .first()
        )
        if not token:
            raise Token.DoesNotExist(
                f"{self}: no valid token with the required scopes"
            )
        return token

    def mark_sync(self, ok: bool, error: str = "") -> None:
        self.last_sync_at = timezone.now()
        self.last_sync_ok = ok
        self.last_error = error[:2000]
        self.save(update_fields=["last_sync_at", "last_sync_ok", "last_error"])


class PowerState(models.TextChoices):
    UNSPECIFIED = "Unspecified", "Unspecified"
    ONLINE = "Online", "Online"
    OFFLINE = "Offline", "Offline"
    LOW = "Low", "Low (out of fuel, power or workforce)"
    PENDING = "Pending", "Pending (waiting on startup cost)"


class SkyhookState(models.TextChoices):
    UNSPECIFIED = "Unspecified", "Unspecified"
    SHIELD_VULNERABLE = "ShieldVulnerable", "Shield vulnerable"
    ARMOR_REINFORCED = "ArmorReinforced", "Armor reinforced"
    ARMOR_VULNERABLE = "ArmorVulnerable", "Armor vulnerable"
    HULL_REINFORCED = "HullReinforced", "Hull reinforced"
    HULL_VULNERABLE = "HullVulnerable", "Hull vulnerable"


class SovHub(models.Model):
    """One sovereignty hub, as reported by the corporation structures route."""

    hub_id = models.BigIntegerField(primary_key=True)
    owner = models.ForeignKey(
        Owner, on_delete=models.CASCADE, related_name="sov_hubs"
    )
    eve_solar_system = models.ForeignKey(
        EveSolarSystem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    solar_system_id = models.BigIntegerField(db_index=True)

    power_available = models.BigIntegerField(default=0)
    power_allocated = models.BigIntegerField(default=0)
    workforce_available = models.BigIntegerField(default=0)
    workforce_allocated = models.BigIntegerField(default=0)

    vulnerability_start = models.DateTimeField(null=True, blank=True)
    vulnerability_end = models.DateTimeField(null=True, blank=True)

    reagent_bay_updated_at = models.DateTimeField(null=True, blank=True)
    fuel_access_list_id = models.BigIntegerField(null=True, blank=True)
    workforce_transport = models.JSONField(default=dict, blank=True)

    # Denormalised from the reagent rows so the dashboard can sort in the DB.
    fuel_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # last_seen_at: the listing confirmed it still exists.
    # detail_updated_at: we actually spent a call pulling its details. Details
    # rotate under a per-run budget, so these two drift apart on purpose.
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    detail_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["fuel_expires_at"]
        verbose_name = "sovereignty hub"

    def __str__(self) -> str:
        return f"Sov Hub {self.system_name}"

    @property
    def system_name(self) -> str:
        if self.eve_solar_system:
            return self.eve_solar_system.name
        return str(self.solar_system_id)

    @property
    def has_details(self) -> bool:
        """False while a newly discovered hub waits its turn in the rotation."""
        return self.detail_updated_at is not None

    @property
    def hours_of_fuel_left(self):
        """Hours until the first reagent runs dry, measured from now."""
        if not self.fuel_expires_at:
            return None
        return (self.fuel_expires_at - timezone.now()).total_seconds() / 3600

    @property
    def is_fuel_critical(self) -> bool:
        hours = self.hours_of_fuel_left
        return hours is not None and hours < 24

    @property
    def power_free(self) -> int:
        return self.power_available - self.power_allocated

    @property
    def workforce_free(self) -> int:
        return self.workforce_available - self.workforce_allocated

    @property
    def is_overallocated(self) -> bool:
        return self.power_free < 0 or self.workforce_free < 0

    def recalculate_fuel_expiry(self) -> None:
        """Set ``fuel_expires_at`` to the earliest reagent depletion time.

        Reagent amounts are a snapshot taken at ``reagent_bay_updated_at``, not
        at request time, so the countdown has to start from that timestamp.
        """
        base = self.reagent_bay_updated_at or self.last_seen_at
        soonest = None
        for reagent in self.reagents.all():
            if reagent.burning_per_hour <= 0:
                continue
            expiry = base + timedelta(
                hours=reagent.amount / reagent.burning_per_hour
            )
            if soonest is None or expiry < soonest:
                soonest = expiry
        self.fuel_expires_at = soonest
        self.save(update_fields=["fuel_expires_at"])


class SovHubReagent(models.Model):
    hub = models.ForeignKey(
        SovHub, on_delete=models.CASCADE, related_name="reagents"
    )
    type_id = models.BigIntegerField()
    eve_type = models.ForeignKey(
        EveType, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    amount = models.BigIntegerField(default=0)
    burning_per_hour = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["hub", "type_id"], name="holdfast_hub_reagent_unique"
            )
        ]
        ordering = ["type_id"]

    def __str__(self) -> str:
        return f"{self.type_name} x{self.amount}"

    @property
    def type_name(self) -> str:
        return self.eve_type.name if self.eve_type else str(self.type_id)

    @property
    def hours_left(self):
        if self.burning_per_hour <= 0:
            return None
        base = self.hub.reagent_bay_updated_at or self.hub.last_seen_at
        elapsed = (timezone.now() - base).total_seconds() / 3600
        return max(self.amount / self.burning_per_hour - elapsed, 0)


class SovHubUpgrade(models.Model):
    hub = models.ForeignKey(
        SovHub, on_delete=models.CASCADE, related_name="upgrades"
    )
    type_id = models.BigIntegerField()
    eve_type = models.ForeignKey(
        EveType, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    power_state = models.CharField(
        max_length=16, choices=PowerState.choices, default=PowerState.UNSPECIFIED
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["hub", "type_id"], name="holdfast_hub_upgrade_unique"
            )
        ]
        ordering = ["type_id"]

    def __str__(self) -> str:
        return f"{self.type_name} ({self.power_state})"

    @property
    def type_name(self) -> str:
        return self.eve_type.name if self.eve_type else str(self.type_id)

    @property
    def is_starved(self) -> bool:
        return self.power_state == PowerState.LOW


class Skyhook(models.Model):
    skyhook_id = models.BigIntegerField(primary_key=True)
    owner = models.ForeignKey(
        Owner, on_delete=models.CASCADE, related_name="skyhooks"
    )
    planet_id = models.BigIntegerField(db_index=True)
    eve_planet = models.ForeignKey(
        EvePlanet, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    eve_solar_system = models.ForeignKey(
        EveSolarSystem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    is_active = models.BooleanField(default=False)
    effective_workforce = models.BigIntegerField(null=True, blank=True)
    state = models.CharField(
        max_length=20, choices=SkyhookState.choices, default=SkyhookState.UNSPECIFIED
    )

    theft_start = models.DateTimeField(null=True, blank=True, db_index=True)
    theft_end = models.DateTimeField(null=True, blank=True)
    reinforce_end = models.DateTimeField(null=True, blank=True, db_index=True)

    # A mercenary den at anarchy level 2 or above siphons the workforce output
    # of the skyhook it is attached to. ESI exposes no way to see someone
    # else's den, so a sustained drop below this skyhook's own historic peak is
    # the only automatic signal that one is sitting on it.
    workforce_high_water = models.BigIntegerField(null=True, blank=True)
    workforce_dropped_at = models.DateTimeField(null=True, blank=True)

    # A skyhook's own workforce output is always a round multiple of ten. A den
    # takes a flat percentage of it, and a percentage of a number that is not a
    # multiple of a hundred lands somewhere that is not a multiple of ten -- so
    # an un-round figure is a fingerprint of siphoning that needs no history to
    # read. That matters because a hook already being siphoned the first time we
    # ever see it makes its own peak useless as a baseline.
    workforce_siphon_percent = models.FloatField(null=True, blank=True, db_index=True)
    workforce_base = models.BigIntegerField(null=True, blank=True)

    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    detail_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["theft_start"]

    def __str__(self) -> str:
        return f"Skyhook {self.planet_name}"

    @property
    def planet_name(self) -> str:
        return self.eve_planet.name if self.eve_planet else str(self.planet_id)

    @property
    def system_name(self) -> str:
        return self.eve_solar_system.name if self.eve_solar_system else "?"

    @property
    def has_details(self) -> bool:
        """False while a newly discovered skyhook waits its turn."""
        return self.detail_updated_at is not None

    @property
    def total_unsecured(self) -> int:
        return sum(r.unsecured_stock for r in self.reagents.all())

    @property
    def total_secured(self) -> int:
        return sum(r.secured_stock for r in self.reagents.all())

    @property
    def is_theft_window_open(self) -> bool:
        now = timezone.now()
        if not self.theft_start or not self.theft_end:
            return False
        return self.theft_start <= now < self.theft_end

    @property
    def is_under_attack(self) -> bool:
        return self.state not in (
            SkyhookState.UNSPECIFIED,
            SkyhookState.SHIELD_VULNERABLE,
        )

    @property
    def is_temperate(self) -> bool:
        """Only temperate planets can host a mercenary den next to a skyhook."""
        return bool(self.eve_planet and self.eve_planet.eve_type_id == TEMPERATE_PLANET_TYPE_ID)

    @property
    def is_siphoned(self) -> bool:
        return self.workforce_siphon_percent is not None

    @property
    def siphoned_amount(self):
        if self.workforce_base is None or self.effective_workforce is None:
            return None
        return self.workforce_base - self.effective_workforce

    @property
    def siphon_estimate(self):
        """How much is being taken, and how well we know it.

        Two mechanisms answer this, and they disagree about certainty rather
        than about the number. The fingerprint is arithmetic -- an untouched
        skyhook cannot report an un-round figure -- so ``measured`` is as
        certain as anything here gets. Falling exactly to 90% of a peak we
        recorded ourselves is strong but rests on that peak having been clean,
        so it is ``inferred``. A drop that no rate explains is ``suspected``:
        something is wrong, we cannot say it is a den.

        Returns ``(percent, amount, certainty)``, all ``None`` when nothing is
        being taken.
        """
        from .core.siphon import infer_from_peak

        if self.workforce_siphon_percent:
            return self.workforce_siphon_percent, self.siphoned_amount, "measured"
        if not self.workforce_dropped_at:
            return None, None, None
        percent, base = infer_from_peak(
            self.effective_workforce, self.workforce_high_water
        )
        if percent:
            return percent, base - self.effective_workforce, "inferred"
        return (
            self.workforce_shortfall_percent,
            self.workforce_shortfall,
            "suspected",
        )

    @property
    def is_siphon_suspected(self) -> bool:
        """True when either mechanism has something to say."""
        return self.siphon_estimate[0] is not None

    @property
    def workforce_shortfall(self):
        """How far below its own peak this skyhook's workforce is sitting."""
        if self.workforce_high_water is None or self.effective_workforce is None:
            return None
        return self.workforce_high_water - self.effective_workforce

    @property
    def workforce_shortfall_percent(self):
        shortfall = self.workforce_shortfall
        if not shortfall or not self.workforce_high_water:
            return None
        return shortfall / self.workforce_high_water * 100


class SkyhookReagent(models.Model):
    skyhook = models.ForeignKey(
        Skyhook, on_delete=models.CASCADE, related_name="reagents"
    )
    type_id = models.BigIntegerField()
    eve_type = models.ForeignKey(
        EveType, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    secured_stock = models.BigIntegerField(default=0)
    unsecured_stock = models.BigIntegerField(default=0)
    last_cycle = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["skyhook", "type_id"], name="holdfast_skyhook_reagent_unique"
            )
        ]
        ordering = ["type_id"]

    def __str__(self) -> str:
        return f"{self.type_name}: {self.unsecured_stock} unsecured"

    @property
    def type_name(self) -> str:
        return self.eve_type.name if self.eve_type else str(self.type_id)


class ReagentThreshold(models.Model):
    """Per-reagent bar for skyhook theft alerts, editable in Django admin.

    Reagents are not worth the same trip: a hauler will cross the region for
    Magmatic Gas and shrug at the same number of units of something cheap. So
    each reagent gets its own bar rather than one global number. A reagent with
    no row here falls back to ``HOLDFAST_SKYHOOK_MIN_UNSECURED``.
    """

    type_id = models.BigIntegerField(unique=True)
    eve_type = models.ForeignKey(
        EveType, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    min_unsecured = models.BigIntegerField(
        default=100,
        help_text=(
            "Alert only once a skyhook holds at least this many unsecured units "
            "of this reagent."
        ),
    )
    is_enabled = models.BooleanField(
        default=True, help_text="Uncheck to never alert on this reagent."
    )

    class Meta:
        ordering = ["eve_type__name", "type_id"]
        verbose_name = "reagent alert threshold"

    def __str__(self) -> str:
        return f"{self.type_name}: {self.min_unsecured:,}"

    @property
    def type_name(self) -> str:
        return self.eve_type.name if self.eve_type else str(self.type_id)


class SovSystem(models.Model):
    """A row from the public /sovereignty/systems route.

    Only systems held by a tracked alliance are kept -- storing all 5485 would
    churn the SD card for no gain.
    """

    solar_system_id = models.BigIntegerField(primary_key=True)
    eve_solar_system = models.ForeignKey(
        EveSolarSystem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    alliance_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    corporation_id = models.BigIntegerField(null=True, blank=True)
    claimed_since = models.DateTimeField(null=True, blank=True)
    is_capital_system = models.BooleanField(default=False)

    activity_defense_multiplier = models.FloatField(
        null=True, blank=True, db_index=True
    )
    military_level = models.IntegerField(null=True, blank=True)
    industrial_level = models.IntegerField(null=True, blank=True)
    strategic_level = models.IntegerField(null=True, blank=True)

    hub_id = models.BigIntegerField(null=True, blank=True)
    vulnerability_start = models.DateTimeField(null=True, blank=True)
    vulnerability_end = models.DateTimeField(null=True, blank=True)

    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["activity_defense_multiplier"]
        verbose_name = "sovereignty system"

    def __str__(self) -> str:
        return self.system_name

    @property
    def system_name(self) -> str:
        if self.eve_solar_system:
            return self.eve_solar_system.name
        return str(self.solar_system_id)


class Webhook(models.Model):
    """Discord webhook to post alerts to."""

    class PingType(models.TextChoices):
        NONE = "NN", "no ping"
        HERE = "HE", "@here"
        EVERYONE = "EV", "@everyone"

    name = models.CharField(max_length=100, unique=True)
    url = models.CharField(
        max_length=500,
        help_text="Discord webhook URL, from Channel Settings > Integrations",
    )
    is_enabled = models.BooleanField(default=True)
    ping_type = models.CharField(
        max_length=2, choices=PingType.choices, default=PingType.NONE
    )
    notes = models.TextField(blank=True)

    def __str__(self) -> str:
        return self.name


class AlertLog(models.Model):
    """One row per alert already sent, so we don't repeat the same warning."""

    key = models.CharField(max_length=255, unique=True)
    sent_at = models.DateTimeField(default=timezone.now, db_index=True)

    def __str__(self) -> str:
        return self.key


# ==========================================================================
# Runtime configuration
# ==========================================================================


class HoldfastConfig(models.Model):
    """One row of numbers operators tune without a deploy.

    Fuel bands drive the dashboard colours *and* the Discord alerts from the
    same values. Keeping them separate was how you ended up with a board that
    had been amber for five days before anything pinged.
    """

    fuel_warning_days = models.FloatField(
        default=7, help_text="Amber below this many days of fuel left."
    )
    fuel_danger_days = models.FloatField(
        default=3, help_text="Red below this many days."
    )
    fuel_critical_days = models.FloatField(
        default=1, help_text="Critical below this many days."
    )

    den_anchor_grace_days = models.IntegerField(
        default=7,
        help_text=(
            "Flag an approved den claim that still has no anchored den after "
            "this many days. Never revokes anything by itself."
        ),
    )

    workforce_drop_percent = models.FloatField(
        default=5,
        help_text=(
            "Flag a skyhook whose workforce sits this many percent below its "
            "own historic peak -- the fingerprint of a hostile den siphoning it."
        ),
    )
    workforce_drop_grace_hours = models.IntegerField(
        default=24,
        help_text="Only alert once the drop has persisted this long.",
    )

    # --- sovereignty ---
    adm_alert_threshold = models.FloatField(
        default=3.0,
        null=True,
        blank=True,
        help_text="Warn when a system's ADM falls below this. Blank disables it.",
    )
    notify_upgrade_offline = models.BooleanField(
        default=True,
        help_text="Warn when a sovereignty upgrade drops out for lack of fuel, "
        "power or workforce.",
    )
    notify_upgrade_den_caused = models.BooleanField(
        default=True,
        help_text="Call it out separately when a den siphoning workforce in the "
        "same system is the likely reason an upgrade went down.",
    )
    sov_discord_enabled = models.BooleanField(
        default=True, help_text="Post sovereignty alerts to Discord."
    )

    # --- skyhooks ---
    # How far ahead the dashboard looks for theft windows. Also drives the
    # sidebar badge, so the number on the menu and the rows on the page always
    # describe the same stretch of time.
    skyhook_theft_horizon_hours = models.IntegerField(
        default=24,
        help_text="How far ahead to list theft windows, in hours.",
    )
    skyhook_theft_lead_minutes = models.IntegerField(
        default=45,
        help_text="How long before a theft window opens to warn.",
    )
    skyhook_discord_enabled = models.BooleanField(
        default=True, help_text="Post skyhook alerts to Discord."
    )

    # --- dens ---
    notify_den_skyhook_impact = models.BooleanField(
        default=True,
        help_text="Warn when a den starts siphoning one of our skyhooks.",
    )
    notify_den_sov_impact = models.BooleanField(
        default=True,
        help_text="Warn when a den's siphoning is severe enough to knock a "
        "sovereignty upgrade offline.",
    )
    den_discord_enabled = models.BooleanField(
        default=True, help_text="Post den alerts to Discord."
    )

    class Meta:
        verbose_name = "configuration"
        verbose_name_plural = "configuration"

    def __str__(self) -> str:
        return "SOV Monitor configuration"

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls) -> "HoldfastConfig":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def fuel_bands(self):
        """Descending (days, severity) pairs, widest band first."""
        return [
            (self.fuel_warning_days, "warning"),
            (self.fuel_danger_days, "danger"),
            (self.fuel_critical_days, "critical"),
        ]

    def fuel_severity(self, hours_left):
        """Worst band a given hours-of-fuel-left figure falls into."""
        if hours_left is None:
            return None
        days = hours_left / 24
        if days <= self.fuel_critical_days:
            return "critical"
        if days <= self.fuel_danger_days:
            return "danger"
        if days <= self.fuel_warning_days:
            return "warning"
        return None


# ==========================================================================
# Mercenary dens
# ==========================================================================


class EvolutionLevel(models.TextChoices):
    UNSPECIFIED = "Unspecified", "Unspecified"
    L0 = "Level0", "Level 0"
    L1 = "Level1", "Level 1"
    L2 = "Level2", "Level 2"
    L3 = "Level3", "Level 3"
    L4 = "Level4", "Level 4"


LEVEL_NUMBERS = {
    EvolutionLevel.L0: 0,
    EvolutionLevel.L1: 1,
    EvolutionLevel.L2: 2,
    EvolutionLevel.L3: 3,
    EvolutionLevel.L4: 4,
}


class DenCharacter(models.Model):
    """A character whose mercenary dens we pull.

    Den routes are character scoped -- there is no corporation or public
    equivalent -- so every den operator registers their own token.
    """

    character_ownership = models.OneToOneField(
        CharacterOwnership,
        on_delete=models.CASCADE,
        related_name="holdfast_den_character",
    )
    is_enabled = models.BooleanField(default=True, db_index=True)

    # Alliance Auth has no per-user timezone of its own, so operators pick one
    # here. Sourced from the standard library rather than the community
    # `timezones` app, which not every install has.
    timezone = models.CharField(
        max_length=8,
        blank=True,
        choices=TIMEZONE_CHOICES,
        help_text="Roughly when you are awake. Shown to den managers so they "
        "know when they can reach you.",
    )
    contact_note = models.CharField(
        max_length=200,
        blank=True,
        help_text="Anything else a den manager should know to reach you.",
    )

    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_ok = models.BooleanField(default=False)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["character_ownership__character__character_name"]
        verbose_name = "den character"

    def __str__(self) -> str:
        return str(self.character_ownership.character.character_name)

    @property
    def character(self):
        return self.character_ownership.character

    @property
    def user(self):
        return self.character_ownership.user

    @property
    def discord_name(self):
        """Discord handle, if this install runs a Discord service at all."""
        try:
            return self.user.discord.username
        except Exception:  # noqa: BLE001 - no discord app, or not linked
            return None

    @property
    def local_time(self):
        """Current time where this operator is, when they have said where."""
        zone = TIMEZONE_ZONES.get(self.timezone)
        if not zone:
            return None
        try:
            from zoneinfo import ZoneInfo

            return timezone.now().astimezone(ZoneInfo(zone))
        except Exception:  # noqa: BLE001 - a bad name must not break the page
            return None

    def fetch_token(self) -> Token:
        from .app_settings import HOLDFAST_DEN_ESI_SCOPES

        token = (
            Token.objects.filter(character_id=self.character.character_id)
            .require_scopes(HOLDFAST_DEN_ESI_SCOPES)
            .require_valid()
            .first()
        )
        if not token:
            raise Token.DoesNotExist(f"{self}: no valid token with den scopes")
        return token

    def mark_sync(self, ok: bool, error: str = "") -> None:
        self.last_sync_at = timezone.now()
        self.last_sync_ok = ok
        self.last_error = error[:2000]
        self.save(update_fields=["last_sync_at", "last_sync_ok", "last_error"])


class DenSlot(models.Model):
    """A temperate-planet skyhook of ours: one den-sized piece of ground.

    Slots are created automatically from synced skyhooks; nobody adds them by
    hand. What people do add by hand is the record of a den we cannot see --
    ESI exposes only your own dens, so a hostile one is invisible except as a
    workforce shortfall.
    """

    class Status(models.TextChoices):
        FREE = "free", "Free"
        PENDING = "pending", "Claim pending"
        ASSIGNED = "assigned", "Assigned, not yet anchored"
        ANCHORED = "anchored", "Den anchored"
        RECORDED = "recorded", "Den recorded by hand"
        HOSTILE = "hostile", "Hostile den"

    skyhook = models.OneToOneField(
        Skyhook, on_delete=models.CASCADE, related_name="den_slot"
    )

    # A den ESI will not show us. That is the normal case for a hostile den,
    # but it is just as true of a friendly one whose operator has not
    # registered a token here -- an alliance can know perfectly well who is
    # running a den without being able to read it. Both are recorded the same
    # way and told apart by ``recorded_hostile``.
    recorded_den = models.BooleanField(
        default=False,
        help_text="A den is sitting here that ESI does not show us.",
    )
    recorded_hostile = models.BooleanField(
        default=False,
        help_text="The recorded den belongs to someone outside the alliance.",
    )
    recorded_owner_note = models.CharField(
        max_length=200,
        blank=True,
        help_text="Who is running it, as far as anyone knows.",
    )
    recorded_corporation_note = models.CharField(
        max_length=200,
        blank=True,
        help_text="Their corporation, if known.",
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    recorded_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["skyhook__eve_planet__name"]
        verbose_name = "den slot"

    def __str__(self) -> str:
        return f"Den slot at {self.skyhook.planet_name}"

    @property
    def planet_name(self) -> str:
        return self.skyhook.planet_name

    @property
    def system_name(self) -> str:
        return self.skyhook.system_name

    @property
    def anchored_den(self):
        dens = list(self.dens.all())
        for den in dens:
            if den.state in (MercenaryDen.State.RUNNING, MercenaryDen.State.PAUSED):
                return den
        return dens[0] if dens else None

    @property
    def approved_claim(self):
        return self.claims.filter(status=DenClaim.Status.APPROVED).first()

    @property
    def pending_claims(self):
        return self.claims.filter(status=DenClaim.Status.PENDING)

    @property
    def status(self) -> str:
        # A den we can actually read outranks a note somebody typed: if an
        # operator has since registered a token, the hand record is stale and
        # the live one is the truth.
        if self.anchored_den:
            return self.Status.ANCHORED
        if self.recorded_den:
            return self.Status.HOSTILE if self.recorded_hostile else self.Status.RECORDED
        if self.approved_claim:
            return self.Status.ASSIGNED
        if self.pending_claims.exists():
            return self.Status.PENDING
        return self.Status.FREE

    @property
    def status_label(self) -> str:
        return dict(self.Status.choices).get(self.status, self.status)

    @property
    def is_claimable(self) -> bool:
        return self.status in (self.Status.FREE, self.Status.PENDING)

    @property
    def holder_name(self):
        """Who is running the den here, however we happen to know it."""
        den = self.anchored_den
        if den and den.eve_character:
            return den.eve_character.character_name
        claim = self.approved_claim
        if claim:
            return claim.character.character_name
        if self.recorded_den:
            unknown = "unknown (hostile)" if self.recorded_hostile else "unknown"
            return self.recorded_owner_note or unknown
        return None

    @property
    def holder_corporation(self):
        """Corporation of whoever holds it, resolved from the character."""
        den = self.anchored_den
        if den and den.eve_character:
            return den.eve_character.corporation_name
        claim = self.approved_claim
        if claim:
            return claim.character.corporation_name
        if self.recorded_den:
            return self.recorded_corporation_note or None
        return None

    @property
    def holder_source(self) -> str:
        """Where the name came from: a token, a person, or nowhere."""
        if self.anchored_den:
            return "auto"
        if self.approved_claim:
            return "approved"
        if self.recorded_den:
            return "manual"
        return ""

    @property
    def holder_label(self) -> str:
        """"name -- corporation (auto)", for the pings and the boards.

        One string, built in one place, so an alert and a table cannot end up
        describing the same slot differently. How we know matters as much as
        who it is: a token keeps itself current, a hand record does not.
        """
        name = self.holder_name
        if not name:
            return "unknown"
        corporation = self.holder_corporation
        source = self.holder_source
        label = f"{name} -- {corporation}" if corporation else name
        return f"{label} ({source})" if source else label

    @property
    def awaiting_removal(self) -> bool:
        """Approval was taken back but the den is still standing.

        Revoking a claim does not unanchor anything -- only the operator can
        do that -- so the slot has to keep saying so until they have.
        """
        if not self.anchored_den or self.approved_claim:
            return False
        return self.claims.filter(status=DenClaim.Status.REVOKED).exists()

    @property
    def is_overdue(self) -> bool:
        """Approved, but nothing anchored inside the grace period."""
        claim = self.approved_claim
        if not claim or self.anchored_den or not claim.decided_at:
            return False
        grace = HoldfastConfig.get_solo().den_anchor_grace_days
        return timezone.now() - claim.decided_at > timedelta(days=grace)


class DenClaim(models.Model):
    """An application to run a den on one slot, and its decision."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"
        # Granted and then taken back. Distinct from rejected, which was never
        # granted: a revoked claim usually means a den has to come down.
        REVOKED = "revoked", "Revoked"

    slot = models.ForeignKey(DenSlot, on_delete=models.CASCADE, related_name="claims")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="holdfast_den_claims",
    )
    character = models.ForeignKey(
        EveCharacter, on_delete=models.CASCADE, related_name="+"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    note = models.TextField(blank=True, help_text="Why you want it.")

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    decision_note = models.TextField(blank=True)

    # A decided claim stays on the books for whoever has to review the history
    # later, but the applicant should not have to look at "rejected" forever.
    # Dismissing hides it from their own page and nowhere else.
    dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "den claim"

    def __str__(self) -> str:
        return f"{self.character} -> {self.slot.planet_name} ({self.status})"

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.PENDING

    @property
    def is_dismissable(self) -> bool:
        """A decision the applicant can clear off their own page."""
        return self.status != self.Status.PENDING and self.dismissed_at is None


class MercenaryDen(models.Model):
    """One den, pulled from its own owner's token."""

    class State(models.TextChoices):
        UNSPECIFIED = "Unspecified", "Unspecified"
        RUNNING = "Running", "Running"
        PAUSED = "Paused", "Paused (reinforced)"
        DISABLED = "Disabled", "Disabled (owner lost the skill)"

    den_id = models.BigIntegerField(primary_key=True)
    den_character = models.ForeignKey(
        DenCharacter, on_delete=models.CASCADE, related_name="dens"
    )
    eve_character = models.ForeignKey(
        EveCharacter, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    type_id = models.BigIntegerField(null=True, blank=True)

    planet_id = models.BigIntegerField(db_index=True)
    eve_planet = models.ForeignKey(
        EvePlanet, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    eve_solar_system = models.ForeignKey(
        EveSolarSystem, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    # The den detail response names the skyhook it is attached to, including
    # which corporation owns that skyhook -- so a den sitting on someone
    # else's ground is obvious without any guessing.
    skyhook_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    skyhook_corporation_id = models.BigIntegerField(null=True, blank=True)
    slot = models.ForeignKey(
        DenSlot, on_delete=models.SET_NULL, null=True, blank=True, related_name="dens"
    )

    state = models.CharField(
        max_length=12, choices=State.choices, default=State.UNSPECIFIED, db_index=True
    )
    development_level = models.CharField(
        max_length=12, choices=EvolutionLevel.choices, default=EvolutionLevel.UNSPECIFIED
    )
    development_amount = models.BigIntegerField(default=0)
    anarchy_level = models.CharField(
        max_length=12, choices=EvolutionLevel.choices, default=EvolutionLevel.UNSPECIFIED
    )
    anarchy_amount = models.BigIntegerField(default=0)
    infomorphs = models.BigIntegerField(default=0)

    reinforce_end = models.DateTimeField(null=True, blank=True)

    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    detail_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["eve_planet__name"]
        verbose_name = "mercenary den"

    def __str__(self) -> str:
        return f"Den at {self.planet_name}"

    @property
    def planet_name(self) -> str:
        return self.eve_planet.name if self.eve_planet else str(self.planet_id)

    @property
    def system_name(self) -> str:
        return self.eve_solar_system.name if self.eve_solar_system else "?"

    @property
    def anarchy_number(self) -> int:
        return LEVEL_NUMBERS.get(self.anarchy_level, 0)

    @property
    def development_number(self) -> int:
        return LEVEL_NUMBERS.get(self.development_level, 0)

    @property
    def is_siphoning(self) -> bool:
        """From anarchy level 2 a den starts stealing its skyhook's workforce."""
        return self.anarchy_number >= 2

    @property
    def is_on_our_ground(self) -> bool:
        return self.slot_id is not None

    @property
    def has_details(self) -> bool:
        return self.detail_updated_at is not None


class MercenaryTacticalOperation(models.Model):
    """An MTO offered at one of our dens.

    Worth pulling on its own route rather than waiting for the in-game
    notification: this endpoint carries the state machine and the expiry.
    """

    class State(models.TextChoices):
        UNSPECIFIED = "Unspecified", "Unspecified"
        AVAILABLE = "Available", "Available"
        STARTED = "Started", "Started"
        COMPLETED = "Completed", "Completed"
        EXPIRED = "Expired", "Expired"
        REMOVED = "Removed", "Removed"

    operation_id = models.CharField(max_length=128, primary_key=True)
    den_character = models.ForeignKey(
        DenCharacter, on_delete=models.CASCADE, related_name="operations"
    )
    den = models.ForeignKey(
        MercenaryDen,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operations",
    )
    mercenary_den_id = models.BigIntegerField(db_index=True)

    dungeon_type_id = models.BigIntegerField(null=True, blank=True)
    state = models.CharField(
        max_length=12, choices=State.choices, default=State.UNSPECIFIED, db_index=True
    )
    expires = models.DateTimeField(null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["expires"]
        verbose_name = "mercenary tactical operation"

    def __str__(self) -> str:
        return f"MTO {self.operation_id} ({self.state})"

    @property
    def type_name(self) -> str:
        """What kind of operation this is, as far as ESI will say.

        ``dungeon_type_id`` indexes dungeons, not inventory types, so looking
        it up in the type tables returns whatever unrelated item happens to
        share the number -- 12367 comes back as the skill "Explosive Shield
        Compensation". There is no route that names a dungeon, so the number
        is reported as a number rather than dressed up as a wrong name.
        """
        if self.dungeon_type_id is None:
            return "unknown operation"
        return f"Operation #{self.dungeon_type_id}"

    @property
    def is_actionable(self) -> bool:
        return self.state in (self.State.AVAILABLE, self.State.STARTED)


class DenEvent(models.Model):
    """A den notification pulled from a character's in-game notification list.

    Notifications live server-side, so being logged out loses nothing. They
    are only needed for the one thing the dedicated routes cannot show: a den
    being shot right now that is not reinforced yet.
    """

    class Kind(models.TextChoices):
        ATTACKED = "MercenaryDenAttacked", "Under attack"
        REINFORCED = "MercenaryDenReinforced", "Reinforced"
        NEW_MTO = "MercenaryDenNewMTO", "New tactical operation"

    notification_id = models.BigIntegerField(primary_key=True)
    den_character = models.ForeignKey(
        DenCharacter, on_delete=models.CASCADE, related_name="events"
    )
    den = models.ForeignKey(
        MercenaryDen,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)
    timestamp = models.DateTimeField(db_index=True)
    text = models.TextField(blank=True)
    is_alerted = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name = "den event"

    def __str__(self) -> str:
        return f"{self.get_kind_display()} @ {self.timestamp:%Y-%m-%d %H:%M}"


class SystemCostIndex(models.Model):
    """Industry cost indices for one solar system.

    ``/industry/systems`` is public, unauthenticated, cached an hour, and
    returns every system in New Eden in a single call -- so the cost of keeping
    this current does not grow with how much space an alliance holds. Only the
    systems a tracked alliance actually owns are stored, which keeps the table
    proportional to the alliance rather than to the cluster.
    """

    solar_system_id = models.BigIntegerField(primary_key=True)
    eve_solar_system = models.ForeignKey(
        EveSolarSystem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    alliance_id = models.BigIntegerField(null=True, blank=True, db_index=True)

    manufacturing = models.FloatField(null=True, blank=True, db_index=True)
    reaction = models.FloatField(null=True, blank=True, db_index=True)
    copying = models.FloatField(null=True, blank=True)
    invention = models.FloatField(null=True, blank=True)
    researching_time_efficiency = models.FloatField(null=True, blank=True)
    researching_material_efficiency = models.FloatField(null=True, blank=True)

    # Anything CCP adds later lands here rather than needing a migration.
    other_indices = models.JSONField(default=dict, blank=True)

    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["manufacturing"]
        verbose_name = "system cost index"
        verbose_name_plural = "system cost indices"

    def __str__(self) -> str:
        return f"{self.system_name} cost indices"

    @property
    def system_name(self) -> str:
        if self.eve_solar_system:
            return self.eve_solar_system.name
        return str(self.solar_system_id)

    @property
    def region_name(self):
        if not self.eve_solar_system:
            return None
        try:
            return self.eve_solar_system.eve_constellation.eve_region.name
        except AttributeError:
            return None

    def as_percent(self, field):
        """Cost indices are fractions; the game shows them as percentages."""
        value = getattr(self, field, None)
        return None if value is None else value * 100


class SovCampaign(models.Model):
    """An Entosis campaign against, or in defence of, a sovereignty structure.

    This is how "reinforced" becomes visible. The hub detail route carries no
    state field -- it tells you when a hub is vulnerable, not whether anyone
    actually knocked it down -- but a campaign existing against our IHUB means
    exactly that. Public route, cached five seconds, so the scores here are
    close to live during a fight.
    """

    class EventType(models.TextChoices):
        IHUB = "ihub_defense", "Sovereignty hub defence"
        TCU = "tcu_defense", "TCU defence"
        STATION = "station_defense", "Station defence"
        FREEPORT = "station_freeport", "Station freeport"

    campaign_id = models.BigIntegerField(primary_key=True)
    structure_id = models.BigIntegerField(db_index=True)
    solar_system_id = models.BigIntegerField(db_index=True)
    eve_solar_system = models.ForeignKey(
        EveSolarSystem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    constellation_id = models.BigIntegerField(null=True, blank=True)
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    defender_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    defender_score = models.FloatField(null=True, blank=True)
    attackers_score = models.FloatField(null=True, blank=True)
    start_time = models.DateTimeField(db_index=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["start_time"]
        verbose_name = "sovereignty campaign"

    def __str__(self) -> str:
        return f"{self.get_event_type_display()} in {self.system_name}"

    @property
    def system_name(self) -> str:
        if self.eve_solar_system:
            return self.eve_solar_system.name
        return str(self.solar_system_id)

    @property
    def has_started(self) -> bool:
        return self.start_time <= timezone.now()


class AlertCategory(models.TextChoices):
    """The kinds of alert this app raises.

    Split finely enough that an alliance can send fuel warnings to a logistics
    channel and "something is being shot" to a ping channel, which is what
    happens the moment more than one person is responsible for anything.
    """

    SOV_FUEL = "sov_fuel", "Sovereignty: hub fuel"
    SOV_UPGRADE = "sov_upgrade", "Sovereignty: upgrade unpowered"
    SOV_ADM = "sov_adm", "Sovereignty: ADM below threshold"
    SOV_REINFORCED = "sov_reinforced", "Sovereignty: hub reinforced"
    SKYHOOK_THEFT = "skyhook_theft", "Skyhook: theft window opening"
    SKYHOOK_ATTACK = "skyhook_attack", "Skyhook: under attack"
    DEN_ATTACK = "den_attack", "Den: under attack"
    DEN_REINFORCED = "den_reinforced", "Den: reinforced"
    DEN_MTO = "den_mto", "Den: tactical operation available"
    DEN_SIPHON = "den_siphon", "Den: siphoning our workforce"


# Which front-end settings page owns each category, and which master switch
# turns it off.
CATEGORY_SECTIONS = {
    AlertCategory.SOV_FUEL: "sov",
    AlertCategory.SOV_UPGRADE: "sov",
    AlertCategory.SOV_ADM: "sov",
    AlertCategory.SOV_REINFORCED: "sov",
    AlertCategory.SKYHOOK_THEFT: "skyhook",
    AlertCategory.SKYHOOK_ATTACK: "skyhook",
    AlertCategory.DEN_ATTACK: "den",
    AlertCategory.DEN_REINFORCED: "den",
    AlertCategory.DEN_MTO: "den",
    AlertCategory.DEN_SIPHON: "den",
}


class AlertRoute(models.Model):
    """Where one category of alert goes.

    A category with no webhooks selected falls back to every enabled webhook,
    so an existing install keeps working after an upgrade instead of going
    quiet -- silence is the one failure mode nobody notices.
    """

    category = models.CharField(
        max_length=32, choices=AlertCategory.choices, unique=True
    )
    webhooks = models.ManyToManyField(
        Webhook,
        blank=True,
        related_name="routes",
        help_text="Leave empty to use every enabled webhook.",
    )
    is_enabled = models.BooleanField(
        default=True, help_text="Uncheck to stop this kind of alert entirely."
    )

    class Meta:
        ordering = ["category"]
        verbose_name = "alert route"

    def __str__(self) -> str:
        return self.get_category_display()

    @property
    def section(self) -> str:
        return CATEGORY_SECTIONS.get(self.category, "sov")

    @classmethod
    def for_category(cls, category):
        route, _created = cls.objects.get_or_create(category=category)
        return route


class DenOperator(models.Model):
    """Contact details for a person, not for each of their characters.

    Timezone and "how to reach me" belong to the human. Someone running four
    alts is still awake at one time of day, and asking them to fill the same
    answer in four times just produces three stale copies.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="holdfast_den_operator",
    )
    timezone = models.CharField(
        max_length=8,
        blank=True,
        choices=TIMEZONE_CHOICES,
        help_text="Roughly when you are awake, so den managers know when to "
        "expect an answer.",
    )
    contact_note = models.CharField(
        max_length=200,
        blank=True,
        help_text="Anything else a den manager should know to reach you.",
    )
    updated_at = models.DateTimeField(default=timezone_module.now)

    class Meta:
        verbose_name = "den operator"

    def __str__(self) -> str:
        return str(self.user)

    @classmethod
    def for_user(cls, user):
        operator, _created = cls.objects.get_or_create(user=user)
        return operator

    @property
    def local_time(self):
        zone = TIMEZONE_ZONES.get(self.timezone)
        if not zone:
            return None
        try:
            from zoneinfo import ZoneInfo

            return timezone_module.now().astimezone(ZoneInfo(zone))
        except Exception:  # noqa: BLE001
            return None

    @property
    def characters(self):
        return [
            den_character.character
            for den_character in DenCharacter.objects.filter(
                character_ownership__user=self.user
            ).select_related("character_ownership__character")
        ]
