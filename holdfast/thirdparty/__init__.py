"""Optional integrations with other Alliance Auth apps.

Nothing in here is imported unless the other app is installed and asks for it,
so an install without them carries no weight and no import errors.

allianceauth-corptools is the one that is read rather than imported:
``corptools_source`` looks its models up with ``apps.get_model`` and never
imports it, so it can be installed, absent or a different version without an
import error.
"""
