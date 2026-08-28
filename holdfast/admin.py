from django.contrib import admin

from .models import (
    AlertLog,
    AlertRoute,
    SovCampaign,
    DenCharacter,
    DenClaim,
    DenEvent,
    DenSlot,
    MercenaryDen,
    MercenaryTacticalOperation,
    Owner,
    ReagentThreshold,
    Skyhook,
    SkyhookReagent,
    SovHub,
    SovHubReagent,
    SovHubUpgrade,
    HoldfastConfig,
    SovSystem,
    Webhook,
)


@admin.register(Owner)
class OwnerAdmin(admin.ModelAdmin):
    list_display = ("corporation", "character_ownership", "is_enabled", "last_sync_at", "last_sync_ok")
    list_filter = ("is_enabled", "last_sync_ok")
    readonly_fields = ("last_sync_at", "last_sync_ok", "last_error")


class SovHubReagentInline(admin.TabularInline):
    model = SovHubReagent
    extra = 0


class SovHubUpgradeInline(admin.TabularInline):
    model = SovHubUpgrade
    extra = 0


@admin.register(SovHub)
class SovHubAdmin(admin.ModelAdmin):
    list_display = ("hub_id", "system_name", "owner", "fuel_expires_at", "last_seen_at")
    inlines = (SovHubReagentInline, SovHubUpgradeInline)


class SkyhookReagentInline(admin.TabularInline):
    model = SkyhookReagent
    extra = 0


@admin.register(Skyhook)
class SkyhookAdmin(admin.ModelAdmin):
    list_display = ("skyhook_id", "planet_name", "owner", "state", "theft_start", "last_seen_at")
    list_filter = ("state", "is_active")
    inlines = (SkyhookReagentInline,)


@admin.register(ReagentThreshold)
class ReagentThresholdAdmin(admin.ModelAdmin):
    """Edit the bars straight in the list view -- that is the whole point."""

    list_display = ("reagent", "min_unsecured", "is_enabled")
    list_display_links = ("reagent",)
    list_editable = ("min_unsecured", "is_enabled")
    readonly_fields = ("type_id", "eve_type")

    @admin.display(description="reagent", ordering="eve_type__name")
    def reagent(self, obj):
        return obj.type_name


@admin.register(SovSystem)
class SovSystemAdmin(admin.ModelAdmin):
    list_display = ("solar_system_id", "system_name", "alliance_id", "activity_defense_multiplier")


@admin.register(Webhook)
class WebhookAdmin(admin.ModelAdmin):
    list_display = ("name", "is_enabled", "ping_type")


@admin.register(AlertLog)
class AlertLogAdmin(admin.ModelAdmin):
    list_display = ("key", "sent_at")
    search_fields = ("key",)


@admin.register(HoldfastConfig)
class HoldfastConfigAdmin(admin.ModelAdmin):
    """Single row. The fuel bands here colour the dashboard and drive alerts."""

    list_display = (
        "__str__",
        "fuel_warning_days",
        "fuel_danger_days",
        "fuel_critical_days",
    )

    def has_add_permission(self, request):
        return not HoldfastConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DenCharacter)
class DenCharacterAdmin(admin.ModelAdmin):
    list_display = ("__str__", "is_enabled", "last_sync_at", "last_sync_ok")
    list_filter = ("is_enabled", "last_sync_ok")
    readonly_fields = ("last_sync_at", "last_sync_ok", "last_error")


class DenClaimInline(admin.TabularInline):
    model = DenClaim
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(DenSlot)
class DenSlotAdmin(admin.ModelAdmin):
    list_display = (
        "planet_name",
        "system_name",
        "status_label",
        "holder_name",
        "recorded_den",
        "recorded_hostile",
    )
    list_filter = ("recorded_den",
        "recorded_hostile",)
    inlines = (DenClaimInline,)
    readonly_fields = ("skyhook", "created_at")


@admin.register(DenClaim)
class DenClaimAdmin(admin.ModelAdmin):
    list_display = ("slot", "character", "user", "status", "created_at", "decided_at")
    list_filter = ("status",)
    search_fields = ("character__character_name", "user__username")


@admin.register(MercenaryDen)
class MercenaryDenAdmin(admin.ModelAdmin):
    list_display = (
        "den_id",
        "planet_name",
        "eve_character",
        "state",
        "anarchy_level",
        "development_level",
        "detail_updated_at",
    )
    list_filter = ("state", "anarchy_level", "development_level")


@admin.register(MercenaryTacticalOperation)
class MercenaryTacticalOperationAdmin(admin.ModelAdmin):
    list_display = ("operation_id", "den", "type_name", "state", "expires")
    list_filter = ("state",)


@admin.register(DenEvent)
class DenEventAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "kind", "den", "den_character", "is_alerted")
    list_filter = ("kind", "is_alerted")


@admin.register(AlertRoute)
class AlertRouteAdmin(admin.ModelAdmin):
    """Managers normally do this from the section settings pages; this is here
    for the initial setup and for anyone who prefers the admin."""

    list_display = ("category", "is_enabled", "channel_list")
    list_filter = ("is_enabled",)
    filter_horizontal = ("webhooks",)

    @admin.display(description="channels")
    def channel_list(self, obj):
        names = ", ".join(w.name for w in obj.webhooks.all())
        return names or "(every enabled webhook)"


@admin.register(SovCampaign)
class SovCampaignAdmin(admin.ModelAdmin):
    list_display = (
        "campaign_id", "system_name", "event_type", "start_time",
        "defender_score", "attackers_score",
    )
    list_filter = ("event_type",)
