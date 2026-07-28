#!/usr/bin/env python3
"""Kairos docs sync — reviews the release code, then updates the docs.

Polls `Kairos-rest/app`'s production branch, and for every new commit range it
does two passes over NAN (qwen3.6):

  1. Triage — given the commit subjects *and the filtered release diff*, decide
     whether anything is customer-visible, draft the changelog entry, and name
     the feature pages that need to change (or that need to exist).
  2. Per-page edit — for each named page, given the page's current MDX and the
     slice of the diff behind it, propose section-level changes. New pages get a
     drafted body plus a `docs.json` nav entry.

It then opens (or force-pushes) a single PR against `Kairos-rest/kairos-docs`
containing the changed feature pages, the nav change and the changelog entry.
It never touches `main` directly and never auto-merges.

Why two passes instead of one: a single call asked to both summarise a release
and rewrite prose ends up doing neither well, and it makes the page edits
impossible to attribute. Splitting them means the changelog is drafted from the
whole release while each page edit is grounded only in the files that actually
touch that page.

Original changelog-only version was KAI-246; this is the KAI-403 rewrite that
adds diff review and feature-page editing. History: the old version fed the
model commit subject lines only, asked it for `touched_feature_slugs`, then
discarded that field and hard-coded `git add changelog.mdx` — so feature pages
never moved after the initial content drop.

Safety properties kept from KAI-246:
  * Fail-soft — if GitHub or NAN is unreachable the run is skipped and the
    cursor is NOT advanced, so the range is retried on the next trigger.
  * Single-flight via flock; an overlapping run is a no-op, not a queue.
  * One branch (`docs-sync/auto`), always cut fresh from `main`, force-pushed —
    repeated deploys update the open PR instead of stacking PRs.
  * Every cap (commits, diff budget, pages per run, wall-clock) is logged and
    reported in the PR body. Nothing is silently truncated.

Safety properties added here:
  * The model never writes a page file. It proposes changes to named sections
    and `docs_mdx.apply_actions` splices them in, so frontmatter, the Diátaxis
    marker and unmentioned sections survive untouched.
  * Every model-authored body passes `docs_mdx.validate_body` before it lands —
    internal vocabulary (Prisma, Redis, repo paths, code fences) and unbalanced
    Mintlify components are rejected per-section, and the rejection is reported
    in the PR body instead of failing the run.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from docs_diff import scope_diff_to_paths, select_release_diff  # noqa: E402
from docs_mdx import (  # noqa: E402
    PageParseError,
    apply_actions,
    build_new_page,
    parse_page,
    set_frontmatter_key,
    validate_body,
)
from docs_nav import add_page, group_names, load_nav, page_exists, save_nav  # noqa: E402
from docs_prompts import EDIT_SYSTEM, NEW_PAGE_SYSTEM, TRIAGE_SYSTEM  # noqa: E402

APP_REPO = os.environ.get("DOCS_SYNC_APP_REPO", "Kairos-rest/app")
DOCS_REPO = os.environ.get("DOCS_SYNC_DOCS_REPO", "Kairos-rest/kairos-docs")
DOCS_BRANCH = os.environ.get("DOCS_SYNC_BRANCH", "docs-sync/auto")
BASE_BRANCH = os.environ.get("DOCS_SYNC_BASE_BRANCH", "main")

# State lives beside the checkout on the VPS, not in the repo — the repo holds
# the code, the VPS holds the cursor and the credentials.
STATE_DIR = os.environ.get("DOCS_SYNC_HOME") or os.path.expanduser("~/kairos-docs-pipeline")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOCK_FILE = os.path.join(STATE_DIR, ".docs-sync.lock")
WORKDIR = os.path.join(STATE_DIR, "kairos-docs-checkout")

MAX_COMMITS = int(os.environ.get("DOCS_SYNC_MAX_COMMITS", "60"))
MAX_PAGES_PER_RUN = int(os.environ.get("DOCS_SYNC_MAX_PAGES", "4"))
# qwen3.6 thinking-mode latency is high-variance and this runs on a 30-minute
# cron, not a user-facing request.
NAN_TIMEOUT = float(os.environ.get("DOCS_SYNC_NAN_TIMEOUT", "180"))
# Whole-run ceiling. Worst case is 1 triage + MAX_PAGES_PER_RUN edit calls; the
# deadline stops us starting a call that would overlap the next cron tick.
DEADLINE_SECONDS = float(os.environ.get("DOCS_SYNC_DEADLINE", "1200"))
# A bare urllib UA trips Cloudflare bot-fight-mode (error 1010) on NAN's endpoint.
USER_AGENT = "kairos-docs-pipeline/2.0"

GH_READ_TOKEN = os.environ["GH_APP_READ_TOKEN"]  # read-only PAT, scoped to Kairos-rest/app
GH_DOCS_TOKEN = os.environ["GH_DOCS_WRITE_TOKEN"]  # fine-grained PAT, scoped to Kairos-rest/kairos-docs only
NAN_API_URL = os.environ.get("NAN_API_URL", "https://api.nan.builders/v1")
NAN_API_KEY = os.environ["NAN_API_KEY"]
NAN_MODEL = os.environ.get("NAN_MODEL", "qwen3.6")

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
VALID_DIATAXIS = ("tutorial", "how-to", "reference", "explanation")

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------- GitHub / state


def gh_api(path: str, token: str, method: str = "GET", body: dict | None = None) -> dict | list:
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def advance_cursor(sha: str) -> None:
    save_state({"last_sha": sha, "updated_at": datetime.now(timezone.utc).isoformat()})


# ------------------------------------------------------------------------- NAN


def call_nan(system: str, user: str) -> dict:
    """One NAN chat completion, parsed as JSON.

    Returns `{}` when the model answers with something that is not the JSON
    object we asked for. That is a content failure, not a transport failure, so
    it must not look like "NAN unreachable" to the caller — a `{}` here means
    "the model had nothing usable to say", and the run continues.
    """
    payload = {
        "model": NAN_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        f"{NAN_API_URL}/chat/completions",
        data=json.dumps(payload).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {NAN_API_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(req, timeout=NAN_TIMEOUT) as resp:
        raw = json.loads(resp.read().decode())

    try:
        text = raw["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        log("NAN response had no message content")
        return {}

    if text.startswith("```"):
        text = text.strip("`")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        log(f"NAN returned no JSON object. Raw: {text[:300]}")
        return {}
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        log(f"NAN returned malformed JSON. Raw: {text[start:start + 300]}")
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ------------------------------------------------------------------------- git


def run(cmd: list[str], cwd: str | None = None) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{r.stderr}")
    return r.stdout.strip()


def ensure_docs_checkout() -> None:
    remote = f"https://x-access-token:{GH_DOCS_TOKEN}@github.com/{DOCS_REPO}.git"
    if not os.path.isdir(os.path.join(WORKDIR, ".git")):
        run(["git", "clone", remote, WORKDIR])
    else:
        run(["git", "remote", "set-url", "origin", remote], cwd=WORKDIR)
        run(["git", "fetch", "origin"], cwd=WORKDIR)
    run(["git", "checkout", BASE_BRANCH], cwd=WORKDIR)
    run(["git", "reset", "--hard", f"origin/{BASE_BRANCH}"], cwd=WORKDIR)
    # Always branch fresh from the base — avoids reconciling stale bot-branch drift.
    run(["git", "checkout", "-B", DOCS_BRANCH], cwd=WORKDIR)


# ------------------------------------------------------------------- changelog

CHANGELOG_FALLBACK = (
    '---\ntitle: "Changelog"\ndescription: "Novedades de producto reflejadas en '
    'esta documentación."\n---\n\n{/* diataxis: reference */}\n\n'
    "Cada entrada resume qué cambió en Kairos y qué página de la documentación lo "
    "cubre.\n"
)


def append_changelog_entry(summary_mdx: str, date_label: str, page_links: list[tuple[str, str]]) -> None:
    """Prepend a dated `<Update>` block to changelog.mdx.

    Inserted immediately before the first existing `<Update>` so entries stay
    newest-first *and* the page's intro prose stays above them. (The KAI-246
    version inserted right after the Diátaxis marker, which pushed the intro
    paragraph down between entries — see the orphaned line this release fixes.)
    """
    path = os.path.join(WORKDIR, "changelog.mdx")
    if not os.path.exists(path):
        # `main` may not have changelog.mdx yet — self-heal instead of crashing.
        log("changelog.mdx missing on base branch, seeding a minimal one")
        content = CHANGELOG_FALLBACK
    else:
        with open(path) as f:
            content = f.read()

    body = summary_mdx.strip()
    if page_links:
        links = ", ".join(f"[{title}](/{ref})" for title, ref in page_links)
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


# ------------------------------------------------------------------ page edits


def page_path(slug: str) -> str:
    return os.path.join(WORKDIR, "features", f"{slug}.mdx")


def existing_slugs() -> list[str]:
    features_dir = os.path.join(WORKDIR, "features")
    if not os.path.isdir(features_dir):
        return []
    return sorted(f[:-4] for f in os.listdir(features_dir) if f.endswith(".mdx"))


def page_title(slug: str) -> str:
    """Sidebar title if present, else the H1 title, else the slug."""
    try:
        with open(page_path(slug)) as f:
            page = parse_page(f.read())
    except (OSError, PageParseError):
        return slug
    for key in ("sidebarTitle", "title"):
        m = re.search(rf'^{key}\s*:\s*"?([^"\n]+)"?\s*$', page.frontmatter, re.MULTILINE)
        if m:
            return m.group(1).strip()
    return slug


def edit_existing_page(slug: str, target: dict, diff_text: str) -> dict:
    """Run the per-page edit pass. Returns a report dict for the PR body."""
    path = page_path(slug)
    with open(path) as f:
        source = f.read()
    page = parse_page(source)

    scoped = scope_diff_to_paths(diff_text, target.get("paths") or [])
    user = (
        f"Página: features/{slug}.mdx\n"
        f"Motivo detectado en el triage: {target.get('reason', '(sin motivo)')}\n\n"
        f"Encabezados de nivel 2 existentes (usá estos textos exactos):\n"
        + "\n".join(f"- {h}" for h in page.headings)
        + "\n\nContenido actual de la página:\n"
        + page.render()
        + "\n\nDiff del código relacionado:\n"
        + scoped
    )
    result = call_nan(EDIT_SYSTEM, user)
    actions = result.get("actions")
    if not isinstance(actions, list) or not actions:
        return {"slug": slug, "outcome": "skipped", "detail": "model proposed no usable actions"}

    page, applied, rejected = apply_actions(page, actions)
    if not applied:
        return {
            "slug": slug,
            "outcome": "skipped",
            "detail": "; ".join(rejected) if rejected else "model proposed no change",
        }

    # An updated page is novel again — AGENTS.md wants the NEW tag on it, and a
    # human drops the tag later once it stops being news.
    page.frontmatter = set_frontmatter_key(page.frontmatter, "tag", "NEW")
    with open(path, "w") as f:
        f.write(page.render())

    return {
        "slug": slug,
        "outcome": "edited",
        "applied": applied,
        "rejected": rejected,
        "file": f"features/{slug}.mdx",
    }


def create_new_page(slug: str, target: dict, diff_text: str, nav_config: dict) -> dict:
    """Draft a brand-new feature page and wire it into the nav."""
    title = (target.get("title") or "").strip()
    description = (target.get("description") or "").strip()
    if not title or not description:
        return {"slug": slug, "outcome": "rejected", "detail": "new page missing title or description"}

    diataxis = (target.get("diataxis") or "how-to").strip()
    if diataxis not in VALID_DIATAXIS:
        diataxis = "how-to"

    scoped = scope_diff_to_paths(diff_text, target.get("paths") or [])
    user = (
        f"Página nueva: features/{slug}.mdx\n"
        f"Título: {title}\nDescripción: {description}\n"
        f"Motivo: {target.get('reason', '(sin motivo)')}\n\n"
        f"Diff del código que la implementa:\n{scoped}"
    )
    result = call_nan(NEW_PAGE_SYSTEM, user)
    body = (result.get("body_mdx") or "").strip()
    if not body:
        return {"slug": slug, "outcome": "skipped", "detail": "model declined to draft the page"}

    problems = validate_body(body)
    if problems:
        return {"slug": slug, "outcome": "rejected", "detail": "; ".join(problems)}
    if not re.search(r"^##\s+\S", body, re.MULTILINE):
        return {"slug": slug, "outcome": "rejected", "detail": "drafted page has no level-2 sections"}

    content = build_new_page(
        title=title,
        sidebar_title=(target.get("sidebar_title") or title).strip(),
        description=description,
        icon=(target.get("icon") or "file-text").strip(),
        diataxis=diataxis,
        body_mdx=body,
    )
    # Round-trip through the parser: if we cannot re-read what we just wrote,
    # the site cannot render it either.
    try:
        parse_page(content)
    except PageParseError as e:
        return {"slug": slug, "outcome": "rejected", "detail": f"drafted page unparseable: {e}"}

    os.makedirs(os.path.join(WORKDIR, "features"), exist_ok=True)
    with open(page_path(slug), "w") as f:
        f.write(content)

    _, note = add_page(nav_config, f"features/{slug}", (target.get("nav_group") or "").strip())
    return {
        "slug": slug,
        "outcome": "created",
        "file": f"features/{slug}.mdx",
        "nav_note": note,
        "title": title,
    }


# ------------------------------------------------------------------------- PR


def build_pr_body(main_sha: str, cursor: str, num_commits: int, diff_meta: dict,
                  reports: list[dict], deferred: list[str], caps: list[str]) -> str:
    lines = [
        f"Borrador automático del pipeline de documentación. Resume "
        f"{num_commits} commit(s) desplegados a producción, rango "
        f"[`{cursor[:7]}...{main_sha[:7]}`](https://github.com/{APP_REPO}/compare/{cursor}...{main_sha}).",
        "",
        f"Generado por NAN (`{NAN_MODEL}`) en dos pasos: triage del diff y edición por página.",
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


def commit_and_push(paths: list[str], main_sha: str) -> None:
    # Explicit paths, never `git add -A`: the checkout is shared with the state
    # dir's siblings and a stray file must not ride along into a docs PR.
    run(["git", "add", "--"] + paths, cwd=WORKDIR)
    if not run(["git", "status", "--porcelain"], cwd=WORKDIR):
        raise RuntimeError("nothing staged after applying changes")
    run([
        "git", "-c", "user.email=docs-sync@kairos.rest", "-c", "user.name=Kairos Docs Sync",
        "commit", "-m", f"docs: auto-draft docs + changelog for {main_sha[:7]}",
    ], cwd=WORKDIR)
    run(["git", "push", "--force-with-lease", "origin", f"HEAD:{DOCS_BRANCH}"], cwd=WORKDIR)


def open_or_update_pr(body: str) -> None:
    owner = DOCS_REPO.split("/")[0]
    existing = gh_api(
        f"/repos/{DOCS_REPO}/pulls?head={owner}:{DOCS_BRANCH}&state=open", GH_DOCS_TOKEN
    )
    if existing:
        number = existing[0]["number"]
        # Refresh the body too — the previous run's report described a different
        # commit range and would otherwise mislead the reviewer.
        gh_api(f"/repos/{DOCS_REPO}/pulls/{number}", GH_DOCS_TOKEN, method="PATCH", body={"body": body})
        log(f"PR already open (#{number}), pushed new commit and refreshed the body")
        return
    pr = gh_api(
        f"/repos/{DOCS_REPO}/pulls",
        GH_DOCS_TOKEN,
        method="POST",
        body={
            "title": "docs: draft update from latest production deploy",
            "head": DOCS_BRANCH,
            "base": BASE_BRANCH,
            "body": body,
        },
    )
    log(f"opened PR #{pr['number']}: {pr['html_url']}")


# ------------------------------------------------------------------------ main


def main() -> int:
    os.makedirs(STATE_DIR, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another run is in progress, skipping")
        return 0

    started = time.monotonic()
    caps: list[str] = []

    state = load_state()
    cursor = state.get("last_sha")

    try:
        head = gh_api(f"/repos/{APP_REPO}/commits/{BASE_BRANCH}", GH_READ_TOKEN)
        main_sha = head["sha"]
    except (urllib.error.URLError, KeyError, TypeError) as e:
        log(f"GitHub unreachable, skipping run without advancing cursor: {e}")
        return 0

    if cursor is None:
        log(f"no cursor on disk — bootstrapping to current {BASE_BRANCH} ({main_sha[:7]}), no draft this run")
        advance_cursor(main_sha)
        return 0

    if cursor == main_sha:
        log("no new commits since last run")
        return 0

    try:
        compare = gh_api(f"/repos/{APP_REPO}/compare/{cursor}...{main_sha}", GH_READ_TOKEN)
    except (urllib.error.URLError, KeyError, TypeError) as e:
        log(f"GitHub compare failed, skipping run without advancing cursor: {e}")
        return 0

    commits = compare.get("commits", [])
    if not commits:
        log("compare returned zero commits, advancing cursor")
        advance_cursor(main_sha)
        return 0

    if len(commits) > MAX_COMMITS:
        omitted = len(commits) - MAX_COMMITS
        commits = commits[-MAX_COMMITS:]
        msg = f"Rango recortado: se omitieron {omitted} commits más antiguos (límite {MAX_COMMITS})."
        caps.append(msg)
        log(f"commit range capped: {omitted} older commits omitted, kept newest {MAX_COMMITS}")

    diff_meta = select_release_diff(compare.get("files", []))
    log(
        f"diff selected: {len(diff_meta['included_paths'])} file(s) in, "
        f"{diff_meta['ignored_count']} ignored, {len(diff_meta['dropped_paths'])} dropped by budget, "
        f"{len(diff_meta['no_patch_paths'])} without a patch"
    )
    if diff_meta["dropped_paths"]:
        caps.append(
            f"Presupuesto de contexto: se recortaron {len(diff_meta['dropped_paths'])} archivo(s) del diff."
        )
    # GitHub's compare endpoint returns at most 300 files.
    if len(compare.get("files", [])) >= 300:
        caps.append("GitHub devolvió el máximo de 300 archivos en el compare: el rango puede estar incompleto.")
        log("compare hit GitHub's 300-file ceiling")

    try:
        features_resp = gh_api(f"/repos/{APP_REPO}/contents/docs/FEATURES.md?ref={BASE_BRANCH}", GH_READ_TOKEN)
        features_md = base64.b64decode(features_resp["content"]).decode()
    except Exception as e:  # noqa: BLE001 - index is a nice-to-have, never fatal
        log(f"could not fetch docs/FEATURES.md, continuing without it: {e}")
        features_md = ""

    # The checkout has to exist before triage so the model can be told which
    # pages and nav groups actually exist right now.
    try:
        ensure_docs_checkout()
    except (RuntimeError, OSError) as e:
        log(f"docs checkout failed, NOT advancing cursor (will retry next run): {e}")
        return 1

    nav_path = os.path.join(WORKDIR, "docs.json")
    nav_config = load_nav(nav_path)
    slugs = existing_slugs()

    commit_lines = "\n".join(
        f"- {c['sha'][:7]} {c['commit']['message'].splitlines()[0]}" for c in commits
    )
    triage_user = (
        f"Commits desplegados (rama {BASE_BRANCH} de la app):\n{commit_lines}\n\n"
        f"Páginas de documentación existentes (slugs válidos para kind 'edit'):\n"
        + ", ".join(slugs)
        + "\n\nGrupos de navegación existentes (valores válidos para nav_group):\n"
        + ", ".join(f'"{g}"' for g in group_names(nav_config))
        + f"\n\nÍndice interno de funcionalidades (referencia):\n{features_md[:4000]}"
        + f"\n\nDiff de los archivos que pueden afectar al cliente:\n{diff_meta['text']}"
    )

    try:
        triage = call_nan(TRIAGE_SYSTEM, triage_user)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log(f"NAN unreachable during triage, skipping run without advancing cursor: {e}")
        return 0

    changelog_mdx = (triage.get("changelog_mdx") or "").strip()
    raw_targets = triage.get("targets") or []
    if triage.get("no_changes") or (not changelog_mdx and not raw_targets):
        log(f"NAN found nothing doc-worthy in {len(commits)} commit(s), advancing cursor without a PR")
        advance_cursor(main_sha)
        return 0

    # Validate targets before spending a call on any of them.
    targets: list[dict] = []
    for t in raw_targets:
        if not isinstance(t, dict):
            continue
        slug = (t.get("slug") or "").strip().lower()
        kind = (t.get("kind") or "").strip()
        if not SLUG_RE.match(slug):
            log(f"dropping target with invalid slug {slug!r}")
            continue
        if kind == "edit" and slug not in slugs:
            log(f"dropping edit target for unknown page {slug!r}")
            continue
        if kind == "new" and (slug in slugs or page_exists(nav_config, f"features/{slug}")):
            log(f"target {slug!r} marked new but the page already exists, treating as edit")
            kind = "edit"
        if kind not in ("edit", "new"):
            log(f"dropping target {slug!r} with unknown kind {kind!r}")
            continue
        t["slug"], t["kind"] = slug, kind
        targets.append(t)

    # Deduplicate while preserving the model's ordering (most relevant first).
    seen: set[str] = set()
    ordered: list[dict] = []
    for t in targets:
        if t["slug"] in seen:
            continue
        seen.add(t["slug"])
        ordered.append(t)

    deferred: list[str] = []
    if len(ordered) > MAX_PAGES_PER_RUN:
        deferred = [t["slug"] for t in ordered[MAX_PAGES_PER_RUN:]]
        ordered = ordered[:MAX_PAGES_PER_RUN]
        caps.append(f"Límite de {MAX_PAGES_PER_RUN} página(s) por corrida alcanzado.")
        log(f"page cap reached: deferring {deferred}")

    reports: list[dict] = []
    changed_files: list[str] = []
    nav_dirty = False

    for t in ordered:
        slug = t["slug"]
        if time.monotonic() - started > DEADLINE_SECONDS:
            deferred.append(slug)
            log(f"deadline reached, deferring {slug}")
            continue
        try:
            if t["kind"] == "new":
                report = create_new_page(slug, t, diff_meta["text"], nav_config)
                if report["outcome"] == "created":
                    nav_dirty = True
            else:
                report = edit_existing_page(slug, t, diff_meta["text"])
        except (urllib.error.URLError, TimeoutError) as e:
            # Transport failure on one page: keep the rest of the run, say so.
            log(f"NAN unreachable while editing {slug}: {e}")
            report = {"slug": slug, "outcome": "failed", "detail": f"NAN inalcanzable: {e}"}
        except (PageParseError, OSError, RuntimeError, KeyError, TypeError) as e:
            log(f"page edit failed for {slug}: {e}")
            report = {"slug": slug, "outcome": "failed", "detail": str(e)}

        reports.append(report)
        if report.get("file"):
            changed_files.append(report["file"])
        log(f"{slug}: {report['outcome']}{' — ' + report['detail'] if report.get('detail') else ''}")

    if deferred:
        caps.append(f"Páginas diferidas: {', '.join(deferred)}.")

    if not changelog_mdx and not changed_files:
        log("nothing to write after the edit pass, advancing cursor without a PR")
        advance_cursor(main_sha)
        return 0

    try:
        if nav_dirty:
            save_nav(nav_path, nav_config)
            changed_files.append("docs.json")

        page_links = [
            (r.get("title") or page_title(r["slug"]), f"features/{r['slug']}")
            for r in reports
            if r["outcome"] in ("edited", "created")
        ]
        if not changelog_mdx:
            # Pages moved but the model gave no summary: still record the change,
            # a silent doc edit is worse than a terse changelog line.
            changelog_mdx = "Actualizamos la documentación de las funcionalidades que cambiaron en esta versión."
        append_changelog_entry(
            changelog_mdx, datetime.now(timezone.utc).strftime("%Y-%m-%d"), page_links
        )
        changed_files.append("changelog.mdx")

        body = build_pr_body(main_sha, cursor, len(commits), diff_meta, reports, deferred, caps)
        commit_and_push(sorted(set(changed_files)), main_sha)
        open_or_update_pr(body)
    except (RuntimeError, OSError, urllib.error.URLError, KeyError, TypeError) as e:
        log(f"docs repo operation failed, NOT advancing cursor (will retry next run): {e}")
        return 1

    advance_cursor(main_sha)
    edited = sum(1 for r in reports if r["outcome"] in ("edited", "created"))
    log(
        f"done — {len(commits)} commit(s) up to {main_sha[:7]}: "
        f"{edited} page(s) changed, {len(deferred)} deferred"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
