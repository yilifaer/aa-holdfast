"""Three sidebar entries from one app.

Splitting the menu is not the same as splitting the app. These three areas
share a sync layer, a rate-limit budget and a set of tokens, and the den
features are derived from skyhook data -- but to the people using it they are
three different jobs, so they get three different doors.

Registering several menu items from one app is a supported pattern; corptools
and Alliance Auth's own group management both do it.
"""

from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook
from django.utils.translation import gettext_lazy as _

from . import urls
from .core import attention
from .views.common import den_can_enter, skyhook_can_enter, sov_can_enter


def _section_pages(prefix, *extra):
    """Every page in one section, for highlighting its sidebar entry.

    Auth matches ``navactive`` against exact URL names, or a whole namespace --
    never a prefix, which silently matches nothing. The namespace form is out
    too: all three entries share this app's, so it would light them together.
    With nothing lit, Auth also collapses whatever sidebar folder an admin
    filed these entries under, and every section's home redirects elsewhere,
    so that happened on every page.
    """
    names = [p.name for p in urls.urlpatterns if getattr(p, "name", None)]
    return [
        f"holdfast:{name}"
        for name in names + list(extra)
        if name.startswith(prefix) or name in extra
    ]


class _HoldfastMenu(MenuItemHook):
    """Shared plumbing: hide the entry from anyone who may not open it, and
    carry a badge showing how many things in that section want attention.

    The sidebar renders on every page of the site, so the counters behind
    ``counter`` are aggregate queries with a short per-user cache -- see
    ``core.attention``.
    """

    predicate = staticmethod(lambda user: False)
    counter = staticmethod(lambda user: 0)

    def render(self, request):
        if not self.predicate(request.user):
            return ""
        count = self.counter(request.user)
        self.count = count if count else None
        return MenuItemHook.render(self, request)


class SovMenu(_HoldfastMenu):
    predicate = staticmethod(sov_can_enter)
    counter = staticmethod(attention.sov_count)

    def __init__(self):
        MenuItemHook.__init__(
            self,
            _("SOV Monitor"),
            "fas fa-flag fa-fw",
            "holdfast:sov_home",
            # The Tokens page belongs to no section, and its title already
            # says SOV Monitor -- so that is the entry it keeps lit.
            navactive=_section_pages("sov_", "owners"),
        )


class SkyhookMenu(_HoldfastMenu):
    predicate = staticmethod(skyhook_can_enter)
    counter = staticmethod(attention.skyhook_count)

    def __init__(self):
        MenuItemHook.__init__(
            self,
            _("Skyhook Monitor"),
            "fas fa-satellite-dish fa-fw",
            "holdfast:skyhook_home",
            navactive=_section_pages("skyhook_"),
        )


class DenMenu(_HoldfastMenu):
    predicate = staticmethod(den_can_enter)
    counter = staticmethod(attention.den_count)

    def __init__(self):
        MenuItemHook.__init__(
            self,
            _("Den Monitor"),
            "fas fa-house-flag fa-fw",
            "holdfast:den_home",
            navactive=_section_pages("den_"),
        )


@hooks.register("menu_item_hook")
def register_sov_menu():
    return SovMenu()


@hooks.register("menu_item_hook")
def register_skyhook_menu():
    return SkyhookMenu()


@hooks.register("menu_item_hook")
def register_den_menu():
    return DenMenu()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "holdfast", r"^holdfast/")


@hooks.register("charlink")
def register_charlink():
    """Offer our tokens on the charlink page, for installs that use it.

    charlink imports this module lazily, so returning the path costs nothing
    when charlink is not installed. The manual registration page stays either
    way -- no alliance should have to install a second app to use this one.
    """
    return "holdfast.thirdparty.charlink_hook"
