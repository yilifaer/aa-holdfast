"""Break each rule on purpose and check the guard notices.

A structural test that has never failed is a claim, not evidence. These
mutations are the evidence: each one introduces exactly the mistake the test
exists to catch, runs that test, and puts the file back.
"""

import io
import os
import pathlib
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = os.environ.get("HOLDFAST_PYTHON", sys.executable)

MUTATIONS = [
    (
        "a category with no section mapping",
        "holdfast/models.py",
        '    AlertCategory.SOV_ADM: "sov",\n',
        "",
        "AlertCategoryTests",
    ),
    (
        "a category nothing emits",
        "holdfast/core/alerts.py",
        "category=AlertCategory.SOV_ADM",
        "category=None",
        "AlertCategoryTests",
    ),
    (
        "a permission nobody checks",
        "holdfast/models.py",
        '            ("manage_owners",',
        '            ("ghost_perm", "declared and never enforced"),\n            ("manage_owners",',
        "PermissionTests",
    ),
    (
        # The real shape of the mistake: somebody adds a switch to the model
        # and the form, and forgets the half that reads it.
        "a new config field nothing reads",
        "holdfast/models.py",
        "    den_discord_enabled = models.BooleanField(",
        "    notify_something_new = models.BooleanField(default=True)\n"
        "    den_discord_enabled = models.BooleanField(",
        "SettingsFormTests",
    ),
    (
        "a form input with no config field behind it",
        "holdfast/templates/holdfast/sov/settings.html",
        'name="fuel_warning_days"',
        'name="fuel_warnning_days"',
        "SettingsFormTests",
    ),
]

for label, relative, old, new, test_class in MUTATIONS:
    path = ROOT / relative
    original = path.read_text(encoding="utf-8")
    if old not in original:
        print(f"  ??  anchor missing, skipped: {label}")
        continue
    path.write_text(original.replace(old, new, 1), encoding="utf-8")
    try:
        result = subprocess.run(
            [PY, "runtests.py", f"holdfast.tests.test_structure.{test_class}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stdout + result.stderr
        caught = result.returncode != 0
        mark = "caught" if caught else "MISSED"
        print(f"  {mark:<7} {label}")
    finally:
        path.write_text(original, encoding="utf-8")

print("\nall files restored")
