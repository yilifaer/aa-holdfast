"""Assertions about the shape of the code, not about what it computes.

Every bug an outside review found in 0.1.0 shared a trait: it was invisible on
this alliance's data. A budget split that rounds to zero needs one hub among
two hundred skyhooks. Cross-alliance writes need a second alliance. A dead
settings switch needs somebody to toggle it. No amount of testing against our
own numbers would have found any of them, because our numbers are what hid
them.

What these tests check instead is that the code keeps its own rules -- a write
path goes through a scoped queryset, a setting somebody can change is read
somewhere, an alert category has something that emits it. Those hold or fail
regardless of whose data is in the database, which is exactly the property the
other tests lack.

They will mostly pass forever. That is fine: the cost is a second of CI, and
the thing they are guarding against is a Tuesday afternoon where somebody adds
a field to a form and forgets the other half.
"""

import ast
import pathlib
import re

from django.test import SimpleTestCase

ROOT = pathlib.Path(__file__).resolve().parent.parent
SECTIONS = ("sov", "skyhook", "den")


def _read(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def _core_source():
    return "".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "core").glob("*.py"))
        # Samples build embeds for the test-alert command; they prove nothing
        # about whether a category is ever emitted for real.
        if path.name != "alert_samples.py"
    )


class WriteScopeTests(SimpleTestCase):
    """A POST handler must not fetch a row by primary key alone.

    The list pages filtered by alliance from the start, which is what made the
    gap invisible: everything looked scoped until you POSTed an id you were
    not shown. A bare ``get_object_or_404(Model, pk=...)`` in a write path is
    that gap, so it is banned outright -- either look through a scoped
    queryset, or constrain by the requesting user.
    """

    def _post_handlers(self, module):
        tree = ast.parse(_read("views", f"{module}.py"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            decorators = " ".join(ast.unparse(d) for d in node.decorator_list)
            if "require_POST" in decorators:
                yield node

    def test_every_write_path_is_scoped(self):
        offenders = []
        for module in ("den", "sov", "skyhook", "owners"):
            for node in self._post_handlers(module):
                body = ast.unparse(node)
                for call in re.finditer(r"get_object_or_404\(([^)]*)\)", body):
                    argument = call.group(1)
                    first = argument.split(",")[0].strip()
                    looks_like_bare_model = (
                        first[:1].isupper() and ".objects" not in first
                    )
                    # Constraining by the requester is a scope too, and a
                    # tighter one than the alliance.
                    scoped_by_user = "user=request.user" in argument
                    if looks_like_bare_model and not scoped_by_user:
                        offenders.append(f"{module}.{node.name}: {first}")
        self.assertEqual(
            offenders,
            [],
            "write handlers fetching a row by primary key with no scope",
        )


class SettingsFormTests(SimpleTestCase):
    """Both halves of a settings page have to exist.

    A form field the save view never reads is input that vanishes on submit; a
    config field nothing reads is a switch that does nothing. The second half
    of that pair is what shipped in 0.1.0, eight times.
    """

    # Rendered per row rather than named once, so they cannot be matched
    # against a literal in the view.
    DYNAMIC_PREFIXES = ("route_", "threshold_", "enabled_", "webhook_")

    def test_every_form_field_is_read_by_the_save_view(self):
        views = _read("views", "common.py")
        orphans = []
        for section in SECTIONS:
            template = _read("templates", "holdfast", section, "settings.html")
            source = views + _read("views", f"{section}.py")
            for name in sorted(set(re.findall(r'name="([a-z_]+)"', template))):
                if name.startswith(self.DYNAMIC_PREFIXES) or name == "csrfmiddlewaretoken":
                    continue
                if f'"{name}"' not in source and f"'{name}'" not in source:
                    orphans.append(f"{section}: {name}")
        self.assertEqual(orphans, [], "form fields the save view never reads")

    def test_no_setting_is_written_by_the_form_and_read_by_nobody(self):
        """The audit that found the other seven, kept as a test.

        A field only the settings page touches is a promise the app does not
        keep. If a new one appears, this fails before anyone ships it.
        """
        tree = ast.parse(_read("models.py"))
        fields = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "HoldfastConfig":
                for statement in node.body:
                    if isinstance(statement, ast.Assign) and isinstance(
                        statement.value, ast.Call
                    ):
                        if "models." in ast.unparse(statement.value):
                            fields.append(ast.unparse(statement.targets[0]))

        consumed = ""
        for path in list((ROOT / "core").glob("*.py")) + [ROOT / "models.py"]:
            consumed += path.read_text(encoding="utf-8")

        # SECTION_SWITCH reaches its fields through getattr, so the names only
        # appear as strings.
        unread = [
            name
            for name in fields
            if not re.search(rf"\.{name}\b|['\"]{name}['\"]", consumed)
        ]
        self.assertEqual(
            unread, [], "settings the pages write and nothing ever reads"
        )

    def test_every_form_field_is_a_real_config_field(self):
        """A typo in a template name silently saves nothing."""
        tree = ast.parse(_read("models.py"))
        config_fields = set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "HoldfastConfig":
                for statement in node.body:
                    if isinstance(statement, ast.Assign) and isinstance(
                        statement.value, ast.Call
                    ):
                        if "models." in ast.unparse(statement.value):
                            config_fields.add(ast.unparse(statement.targets[0]))

        unknown = []
        for section in SECTIONS:
            template = _read("templates", "holdfast", section, "settings.html")
            for name in sorted(set(re.findall(r'name="([a-z_]+)"', template))):
                if name.startswith(self.DYNAMIC_PREFIXES) or name == "csrfmiddlewaretoken":
                    continue
                if name not in config_fields:
                    unknown.append(f"{section}: {name}")
        self.assertEqual(unknown, [], "form fields with no config field behind them")


class AlertCategoryTests(SimpleTestCase):
    """A category that nothing emits is a row in the routing table that lies.

    Somebody would tick it, choose a channel, and wait for an alert that has
    no code behind it.
    """

    def test_every_category_is_emitted_somewhere(self):
        models = _read("models.py")
        block = models[models.index("class AlertCategory") :]
        block = block[: block.index("\n\n\n")]
        constants = re.findall(r"^\s{4}([A-Z_]+) = ", block, re.M)
        emitted = _core_source()
        dead = [c for c in constants if f"AlertCategory.{c}" not in emitted]
        self.assertEqual(dead, [], "alert categories nothing ever sends")

    def test_every_category_belongs_to_a_section(self):
        """CATEGORY_SECTIONS drives the routing page and the Discord switch.

        A category missing from it renders on no settings page and ignores the
        section's on/off switch, which is the quiet way to make an alert
        unturnoffable.
        """
        from ..models import CATEGORY_SECTIONS, AlertCategory

        missing = [c for c in AlertCategory if c not in CATEGORY_SECTIONS]
        self.assertEqual(missing, [], "categories with no section")


class PermissionTests(SimpleTestCase):
    """A permission nobody checks is a permission nobody should be granted.

    This app shipped `den_claim` before, gating an action whose only entry
    point was behind a different permission -- so a group holding it looked
    able to claim a site and could not reach the page to do it.
    """

    def test_every_declared_permission_is_enforced_somewhere(self):
        models = _read("models.py")
        block = models[models.index("permissions = (") :]
        block = block[: block.index("\n        )")]
        declared = re.findall(r'\("([a-z_]+)",', block)

        enforced = "".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "views").glob("*.py"))
        ) + _read("auth_hooks.py") + _read("thirdparty", "charlink_hook.py")

        unused = [name for name in declared if f'"{name}"' not in enforced]
        self.assertEqual(unused, [], "permissions declared and never checked")


