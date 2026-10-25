"""Alliance Auth's own notification bell, alongside Discord.

Discord is where a fire gets shouted about; the bell is where something
addressed to *you personally* waits until you next log in. A rejected den
claim belongs in the second category -- nobody wants that announced in a
channel, and the applicant should still find out.

Everything here is best-effort: a notification failing must never take a sync
task or a form submission down with it.
"""

from allianceauth.services.hooks import get_extension_logger

logger = get_extension_logger(__name__)


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


def invalidate_badges(user):
    """Refresh a user's sidebar badge immediately after something changed."""
    try:
        from .attention import invalidate

        invalidate(user)
    except Exception:  # noqa: BLE001
        logger.exception("Could not invalidate attention cache")
