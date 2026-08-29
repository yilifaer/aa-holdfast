"""What the package imports has to match what it says it needs.

The failure mode this guards against is invisible on a developer's machine and
on ours: some other app in the same virtualenv already installed the library,
so the import works, the tests pass, and the first person to install into a
clean environment gets an ImportError from a module they have never heard of.

That happened here with PyYAML. Alliance Auth requires it too, so nothing ever
broke -- but depending on somebody else's dependency means an install can break
on a day this package did not change.
"""

import ast
import pathlib
import sys
import unittest

from django.test import SimpleTestCase

try:
    import tomllib  # standard library from Python 3.11
except ModuleNotFoundError:  # pragma: no cover - 3.10 only
    tomllib = None

# Alliance Auth is a hard dependency and drags these in, so importing them
# without naming them is safe and normal in an Auth app.
VIA_ALLIANCEAUTH = {
    "django",
    "celery",
    "solo",
    "sri",
    "django_bootstrap5",
    "sortedm2m",
}

# Imported only by charlink, which imports this module by path when it is
# installed. An install without charlink never reaches the import.
OPTIONAL = {"charlink"}

# Distribution name -> the module it provides, where they differ.
PROVIDES = {
    "django-esi": "esi",
    "django-eveuniverse": "eveuniverse",
    "dhooks-lite": "dhooks_lite",
    "allianceauth-app-utils": "app_utils",
    "pyyaml": "yaml",
}

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent


@unittest.skipIf(
    tomllib is None,
    "tomllib arrived in 3.11 and this test reads pyproject.toml. What it "
    "checks is a fact about the source tree rather than about the "
    "interpreter, so the other versions in the matrix cover it -- and adding "
    "a tomli dependency to support one Python version, inside a test about "
    "not carrying stray dependencies, would be its own joke.",
)
class DependencyDeclarationTests(SimpleTestCase):
    def _declared_modules(self):
        with open(PACKAGE_ROOT.parent / "pyproject.toml", "rb") as handle:
            project = tomllib.load(handle)["project"]
        modules = set()
        for requirement in project["dependencies"]:
            name = (
                requirement.split(">")[0].split("<")[0].split("=")[0].split("[")[0]
            ).strip()
            modules.add(PROVIDES.get(name.lower(), name.lower().replace("-", "_")))
        return modules, project["dependencies"]

    def _imported_modules(self):
        found = {}
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            parts = path.relative_to(PACKAGE_ROOT).parts
            if "migrations" in parts or "tests" in parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    top = name.split(".")[0]
                    if top and top not in sys.stdlib_module_names:
                        found.setdefault(top, set()).add("/".join(parts))
        return found

    def test_every_third_party_import_is_declared(self):
        declared, raw = self._declared_modules()
        allowed = declared | VIA_ALLIANCEAUTH | OPTIONAL | {"holdfast"}
        undeclared = {
            module: sorted(files)
            for module, files in self._imported_modules().items()
            if module not in allowed
        }
        self.assertEqual(
            undeclared,
            {},
            f"imported but not in pyproject dependencies {raw}",
        )

    def test_nothing_is_declared_that_is_never_imported(self):
        """A dependency nobody imports is one more thing for an installer to
        resolve, and one more version pin to go stale."""
        declared, _ = self._declared_modules()
        imported = set(self._imported_modules())
        # allianceauth is imported as `allianceauth`, so it appears in both.
        unused = declared - imported
        self.assertEqual(unused, set(), "declared but never imported")
