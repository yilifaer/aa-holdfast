#!/usr/bin/env python
"""Run the test suite without a full Alliance Auth project.

    python runtests.py            # everything
    python runtests.py holdfast.tests.test_siphon
"""

import sys

import django
from django.conf import settings
from django.test.utils import get_runner


def main(argv):
    import tests.settings as test_settings

    settings.configure(
        **{
            name: getattr(test_settings, name)
            for name in dir(test_settings)
            if name.isupper()
        }
    )
    django.setup()
    runner = get_runner(settings)(verbosity=2, interactive=False)
    labels = argv[1:] or ["holdfast"]
    sys.exit(bool(runner.run_tests(labels)))


if __name__ == "__main__":
    main(sys.argv)
