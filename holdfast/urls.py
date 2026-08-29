from django.urls import path

from .views import den, owners, skyhook, sov

app_name = "holdfast"

urlpatterns = [
    # --- Sovereignty ---
    path("sov/", sov.home, name="sov_home"),
    path("sov/dashboard/", sov.dashboard, name="sov_dashboard"),
    path("sov/fuel/", sov.fuel, name="sov_fuel"),
    path("sov/adm/", sov.adm, name="sov_adm"),
    path("sov/timers/", sov.timers, name="sov_timers"),
    path("sov/cost/", sov.system_cost, name="sov_system_cost"),
    path("sov/settings/", sov.settings_view, name="sov_settings"),
    path("sov/settings/save/", sov.settings_save, name="sov_settings_save"),
    path("sov/settings/test/", sov.settings_test, name="sov_settings_test"),
    # --- Skyhooks ---
    path("skyhook/", skyhook.home, name="skyhook_home"),
    path("skyhook/dashboard/", skyhook.dashboard, name="skyhook_dashboard"),
    path("skyhook/list/", skyhook.skyhook_list, name="skyhook_list"),
    path("skyhook/timers/", skyhook.timers, name="skyhook_timers"),
    path("skyhook/raid/", skyhook.raid_targets, name="skyhook_raid"),
    path("skyhook/settings/", skyhook.settings_view, name="skyhook_settings"),
    path("skyhook/settings/save/", skyhook.settings_save, name="skyhook_settings_save"),
    path("skyhook/settings/test/", skyhook.settings_test, name="skyhook_settings_test"),
    # --- Mercenary dens ---
    path("den/", den.home, name="den_home"),
    path("den/dashboard/", den.dashboard, name="den_dashboard"),
    path("den/information/", den.information, name="den_information"),
    path("den/timers/", den.timers, name="den_timers"),
    path("den/list/", den.den_list, name="den_list"),
    path("den/admin/", den.admin_page, name="den_admin"),
    path("den/settings/", den.settings_view, name="den_settings"),
    path("den/settings/save/", den.settings_save, name="den_settings_save"),
    path("den/settings/test/", den.settings_test, name="den_settings_test"),
    path("den/slot/<int:slot_pk>/claim/", den.claim_slot, name="den_claim_slot"),
    path("den/slot/<int:slot_pk>/record/", den.record_den, name="den_record_den"),
    path("den/claim/<int:claim_pk>/withdraw/", den.withdraw_claim, name="den_withdraw_claim"),
    path("den/claim/<int:claim_pk>/dismiss/", den.dismiss_claim, name="den_dismiss_claim"),
    path(
        "den/claim/<int:claim_pk>/<str:decision>/",
        den.decide_claim,
        name="den_decide_claim",
    ),
    path("den/contact/", den.save_contact, name="den_save_contact"),
    # --- Token registration, shared ---
    path("owners/", owners.index, name="owners"),
    path("owners/add/", owners.add_owner, name="add_owner"),
    path("owners/add-den-character/", owners.add_den_character, name="add_den_character"),
]
