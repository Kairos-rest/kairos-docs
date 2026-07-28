#!/usr/bin/env python3
"""Unit tests for the docs sync pipeline's pure logic.

Deliberately stdlib-only (`python3 -m unittest`) so they run on the VPS with no
install step. Nothing here touches the network — the modules under test were
split out of `docs-sync.py` precisely so the diff selection, MDX surgery and nav
edits could be tested without NAN or GitHub.

Run: python3 -m unittest discover -s tooling/docs-sync -p 'test_*.py'
"""

from __future__ import annotations

import ast
import os
import tempfile
import unittest

from docs_changelog import (
    CHANGELOG_FALLBACK,
    append_changelog_entry,
    build_pr_body,
    escape_link_text,
    vet_changelog_summary,
)
from docs_diff import is_customer_facing, scope_diff_to_paths, select_release_diff
from docs_mdx import (
    PageParseError,
    apply_actions,
    build_new_page,
    parse_page,
    sanitize_scalar,
    set_frontmatter_key,
    validate_body,
)
from docs_nav import add_page, group_names, page_exists

PAGE = """---
title: "Cómo cargar tus facturas"
sidebarTitle: "Facturas"
description: "Cargá facturas y dejá que Kairos actualice tu stock."
icon: "receipt"
---

{/* diataxis: how-to */}

Cada factura que cargás actualiza el stock y el costo real de tus ingredientes.

## Elegí cómo cargarla

Podés cargarla a mano, por foto o por email.

## Qué pasa después de cargarla

Toda factura recorre el mismo circuito.

<Steps>
  <Step title="A revisar">
    Un administrador confirma los datos.
  </Step>
</Steps>
"""


class TestDiffSelection(unittest.TestCase):
    def test_customer_facing_includes_ui_and_service_code(self):
        for path in (
            "app/dashboard/invoices/page.tsx",
            "app/api/invoices/[id]/confirm/route.ts",
            "components/invoices/upload-panel.tsx",
            "lib/services/invoice/invoice-inline-drain.ts",
            "lib/schemas/invoice.ts",
            "messages/es.json",
            "docs/FEATURES.md",
        ):
            self.assertTrue(is_customer_facing(path), path)

    def test_customer_facing_excludes_internals(self):
        for path in (
            "lib/services/invoice/__tests__/drain.test.ts",
            "lib/services/invoice/drain.spec.ts",
            "tests/e2e/invoices.spec.ts",
            "lib/generated/client.ts",
            "prisma/schema.prisma",
            "prisma/migrations/20260101_x/migration.sql",
            "scripts/seed-demo.ts",
            ".github/workflows/ci.yml",
            "tooling/fudo-re/probe.mjs",
            "docs/agent/deployment.md",
            "AGENTS.md",
            "messages/metadata.json",
            "pnpm-lock.yaml",
        ):
            self.assertFalse(is_customer_facing(path), path)

    def test_exclusion_beats_inclusion_for_nested_tests(self):
        # A test file under an included prefix must still be excluded.
        self.assertFalse(is_customer_facing("app/api/invoices/__tests__/route.test.ts"))

    def test_priority_puts_copy_before_components(self):
        files = [
            {"filename": "components/x.tsx", "patch": "c"},
            {"filename": "messages/es.json", "patch": "m"},
            {"filename": "lib/services/y.ts", "patch": "s"},
        ]
        got = select_release_diff(files)
        self.assertEqual(
            got["included_paths"], ["messages/es.json", "lib/services/y.ts", "components/x.tsx"]
        )

    def test_budget_drops_are_reported_not_silent(self):
        files = [
            {"filename": "messages/es.json", "patch": "a" * 400},
            {"filename": "lib/services/y.ts", "patch": "b" * 400},
        ]
        got = select_release_diff(files, max_total_chars=500, max_file_chars=400)
        self.assertEqual(got["included_paths"], ["messages/es.json"])
        self.assertEqual(got["dropped_paths"], ["lib/services/y.ts"])

    def test_per_file_truncation_is_flagged_in_the_header(self):
        got = select_release_diff([{"filename": "messages/es.json", "patch": "a" * 100}], max_file_chars=10)
        self.assertIn("[patch truncated]", got["text"])

    def test_missing_patch_is_tracked_separately(self):
        got = select_release_diff([{"filename": "components/logo.png"}])
        self.assertEqual(got["no_patch_paths"], ["components/logo.png"])
        self.assertEqual(got["included_paths"], [])

    def test_ignored_count_counts_only_rejected_files(self):
        got = select_release_diff([
            {"filename": "messages/es.json", "patch": "m"},
            {"filename": "pnpm-lock.yaml", "patch": "l"},
            {"filename": "tests/x.spec.ts", "patch": "t"},
        ])
        self.assertEqual(got["ignored_count"], 2)

    def test_scope_diff_keeps_only_requested_files(self):
        diff = select_release_diff([
            {"filename": "messages/es.json", "patch": "MSG"},
            {"filename": "lib/services/y.ts", "patch": "SVC"},
        ])["text"]
        scoped = scope_diff_to_paths(diff, ["lib/services/y.ts"])
        self.assertIn("SVC", scoped)
        self.assertNotIn("MSG", scoped)

    def test_scope_diff_falls_back_to_full_diff_on_a_bad_path(self):
        diff = select_release_diff([{"filename": "messages/es.json", "patch": "MSG"}])["text"]
        self.assertEqual(scope_diff_to_paths(diff, ["nope/none.ts"]), diff)


