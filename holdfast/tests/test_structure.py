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
        for module in ("den", "sov", "skyhook", "owners", "webhooks"):
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


class RegistrationGuardTests(SimpleTestCase):
    """Both ways of registering a corporation must ask the same question.

    The first version of this guard would have gone on a charlink function
    nothing called: the hook registers ``_add_all``, and the two helpers
    beside it had no caller. A guard there guards nothing, so the check is on
    the functions that are actually wired up, by name.
    """

    GUARDED = {
        ("views", "owners.py"): "add_owner",
        ("thirdparty", "charlink_hook.py"): "_add_all",
    }

    def _calls_in(self, path, function_name):
        tree = ast.parse(_read(*path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == function_name:
                return {
                    ast.unparse(call.func)
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                }
        self.fail(f"{'/'.join(path)} has no function {function_name}")

    def test_every_registration_path_calls_the_guard(self):
        for path, function_name in self.GUARDED.items():
            with self.subTest(function=function_name):
                self.assertIn("registration_refusal", self._calls_in(path, function_name))

    def test_no_registration_path_re_enables_an_owner(self):
        """``is_enabled`` in the update_or_create defaults is how re-adding an
        expired token silently switched a deliberately disabled owner back on."""
        for path, function_name in self.GUARDED.items():
            tree = ast.parse(_read(*path))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == function_name:
                    self.assertNotIn('"is_enabled"', ast.unparse(node), function_name)

    def test_the_charlink_hook_has_no_unwired_entry_points(self):
        tree = ast.parse(_read("thirdparty", "charlink_hook.py"))
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        self.assertNotIn("_add_corporation", defined)
        self.assertNotIn("_add_den_character", defined)


class OwnerScopeTests(SimpleTestCase):
    """Every alert that walks owner rows must decide by the active set.

    These loops are what kept posting for a disabled owner's frozen hubs and
    for a foreign alliance's skyhooks. A new check that forgets is the same
    bug again, so each one has to reference the active-owner helper.
    """

    OWNER_ROW_CHECKS = {
        "alerts.py": (
            "check_hub_fuel",
            "check_hub_upgrades",
            "check_skyhook_theft",
            "check_skyhook_attacks",
            "check_hub_reinforced",
        ),
        "den_alerts.py": ("check_workforce_siphon", "check_siphoned_skyhooks"),
    }

    def test_every_owner_row_alert_is_scoped_to_active_owners(self):
        for filename, names in self.OWNER_ROW_CHECKS.items():
            tree = ast.parse(_read("core", filename))
            found = {
                node.name: ast.unparse(node)
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name in names
            }
            for name in names:
                with self.subTest(check=name):
                    self.assertIn(name, found, f"{filename} has no {name}")
                    self.assertIn("_active_owner_ids()", found[name])

    def test_the_adm_check_reads_the_tracked_set_not_the_table(self):
        source = _read("core", "alerts.py")
        block = source[source.index("def check_adm") :]
        block = block[: block.index("\ndef ")]
        self.assertIn("tracked_alliance_ids()", block)

    def test_the_tracked_set_lives_in_core_scope_only(self):
        """One definition. esi_sync used to have its own, derived from the
        owners -- which is the derivation that made a foreign alliance
        tracked in the first place."""
        for path in sorted(ROOT.rglob("*.py")):
            if path.name in ("scope.py",) or "tests" in path.parts:
                continue
            self.assertNotIn(
                "def tracked_alliance_ids", path.read_text(encoding="utf-8"), str(path)
            )


class MigrationTests(SimpleTestCase):
    """A migration is frozen; the code it would import is not.

    The tracked-alliance bootstrap (0009, once part of 0008) first shipped
    calling ``holdfast.core.scope`` for its data step. That
    works until the helper is renamed or its signature changes, at which point
    every fresh install fails inside ``migrate`` in a file nobody edits any
    more. A data migration carries its own loop over the historical models
    it is handed, and reads settings through ``django.conf.settings``.
    """

    def test_no_migration_imports_the_live_app(self):
        for path in sorted((ROOT / "migrations").glob("0*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse(
                        (node.module or "").startswith("holdfast"),
                        f"{path.name} imports {node.module}",
                    )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(
                            alias.name.startswith("holdfast"),
                            f"{path.name} imports {alias.name}",
                        )


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
        skip = re.compile(
            r"_(save|test|add|claim|decide|record|revoke|dismiss|withdraw|delete)$|^add_"
        )
        pages = [n for n in names if not skip.search(n)]

        unreachable = [n for n in pages if f"holdfast:{n}'" not in templates and f'holdfast:{n}"' not in templates]
        self.assertEqual(unreachable, [], "pages with a URL and nothing that links to them")

    def test_the_owners_page_is_linked_from_every_section(self):
        """Token registration belongs to no section, so each one must offer it."""
        for section in SECTIONS:
            nav = _read("templates", "holdfast", section, "base.html")
            self.assertIn("holdfast:owners", nav, f"{section} nav does not offer the owners page")

    def test_the_webhooks_page_is_linked_beside_the_tokens_page(self):
        """Same reason: webhooks belong to the install, not to a section."""
        for parts in [(section, "base.html") for section in SECTIONS] + [("base.html",)]:
            nav = _read("templates", "holdfast", *parts)
            self.assertIn("holdfast:webhooks", nav, f"{'/'.join(parts)} does not offer the webhooks page")


class VersionTagTests(SimpleTestCase):
    """The version has to be visible without Django admin.

    The alliance's IT asked which version was running and the only answer was
    "open the admin and look at the package" -- which they cannot. Every base
    template shows it beside the brand, so it is on every page of the app.
    """

    BASES = [(section, "base.html") for section in SECTIONS] + [("base.html",)]

    def test_the_tag_reports_the_package_version(self):
        from .. import __version__
        from ..templatetags.holdfast_tags import holdfast_version

        self.assertEqual(holdfast_version(), __version__)

    def test_every_base_template_renders_the_tag(self):
        for parts in self.BASES:
            with self.subTest(template="/".join(parts)):
                source = _read("templates", "holdfast", *parts)
                self.assertIn("{% load holdfast_tags %}", source)
                self.assertRegex(source, r"\{% block header_nav_brand %\}.*\{% holdfast_version %\}")