class ReachabilityTests(SimpleTestCase):
    """Every page a permission unlocks must be linked from somewhere.

    The owners page went out with no link to it at all. Nobody here noticed:
    the URL was in muscle memory from the first day, and the README said
    "Owners page" as if that named a place you could click. The first outside
    install spent an evening with full admin access, unable to find it.
    """

    def _template_source(self):
        return "".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "templates").rglob("*.html"))
        )

    def test_every_named_page_is_linked_from_a_template(self):
        urls = _read("urls.py")
        names = re.findall(r'name="([a-z_]+)"', urls)
        templates = self._template_source()

        # Form targets and SSO callbacks are reached by submitting, not
        # clicking; a page you land on after one is not what this guards.
        skip = re.compile(r"_(save|test|add|claim|decide|record|revoke|dismiss|withdraw)$|^add_")
        pages = [n for n in names if not skip.search(n)]

        unreachable = [n for n in pages if f"holdfast:{n}'" not in templates and f'holdfast:{n}"' not in templates]
        self.assertEqual(unreachable, [], "pages with a URL and nothing that links to them")

    def test_the_owners_page_is_linked_from_every_section(self):
        """Token registration belongs to no section, so each one must offer it."""
        for section in SECTIONS:
            nav = _read("templates", "holdfast", section, "base.html")
            self.assertIn("holdfast:owners", nav, f"{section} nav does not offer the owners page")