class TestMdxParsing(unittest.TestCase):
    def test_parse_splits_frontmatter_intro_and_sections(self):
        page = parse_page(PAGE)
        self.assertIn('sidebarTitle: "Facturas"', page.frontmatter)
        self.assertEqual(page.diataxis, "{/* diataxis: how-to */}")
        self.assertTrue(page.intro.startswith("Cada factura"))
        self.assertEqual(page.headings, ["Elegí cómo cargarla", "Qué pasa después de cargarla"])

    def test_render_round_trips_without_losing_content(self):
        page = parse_page(PAGE)
        again = parse_page(page.render())
        self.assertEqual(again.headings, page.headings)
        self.assertEqual(again.frontmatter, page.frontmatter)
        self.assertIn("<Step title=\"A revisar\">", again.sections[1][1])

    def test_page_without_frontmatter_is_rejected(self):
        with self.assertRaises(PageParseError):
            parse_page("# hola\n")

    def test_page_without_diataxis_marker_is_rejected(self):
        with self.assertRaises(PageParseError):
            parse_page('---\ntitle: "x"\n---\n\nsin marcador\n')


class TestValidation(unittest.TestCase):
    def test_accepts_clean_spanish_body(self):
        self.assertEqual(validate_body("Ahora podés aprobar la factura desde **Facturas**."), [])

    def test_rejects_internal_vocabulary(self):
        for bad in (
            "Guardamos el dato en Prisma para tu local.",
            "El webhook actualiza el stock de tu local.",
            "Configurá el cron de tu organización para el envío.",
            "Editá lib/services/invoice/drain.ts para tu local.",
            # Spanish forms matter: the pages are written in Spanish, so an
            # English-only banned list would let these straight through.
            "Corré la migración antes de aprobar la factura del local.",
            "Kairos guarda la factura en la base de datos de tu local.",
            "El endpoint devuelve las facturas de tu local.",
        ):
            self.assertTrue(validate_body(bad), bad)

    def test_rejects_code_fences(self):
        self.assertTrue(validate_body("Copiá esto:\n```ts\nconst x = 1\n```\n"))

    def test_rejects_unbalanced_components(self):
        problems = validate_body("<Steps>\n  <Step title=\"a\">texto suficiente para pasar</Step>\n")
        self.assertTrue(any("unbalanced <Steps>" in p for p in problems))

    def test_accepts_balanced_components(self):
        body = "<Steps>\n  <Step title=\"a\">Confirmá los datos de tu local.</Step>\n</Steps>"
        self.assertEqual(validate_body(body), [])

    def test_self_closing_component_needs_no_partner(self):
        self.assertEqual(validate_body("Mirá el panel de tu local.\n<Frame src=\"/a.png\" />"), [])

    def test_tabs_and_tab_are_counted_separately(self):
        body = (
            "<Tabs>\n  <Tab title=\"Manual\">Cargala a mano desde tu local.</Tab>\n</Tabs>"
        )
        self.assertEqual(validate_body(body), [])

    def test_rejects_placeholders_and_stubs(self):
        self.assertTrue(validate_body("TODO: escribir esta sección más adelante."))
        self.assertTrue(validate_body("corto"))


