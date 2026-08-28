from django import template

register = template.Library()


@register.filter
def hours_human(value):
    """Render a float number of hours as something an FC can read at a glance."""
    if value is None:
        return "-"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "-"
    if value <= 0:
        return "empty"
    if value < 1:
        return f"{value * 60:.0f} min"
    if value < 48:
        return f"{value:.1f} h"
    return f"{value / 24:.1f} d"


@register.filter
def fuel_row_class(hours):
    """Bootstrap table-row colour keyed to how urgent the refuel is."""
    if hours is None:
        return ""
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        return ""
    if hours <= 6:
        return "table-danger"
    if hours <= 24:
        return "table-warning"
    if hours <= 48:
        return "table-info"
    return ""


@register.filter
def as_percent(value, places=2):
    """Cost indices come out of ESI as fractions; the game shows percentages."""
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.{int(places)}f}%"
    except (TypeError, ValueError):
        return "-"
