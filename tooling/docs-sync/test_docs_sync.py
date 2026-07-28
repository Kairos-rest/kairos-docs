#!/usr/bin/env python3
"""Unit tests for the docs sync pipeline's pure logic.

Deliberately stdlib-only (`python3 -m unittest`) so they run on the VPS with no
install step. Nothing here touches the network — the modules under test were
split out of `docs-sync.py` precisely so the diff selection, MDX surgery and nav
edits could be tested without NAN or GitHub.

Run: python3 -m unittest discover -s tooling/docs-sync -p 'test_*.py'
"""

from __future__ import annotations

import unittest

from docs_diff import is_customer_facing, scope_diff_to_paths, select_release_diff
from docs_mdx import (
    PageParseError,
    apply_actions,
    build_new_page,
    parse_page,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