class TestApplyActions(unittest.TestCase):
    def test_replace_section_only_touches_that_section(self):
        page = parse_page(PAGE)
        page, applied, rejected = apply_actions(page, [{
            "type": "replace_section",
            "heading": "Qué pasa después de cargarla",
            "body_mdx": "Ahora la factura queda aprobada en un solo paso.",
        }])
        self.assertEqual(applied, ["replace_section 'Qué pasa después de cargarla'"])
        self.assertEqual(rejected, [])
        self.assertIn("un solo paso", page.sections[1][1])
        self.assertIn("a mano, por foto", page.sections[0][1])

    def test_replace_section_with_unknown_heading_is_rejected_not_applied(self):
        page = parse_page(PAGE)
        page, applied, rejected = apply_actions(page, [{
            "type": "replace_section",
            "heading": "Sección inventada",
            "body_mdx": "Texto nuevo para la sección inventada del local.",
        }])
        self.assertEqual(applied, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn("no such heading", rejected[0])

    def test_invalid_body_is_rejected_and_page_is_untouched(self):
        page = parse_page(PAGE)
        before = page.render()
        page, applied, rejected = apply_actions(page, [{
            "type": "replace_section",
            "heading": "Elegí cómo cargarla",
            "body_mdx": "Guardamos todo en Postgres para tu local.",
        }])
        self.assertEqual(applied, [])
        self.assertTrue(rejected)
        self.assertEqual(page.render(), before)

    def test_append_section_lands_after_the_named_heading(self):
        page = parse_page(PAGE)
        page, applied, _ = apply_actions(page, [{
            "type": "append_section",
            "heading": "Carga por WhatsApp",
            "after_heading": "Elegí cómo cargarla",
            "body_mdx": "Mandá la foto de la factura al número de tu local.",
        }])
        self.assertEqual(page.headings[1], "Carga por WhatsApp")
        self.assertEqual(applied, ["append_section 'Carga por WhatsApp'"])

    def test_append_section_with_duplicate_heading_is_rejected(self):
        page = parse_page(PAGE)
        _, applied, rejected = apply_actions(page, [{
            "type": "append_section",
            "heading": "Elegí cómo cargarla",
            "body_mdx": "Otro texto suficientemente largo para el validador.",
        }])
        self.assertEqual(applied, [])
        self.assertIn("already exists", rejected[0])

    def test_replace_intro_updates_only_the_lead(self):
        page = parse_page(PAGE)
        page, applied, _ = apply_actions(page, [{
            "type": "replace_intro",
            "body_mdx": "Cargá la factura y Kairos actualiza el stock de tu local.",
        }])
        self.assertEqual(applied, ["replace_intro"])
        self.assertIn("actualiza el stock de tu local", page.intro)
        self.assertEqual(len(page.sections), 2)

    def test_identical_body_is_a_noop(self):
        page = parse_page(PAGE)
        _, applied, rejected = apply_actions(page, [{
            "type": "replace_section",
            "heading": "Elegí cómo cargarla",
            "body_mdx": "Podés cargarla a mano, por foto o por email.",
        }])
        self.assertEqual(applied, [])
        self.assertEqual(rejected, [])

    def test_none_action_is_a_clean_noop(self):
        page = parse_page(PAGE)
        _, applied, rejected = apply_actions(page, [{"type": "none"}])
        self.assertEqual((applied, rejected), ([], []))

    def test_unknown_action_type_is_reported(self):
        page = parse_page(PAGE)
        _, _, rejected = apply_actions(page, [{"type": "delete_page", "heading": "x", "body_mdx": "y" * 40}])
        self.assertIn("unknown action type", rejected[0])

    def test_frontmatter_is_never_reachable_from_an_action(self):
        page = parse_page(PAGE)
        page, _, _ = apply_actions(page, [{
            "type": "replace_section",
            "heading": "Elegí cómo cargarla",
            "body_mdx": "Cargala desde el panel de tu local cuando quieras.",
        }])
        self.assertIn('title: "Cómo cargar tus facturas"', page.render())


class TestFrontmatterAndNewPages(unittest.TestCase):
    def test_set_key_replaces_existing_value(self):
        fm = 'title: "x"\ntag: "OLD"'
        self.assertIn('tag: "NEW"', set_frontmatter_key(fm, "tag", "NEW"))
        self.assertNotIn("OLD", set_frontmatter_key(fm, "tag", "NEW"))

    def test_set_key_appends_when_absent(self):
        self.assertIn('tag: "NEW"', set_frontmatter_key('title: "x"', "tag", "NEW"))

    def test_new_page_is_parseable_and_tagged(self):
        content = build_new_page(
            title="Carga por WhatsApp",
            sidebar_title="WhatsApp",
            description="Mandá facturas por WhatsApp.",
            icon="message-circle",
            diataxis="how-to",
            body_mdx="Mandá la foto al número de tu local.\n\n## Cómo activarlo\n\nEntrá a **Configuración**.",
        )
        page = parse_page(content)
        self.assertIn('tag: "NEW"', page.frontmatter)
        self.assertEqual(page.headings, ["Cómo activarlo"])


class TestReviewRegressions(unittest.TestCase):
    """One test per defect found in the KAI-403 pre-merge review.

    Each of these was a reproduced escape from a gate that the docs and the
    commit message claimed was airtight. They are grouped here so the next
    person who loosens one of those gates gets a named failure.
    """

    def test_section_body_may_not_smuggle_a_heading(self):
        # Escaped `validate_body`, then became a real section on the next parse —
        # duplicating an existing heading and, across runs, splitting a Mintlify
        # component's opener from its closer.
        body = "Texto de la seccion para tu local.\n\n## Seccion colada\n\nOtro parrafo."
        self.assertTrue(any("own heading" in p for p in validate_body(body)))

    def test_new_page_body_may_contain_headings(self):
        # The same check must not fire on a whole page, which is *made* of them.
        body = "Introduccion para tu local.\n\n## Como activarlo\n\nEntra a **Configuracion**."
        self.assertEqual(validate_body(body, allow_headings=True), [])

    def test_smuggled_heading_cannot_survive_apply_actions(self):
        page = parse_page(PAGE)
        before = page.render()
        page, applied, rejected = apply_actions(page, [{
            "type": "replace_section",
            "heading": "Elegí cómo cargarla",
            "body_mdx": "A mano o por foto.\n\n## Qué pasa después de cargarla\n\nDuplicada.",
        }])
        self.assertEqual(applied, [])
        self.assertTrue(rejected)
        self.assertEqual(page.render(), before)
        self.assertEqual(page.headings.count("Qué pasa después de cargarla"), 1)

    def test_bare_and_unlisted_code_fences_are_rejected(self):
        # The old pattern enumerated languages, so ```/```text/```yaml walked in —
        # and a fence is what lets a `## ` line hide from the heading check.
        for fence in ("```", "```text", "```yaml", "```csv"):
            body = f"Mira este ejemplo para tu local:\n{fence}\ndato\n```"
            self.assertTrue(
                any("code fence" in p for p in validate_body(body)), fence
            )

    def test_diff_block_separator_survives_a_patch_containing_dashes(self):
        # Real unified diffs contain `--- a/file`, and a removed SQL comment line
        # renders as `--- fetch totals`. Splitting on `--- ` silently dropped
        # everything after such a line.
        patch = "@@ -1,3 +1,3 @@\n-const q = `\n--- fetch monthly totals\n-`\n+const q = 'x'\n"
        diff = select_release_diff([
            {"filename": "lib/services/revenue.ts", "patch": patch},
            {"filename": "components/x.tsx", "patch": "OTHER"},
        ])["text"]
        scoped = scope_diff_to_paths(diff, ["lib/services/revenue.ts"])
        self.assertIn("fetch monthly totals", scoped)
        self.assertIn("const q = 'x'", scoped)
        self.assertNotIn("OTHER", scoped)

    def test_frontmatter_cannot_be_terminated_by_a_model_supplied_scalar(self):
        # `description` carrying a newline + `---` closed the block early; the
        # round-trip parse check accepted it because FRONTMATTER_RE is non-greedy.
        content = build_new_page(
            title='Resumen "rapido"',
            sidebar_title="Resumen",
            description="linea1\n---\ndescripcion colada",
            icon="message-circle",
            diataxis="how-to",
            body_mdx="Intro para tu local.\n\n## Como usarlo\n\nEntra a **Configuracion**.",
        )
        page = parse_page(content)
        # The invariant is that no *line* is a bare `---` (only that closes the
        # block) and that no scalar carries a raw `"`. A collapsed `---` sitting
        # inside a quoted value is inert.
        self.assertNotIn("---", page.frontmatter.splitlines())
        self.assertNotIn('"rapido"', page.frontmatter)
        self.assertIn("descripcion colada", page.frontmatter)
        self.assertEqual(page.headings, ["Como usarlo"])
        for line in page.frontmatter.splitlines():
            key, _, value = line.partition(": ")
            self.assertTrue(value.startswith('"') and value.endswith('"'), line)
            self.assertNotIn('"', value[1:-1], line)

    def test_icon_is_restricted_to_a_safe_charset(self):
        content = build_new_page(
            title="X", sidebar_title="X", description="Y",
            icon='receipt" bad: "yes', diataxis="how-to",
            body_mdx="Intro para tu local.\n\n## Seccion\n\nCuerpo suficiente.",
        )
        self.assertIn('icon: "receiptbadyes"', content)

    def test_sanitize_scalar_collapses_whitespace_and_quotes(self):
        self.assertEqual(sanitize_scalar('a\n b  "c"'), "a b 'c'")

    def test_changelog_summary_goes_through_the_same_gate_as_a_page(self):
        # This path had no validation at all: every banned term reached a public
        # page, and a literal </Update> let the model forge a second dated entry.
        summary, problems = vet_changelog_summary(
            "Mejoras varias.\n</Update>\n<Update label=\"2020-01-01\">Entrada falsa</Update>"
        )
        self.assertTrue(problems)
        self.assertNotIn("Entrada falsa", summary)

    def test_changelog_summary_rejects_internal_vocabulary(self):
        summary, problems = vet_changelog_summary("El webhook de Postgres ya corre en tu local.")
        self.assertTrue(problems)
        self.assertNotIn("Postgres", summary)

    def test_changelog_summary_accepts_clean_prose_unchanged(self):
        text = "Ahora podés aprobar facturas desde el panel de tu local."
        self.assertEqual(vet_changelog_summary(text), (text, []))

    def test_link_text_cannot_break_out_of_the_markdown_link(self):
        self.assertEqual(escape_link_text("Rese[n]as\ny  mas"), "Rese(n)as y mas")

    def test_spanish_plural_database_is_caught(self):
        self.assertTrue(validate_body("Kairos guarda todo en bases de datos de tu local."))


CHANGELOG = '''---
title: "Changelog"
description: "Novedades de producto."
---

{/* diataxis: reference */}

Cada entrada resume qué cambió en Kairos y qué página lo cubre.

<Update label="2026-07-01" description="Actualización automática">
  Entrada vieja.
</Update>
'''


class TestChangelogWriting(unittest.TestCase):
    """`append_changelog_entry` writes to a public page and had zero coverage.

    Its only caller sits inside a try/except that does not catch NameError, so an
    import missed during a refactor was a guaranteed crash on every publishing
    run while the rest of the suite stayed green. These tests call it for real.
    """

    def _write(self, content: str | None) -> str:
        path = os.path.join(tempfile.mkdtemp(), "changelog.mdx")
        if content is not None:
            with open(path, "w") as f:
                f.write(content)
        return path

    def test_entry_lands_above_the_newest_existing_update(self):
        path = self._write(CHANGELOG)
        seeded = append_changelog_entry(path, "Resumen nuevo del local.", "2026-07-28", [])
        self.assertFalse(seeded)
        with open(path) as f:
            out = f.read()
        self.assertLess(out.index('label="2026-07-28"'), out.index('label="2026-07-01"'))

    def test_intro_prose_stays_above_every_entry(self):
        # The KAI-246 insertion point put entries right after the diataxis marker,
        # which pushed this paragraph down between entries on the live site.
        path = self._write(CHANGELOG)
        append_changelog_entry(path, "Resumen nuevo del local.", "2026-07-28", [])
        with open(path) as f:
            out = f.read()
        self.assertLess(out.index("Cada entrada resume"), out.index("<Update"))

    def test_repeated_entries_stay_newest_first(self):
        path = self._write(CHANGELOG)
        append_changelog_entry(path, "Primera del local.", "2026-07-28", [])
        append_changelog_entry(path, "Segunda del local.", "2026-07-29", [])
        with open(path) as f:
            out = f.read()
        self.assertLess(out.index('label="2026-07-29"'), out.index('label="2026-07-28"'))
        self.assertLess(out.index("Cada entrada resume"), out.index("<Update"))

    def test_missing_file_is_seeded_and_reported(self):
        path = self._write(None)
        seeded = append_changelog_entry(path, "Resumen del local.", "2026-07-28", [])
        self.assertTrue(seeded)
        with open(path) as f:
            out = f.read()
        self.assertIn('title: "Changelog"', out)
        self.assertIn("Resumen del local.", out)

    def test_page_links_are_rendered_and_escaped(self):
        path = self._write(CHANGELOG)
        append_changelog_entry(
            path, "Resumen del local.", "2026-07-28",
            [("Rese[n]as", "features/reputation"), ("Facturas", "features/invoices")],
        )
        with open(path) as f:
            out = f.read()
        self.assertIn("[Rese(n)as](/features/reputation)", out)
        self.assertIn("[Facturas](/features/invoices)", out)

    def test_fallback_template_passes_its_own_gate(self):
        # If the seeded file could not itself be re-read, the self-heal path would
        # produce a page the site cannot render.
        self.assertIn("diataxis", CHANGELOG_FALLBACK)


class TestPrBody(unittest.TestCase):
    def _body(self, reports, deferred=(), caps=()) -> str:
        return build_pr_body(
            "Kairos-rest/app", "qwen3.6", "b" * 40, "a" * 40, 3,
            {"included_paths": ["messages/es.json"], "dropped_paths": ["lib/services/x.ts"],
             "ignored_count": 7, "no_patch_paths": ["components/logo.png"]},
            list(reports), list(deferred), list(caps),
        )

    def test_every_outcome_is_reported_distinctly(self):
        body = self._body([
            {"slug": "invoices", "outcome": "edited", "applied": ["replace_section 'X'"], "rejected": ["bad body"]},
            {"slug": "whatsapp", "outcome": "created", "title": "WhatsApp", "nav_note": "grupo raro"},
            {"slug": "cost", "outcome": "rejected", "detail": "leaks Prisma"},
            {"slug": "waste", "outcome": "failed", "detail": "NAN inalcanzable"},
            {"slug": "chat", "outcome": "skipped", "detail": "sin cambios"},
        ])
        for expected in ("features/invoices.mdx", "bad body", "WhatsApp", "grupo raro",
                         "leaks Prisma", "NAN inalcanzable", "sin cambios"):
            self.assertIn(expected, body)

    def test_caps_and_budget_drops_are_never_silent(self):
        body = self._body([], deferred=["cost"], caps=["Límite de 4 página(s) por corrida alcanzado."])
        self.assertIn("lib/services/x.ts", body)      # dropped by budget
        self.assertIn("components/logo.png", body)    # no patch from GitHub
        self.assertIn("cost", body)                   # deferred
        self.assertIn("Límite de 4", body)            # cap
        self.assertIn("7", body)                      # ignored count

    def test_body_links_the_real_commit_range(self):
        body = self._body([])
        self.assertIn(f"compare/{'a' * 40}...{'b' * 40}", body)

    def test_empty_report_says_so_rather_than_looking_complete(self):
        self.assertIn("Ninguna página", self._body([]))


class TestNav(unittest.TestCase):
    def _config(self) -> dict:
        return {
            "navigation": {
                "pages": [
                    {"group": "Primeros pasos", "pages": ["index"]},
                    {"group": "Facturas y proveedores", "pages": ["features/invoices"]},
                ]
            }
        }

    def test_group_names_lists_display_names(self):
        self.assertEqual(group_names(self._config()), ["Primeros pasos", "Facturas y proveedores"])

    def test_add_page_lands_in_the_requested_group(self):
        config, note = add_page(self._config(), "features/whatsapp", "Facturas y proveedores")
        self.assertIsNone(note)
        self.assertIn("features/whatsapp", config["navigation"]["pages"][1]["pages"])

    def test_unknown_group_falls_back_to_last_and_reports_it(self):
        config, note = add_page(self._config(), "features/whatsapp", "Grupo inexistente")
        self.assertIsNotNone(note)
        self.assertIn("features/whatsapp", config["navigation"]["pages"][-1]["pages"])

    def test_adding_an_existing_page_is_a_noop(self):
        config, note = add_page(self._config(), "features/invoices", "Primeros pasos")
        self.assertIsNone(note)
        self.assertEqual(config["navigation"]["pages"][0]["pages"], ["index"])

    def test_page_exists_finds_pages_in_any_group(self):
        self.assertTrue(page_exists(self._config(), "features/invoices"))
        self.assertFalse(page_exists(self._config(), "features/nope"))


def _bound_names(node: ast.AST) -> set[str]:
    """Names a scope binds: params, assignments, loop/with/except targets, defs."""
    bound: set[str] = set()

    def add_args(args: ast.arguments) -> None:
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            bound.add(a.arg)
        for a in (args.vararg, args.kwarg):
            if a:
                bound.add(a.arg)

    own_args = getattr(node, "args", None)
    if isinstance(own_args, ast.arguments):
        add_args(own_args)

    for child in ast.walk(node):
        # Do not descend into a nested function's own bindings; it gets its own pass.
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child is not node:
            bound.add(child.name)
            continue
        # Lambdas get no separate pass, so treat their params as bound here —
        # otherwise the first uniquely-named lambda parameter fails the suite.
        if isinstance(child, ast.Lambda):
            add_args(child.args)
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            bound.add(child.id)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            bound.add(child.name)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(child, (ast.Global, ast.Nonlocal)):
            bound.update(child.names)
    return bound


class TestNoUndefinedNames(unittest.TestCase):
    """Every name a function reads must resolve to a binding or a builtin.

    This exists because moving `append_changelog_entry` between modules left it
    calling `os` and `log`, neither of which was defined in its new home. Nothing
    caught it: the modules imported fine, 53 tests passed, and the `NameError`
    only fired when the function ran — on a cron, in production, inside a
    try/except that does not catch `NameError`.

    AST rather than bytecode: `os.path.exists` is one `Name` load plus two
    `Attribute` nodes, so attribute names never masquerade as globals. It needs no
    dependency, which is what makes it runnable on the VPS.
    """

    MODULES = ("docs-sync.py", "docs_changelog.py", "docs_diff.py",
               "docs_mdx.py", "docs_nav.py", "docs_prompts.py")

    def test_every_name_read_resolves(self):
        import builtins

        here = os.path.dirname(os.path.abspath(__file__))
        findings: list[str] = []

        for filename in self.MODULES:
            with open(os.path.join(here, filename)) as f:
                tree = ast.parse(f.read(), filename)
            module_scope = _bound_names(tree) | set(vars(builtins)) | {"__file__", "__name__"}

            functions = [n for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for fn in functions:
                # Enclosing-scope names: this file's functions are either top level
                # or nested one deep, so module scope plus own bindings covers it.
                # A nested def's params are added too, since the Load walk below
                # descends into it and they would otherwise look unresolved.
                visible = module_scope | _bound_names(fn)
                for inner in ast.walk(fn):
                    if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) and inner is not fn:
                        visible |= _bound_names(inner)
                for child in ast.walk(fn):
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                        if child.id not in visible:
                            findings.append(f"{filename}:{child.lineno} {fn.name} -> {child.id}")

        self.assertEqual(findings, [], f"unresolved names: {findings}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
