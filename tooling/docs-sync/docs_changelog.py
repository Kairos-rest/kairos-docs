"""Changelog writing and PR-report rendering for the docs sync pipeline.

Split out from the job because both are pure text assembly over data the run
already has: given the reports, produce the entry that goes on the public
changelog page and the body that tells a human reviewer exactly what the run did,
deferred, and refused to do.
"""

from __future__ import annotations

import os
import re

from docs_mdx import validate_body

CHANGELOG_FALLBACK = (
    '---\ntitle: "Changelog"\ndescription: "Novedades de producto reflejadas en '
    'esta documentación."\n---\n\n{/* diataxis: reference */}\n\n'
    "Cada entrada resume qué cambió en Kairos y qué página de la documentación lo "
    "cubre.\n"
)


# Returned unvalidated when a drafted summary is rejected, which is safe only
# because it is a constant written here. Do not make it env-configurable without
# running it through `validate_body` first.
FALLBACK_SUMMARY = "Actualizamos la documentación de las funcionalidades que cambiaron en esta versión."


def vet_changelog_summary(summary_mdx: str) -> tuple[str, list[str]]:
    """Gate the changelog entry the same way a page section is gated.

    The entry is the single most-written model-authored artifact on a public
    site, and it used to be the one thing that reached `main` with no validation
    at all — free to use every banned term, and free to emit a literal
    `</Update>` that closes the block early and lets the rest of the text pose as
    a separate, fabricated dated entry.

    Returns `(summary, problems)`. On any problem the caller gets the terse
    fallback text instead, so a rejected draft still records that docs moved.

    Headings are allowed here, unlike in a feature-page section. The heading ban
    exists because `parse_page` does section surgery on feature pages, so a `## `
    inside a section body becomes a real heading on the next parse. changelog.mdx
    is only ever appended to — never parsed into sections — so a heading inside an
    `<Update>` block is inert, and the entries already published use them. Banning
    them here would buy no safety and send most runs to the fallback text.
    """
    problems = validate_body(summary_mdx, allow_headings=True)
    # `validate_body` balances <Update> pairs, but a *balanced* injected pair is
    # still a forged entry — reject the tag outright, it is ours to write.
    if re.search(r"</?Update\b", summary_mdx):
        problems.append("summary must not contain <Update> tags")
    if problems:
        return FALLBACK_SUMMARY, problems
    return summary_mdx.strip(), []


def escape_link_text(text: str) -> str:
    """Neutralise brackets in markdown link text so a title cannot break out."""
    return re.sub(r"\s+", " ", text).replace("[", "(").replace("]", ")").strip()


def append_changelog_entry(path: str, summary_mdx: str, date_label: str,
                           page_links: list[tuple[str, str]]) -> bool:
    """Prepend a dated `<Update>` block to changelog.mdx.

    Inserted immediately before the first existing `<Update>` so entries stay
    newest-first *and* the page's intro prose stays above them. (The KAI-246
    version inserted right after the Diátaxis marker, which pushed the intro
    paragraph down between entries — see the orphaned line this release fixes.)

    Returns True when the file was missing and had to be seeded, so the caller
    can log that without this module needing the job's logger.
    """
    seeded = not os.path.exists(path)
    if seeded:
        # `main` may not have changelog.mdx yet — self-heal instead of crashing.
        content = CHANGELOG_FALLBACK
    else:
        with open(path) as f:
            content = f.read()

    body = summary_mdx.strip()
    if page_links:
        links = ", ".join(f"[{escape_link_text(title)}](/{ref})" for title, ref in page_links)
        body += f"\n\n  Páginas actualizadas: {links}"

    entry = (
        f'\n<Update label="{date_label}" description="Actualización automática">\n'
        f"  {body}\n"
        f"</Update>\n"
    )

    first_update = content.find("\n<Update ")
    if first_update != -1:
        insert_at = first_update
    else:
        marker_match = re.search(r"\{/\*\s*diataxis:[^}]*\*/\}\n", content)
        insert_at = marker_match.end() if marker_match else len(content.rstrip())
        content = content.rstrip() + "\n" if not marker_match else content

    with open(path, "w") as f:
        f.write(content[:insert_at] + entry + content[insert_at:])
    return seeded


def build_pr_body(app_repo: str, model: str, main_sha: str, compare_base: str,
                  num_commits: int, diff_meta: dict, reports: list[dict],
                  deferred: list[str], caps: list[str]) -> str:
    lines = [
        f"Borrador automático del pipeline de documentación. Resume "
        f"{num_commits} commit(s) desplegados a producción, rango "
        f"[`{compare_base[:7]}...{main_sha[:7]}`](https://github.com/{app_repo}/compare/{compare_base}...{main_sha}).",
        "",
        f"Generado por Labestia (`{model}`) en dos pasos: triage del diff y edición por página.",
        "**Requiere revisión humana. Nunca se mergea automáticamente.**",
        "",
        "## Páginas",
        "",
    ]

    if not reports:
        lines.append("Ninguna página de funcionalidad necesitó cambios en este rango.")
    for r in reports:
        slug = r["slug"]
        outcome = r["outcome"]
        if outcome == "edited":
            lines.append(f"- **`features/{slug}.mdx`** — editada: {', '.join(r['applied'])}")
            for rej in r.get("rejected", []):
                lines.append(f"  - descartado por el validador: {rej}")
        elif outcome == "created":
            lines.append(f"- **`features/{slug}.mdx`** — página nueva: {r['title']}")
            if r.get("nav_note"):
                lines.append(f"  - revisar ubicación en el menú: {r['nav_note']}")
        elif outcome == "rejected":
            lines.append(f"- `{slug}` — **rechazada por el validador**, requiere edición manual: {r['detail']}")
        elif outcome == "failed":
            lines.append(f"- `{slug}` — **falló**, requiere edición manual: {r['detail']}")
        else:
            lines.append(f"- `{slug}` — sin cambios: {r.get('detail', '')}")

    if deferred:
        lines += [
            "",
            f"## Diferidas a la próxima corrida ({len(deferred)})",
            "",
            "Se alcanzó el límite de páginas por corrida o el tiempo máximo. "
            "Estas páginas siguen desactualizadas y hay que revisarlas a mano:",
            "",
        ]
        lines += [f"- `{s}`" for s in deferred]

    lines += ["", "## Cobertura del diff", ""]
    lines.append(f"- Archivos analizados: {len(diff_meta['included_paths'])}")
    lines.append(f"- Archivos descartados por no ser visibles al cliente: {diff_meta['ignored_count']}")
    if diff_meta["dropped_paths"]:
        lines.append(
            f"- **Recortados por presupuesto de contexto ({len(diff_meta['dropped_paths'])})**: "
            + ", ".join(f"`{p}`" for p in diff_meta["dropped_paths"][:20])
        )
    if diff_meta["no_patch_paths"]:
        lines.append(
            f"- Sin diff disponible de GitHub ({len(diff_meta['no_patch_paths'])}): "
            + ", ".join(f"`{p}`" for p in diff_meta["no_patch_paths"][:20])
        )
    for cap in caps:
        lines.append(f"- {cap}")

    return "\n".join(lines)
