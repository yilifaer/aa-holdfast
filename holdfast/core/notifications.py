"""Alliance Auth's own notification bell, alongside Discord.

Discord is where a fire gets shouted about; the bell is where something
addressed to *you personally* waits until you next log in. A rejected den
claim belongs in the second category -- nobody wants that announced in a
channel, and the applicant should still find out.

Everything here is best-effort: a notification failing must never take a sync
task or a form submission down with it.
"""

import logging

from django.contrib.auth.models import Permission, User

logger = logging.getLogger(__name__)


def notify_user(user, title, message, level="info") -> bool:
    """Send one notification to one user. Never raises."""
    if user is None:
        return False
    try:
        from allianceauth.notifications import notify

        notify(user=user, title=title, message=message, level=level)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Could not notify %s", user)
        return False


def notify_permission_holders(codename, title, message, level="info") -> int:
    """Notify everyone who holds one of our permissions.

    Used for alliance-level events -- a hub about to go dry, a den siphoning
    our workforce -- so the people responsible see it even if they never open
    Discord.
    """
    try:
        permission = Permission.objects.get(
            content_type__app_label="holdfast", codename=codename
        )
    except Permission.DoesNotExist:
        logger.warning("Permission holdfast.%s does not exist", codename)
        return 0

    users = (
        User.objects.filter(
            # Either granted directly or through a group. Superusers are
            # deliberately excluded: they hold every permission on the site and
            # would otherwise be notified about every app's business.
            models_q(permission)
        )
        .distinct()
    )
    sent = 0
    for user in users:
        if notify_user(user, title, message, level):
            sent += 1
    return sent


def models_q(permission):
    from django.db.models import Q

    return Q(user_permissions=permission) | Q(groups__permissions=permission)


def invalidate_badges(user):
    """Refresh a user's sidebar badge immediately after something changed."""
    try:
        from .attention import invalidate

        invalidate(user)
    except Exception:  # noqa: BLE001
        logger.exception("Could not invalidate attention cache")
