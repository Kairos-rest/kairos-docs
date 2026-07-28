"""MDX page surgery and safety gates for the docs sync pipeline.

The model never writes a whole page file. It proposes changes to named sections
and this module splices them in, so frontmatter, the Diátaxis marker and every
section the model did not mention survive byte-for-byte. That is what makes the
resulting PR reviewable — the diff shows the sections that actually moved.

`validate_body` is the gate that keeps AGENTS.md's content boundaries
enforceable rather than aspirational: no internal implementation vocabulary, no
code fences, and no unbalanced Mintlify components reaching `main`.
"""

from __future__ import annotations

import re

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
DIATAXIS_RE = re.compile(r"\{/\*\s*diataxis:\s*(tutorial|how-to|reference|explanation)\s*\*/\}")
HEADING_RE = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)

# Vocabulary that betrays repo internals. AGENTS.md: "Never mention Prisma,
# internal repo paths, cron job names, or other implementation details."
#
# Pages are written in Spanish, so every term needs its Spanish form too — an
# English-only `\bmigrations?\b` never fires on "migración", which is exactly the
# word a model drafting Spanish copy would reach for. Terms that legitimately
# appear in operator-facing copy (for example "API" when naming a POS
# integration) are deliberately absent.
BANNED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bprisma\b", "Prisma"),
    (r"\bpostgres(ql)?\b", "Postgres"),
    (r"\bupstash\b", "Upstash"),
    (r"\bredis\b", "Redis"),
    (r"\bclerk\b", "Clerk"),
    (r"\bvercel\b", "Vercel"),
    (r"\bsentry\b", "Sentry"),
    (r"\bcron\b", "cron"),
    (r"\bwebhooks?\b", "webhook"),
    (r"\bzod\b", "Zod"),
    (r"\borg_?id\b", "orgId"),
    (r"\bnpm\b|\bpnpm\b", "package manager"),
    (r"\bmigrations?\b|\bmigraci[oó]n(es)?\b", "migration"),
    (r"\bendpoints?\b", "endpoint"),
    (r"\bbases? de datos\b|\bdatabases?\b", "database"),
    (r"\bsql\b", "SQL"),
    (r"\b(lib|app/api|components|prisma)/[a-z0-9._/-]+", "repo path"),
    (r"\.(tsx?|jsx?|sql|prisma)\b", "source file extension"),
    # Ban the fence marker itself, not a list of languages: a bare ``` or an
    # unlisted one (```text, ```yaml) is just as much a code block, and an
    # accepted fence is worse than cosmetic — `parse_page` has no fence
    # awareness, so a `## ` line inside one becomes a real section heading.
    (r"```", "code fence"),
)

# Mintlify components that must open and close in pairs. An imbalance breaks
# the Mintlify build, and the build runs after merge — so it has to be caught
# here, not by a red deploy.
PAIRED_COMPONENTS: tuple[str, ...] = (
    "Tabs", "Tab", "Steps", "Step", "Accordion", "AccordionGroup",
    "Card", "CardGroup", "Note", "Tip", "Warning", "Info", "Check",
    "Update", "Frame", "CodeGroup", "Columns", "Expandable", "ResponseField",
)

MIN_BODY_CHARS = 20


class PageParseError(RuntimeError):
    """Raised when a page does not match the shape every kairos-docs page has."""


class MdxPage:
    """A feature page split into the parts the pipeline treats differently.

    `frontmatter` and `diataxis` are structural and only ever changed
    mechanically. `intro` is the lead paragraph(s) before the first `##`.
    `sections` is an ordered list of `(heading, body)` where body excludes the
    heading line itself.
    """

    def __init__(self, frontmatter: str, diataxis: str, intro: str, sections: list[tuple[str, str]]):
        self.frontmatter = frontmatter
        self.diataxis = diataxis
        self.intro = intro
        self.sections = sections

    @property
    def headings(self) -> list[str]:
        return [h for h, _ in self.sections]

    def render(self) -> str:
        out = [f"---\n{self.frontmatter}\n---\n\n", f"{self.diataxis}\n"]
        if self.intro.strip():
            out.append(f"\n{self.intro.strip()}\n")
        for heading, body in self.sections:
            out.append(f"\n## {heading}\n")
            if body.strip():
                out.append(f"\n{body.strip()}\n")
        return "".join(out)


def parse_page(source: str) -> MdxPage:
    fm = FRONTMATTER_RE.match(source)
    if not fm:
        raise PageParseError("page has no YAML frontmatter block")
    rest = source[fm.end():]

    dia = DIATAXIS_RE.search(rest)
    if not dia:
        raise PageParseError("page has no {/* diataxis: ... */} marker")
    body = rest[dia.end():]

    matches = list(HEADING_RE.finditer(body))
    if not matches:
        return MdxPage(fm.group(1), dia.group(0), body.strip(), [])

    intro = body[: matches[0].start()].strip()
    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append((m.group(1).strip(), body[m.end():end].strip()))
    return MdxPage(fm.group(1), dia.group(0), intro, sections)


def validate_body(body: str, allow_headings: bool = False) -> list[str]:
    """Return a list of problems with model-authored MDX. Empty list = accept.

    `allow_headings` is only true for a whole new page, whose body is *supposed*
    to be a series of level-2 sections. For a section edit it must stay false:
    a `## ` line inside a section body would be invisible to `apply_actions`
    (which validates one section at a time) but becomes a real heading the next
    time the page is parsed. That is how the model can smuggle in a heading it
    was never allowed to create, duplicate an existing one, or — worse — leave a
    Mintlify closing tag orphaned across runs once the new "section" splits its
    opener from its closer. Per-body component balance is not per-page balance.
    """
    problems: list[str] = []
    stripped = body.strip()
    if len(stripped) < MIN_BODY_CHARS:
        problems.append(f"body too short ({len(stripped)} chars)")
    if re.search(r"\b(TODO|TBD|FIXME|lorem ipsum|XXX)\b", stripped, re.IGNORECASE):
        problems.append("body contains a placeholder marker")
    if not allow_headings and re.search(r"^#{1,6}\s", stripped, re.MULTILINE):
        problems.append("section body must not contain its own heading")

    lowered = stripped.lower()
    for pattern, label in BANNED_PATTERNS:
        if re.search(pattern, lowered):
            problems.append(f"body leaks internal detail: {label}")

    for comp in PAIRED_COMPONENTS:
        # `<Tab ` and `<Tab>` both open; `<Tab />` is self-closing and needs no
        # partner. Count openings that are not self-closed.
        opens = len(re.findall(rf"<{comp}(?=[\s>])(?:[^>]*?)(?<!/)>", stripped))
        closes = len(re.findall(rf"</{comp}>", stripped))
        if opens != closes:
            problems.append(f"unbalanced <{comp}>: {opens} open vs {closes} close")

    if re.search(r"^---\s*$", stripped, re.MULTILINE) and stripped.startswith("---"):
        problems.append("body must not contain its own frontmatter block")

    return problems


def apply_actions(page: MdxPage, actions: list[dict]) -> tuple[MdxPage, list[str], list[str]]:
    """Splice model-proposed section changes into `page`.

    Returns `(page, applied, rejected)`. Rejections are per-action and never
    abort the run — a page keeps whatever edits passed, and the PR body reports
    what was dropped and why.
    """
    applied: list[str] = []
    rejected: list[str] = []

    for action in actions:
        if not isinstance(action, dict):
            rejected.append("action is not an object")
            continue
        kind = (action.get("type") or "").strip()
        if kind == "none":
            continue

        body = action.get("body_mdx") or ""
        problems = validate_body(body)
        if problems:
            target = action.get("heading") or kind
            rejected.append(f"{kind} '{target}': {'; '.join(problems)}")
            continue

        if kind == "replace_intro":
            page.intro = body.strip()
            applied.append("replace_intro")
            continue

        heading = (action.get("heading") or "").strip()
        if not heading:
            rejected.append(f"{kind}: missing heading")
            continue

        if kind == "replace_section":
            idx = next((i for i, (h, _) in enumerate(page.sections) if h == heading), None)
            if idx is None:
                rejected.append(f"replace_section '{heading}': no such heading on the page")
                continue
            if page.sections[idx][1].strip() == body.strip():
                continue  # no-op, not worth a commit line
            page.sections[idx] = (heading, body.strip())
            applied.append(f"replace_section '{heading}'")
            continue

        if kind == "append_section":
            if heading in page.headings:
                rejected.append(f"append_section '{heading}': heading already exists")
                continue
            after = (action.get("after_heading") or "").strip()
            if after:
                idx = next((i for i, (h, _) in enumerate(page.sections) if h == after), None)
                if idx is None:
                    page.sections.append((heading, body.strip()))
                else:
                    page.sections.insert(idx + 1, (heading, body.strip()))
            else:
                page.sections.append((heading, body.strip()))
            applied.append(f"append_section '{heading}'")
            continue

        rejected.append(f"unknown action type '{kind}'")

    return page, applied, rejected


def set_frontmatter_key(frontmatter: str, key: str, value: str) -> str:
    """Set or replace a scalar key in a YAML frontmatter block.

    Deliberately dumb — kairos-docs frontmatter is a flat set of quoted scalars,
    so a line-level rewrite is safer here than pulling in a YAML dependency the
    VPS would have to install.
    """
    line = f'{key}: "{value}"'
    pattern = re.compile(rf"^{re.escape(key)}\s*:.*$", re.MULTILINE)
    if pattern.search(frontmatter):
        return pattern.sub(line, frontmatter)
    return frontmatter.rstrip() + "\n" + line


def sanitize_scalar(value: str, max_len: int = 200) -> str:
    """Make a model-supplied string safe to interpolate into a quoted YAML scalar.

    Without this, a `description` containing a newline plus `---` terminates the
    frontmatter block early and leaks the rest into the page body — and the
    round-trip parse check does not catch it, because `FRONTMATTER_RE` is
    non-greedy and happily accepts the truncated block. An embedded `"` is the
    cheaper version of the same problem: invalid YAML that only fails at build
    time, after merge.
    """
    collapsed = re.sub(r"\s+", " ", value).strip()
    return collapsed.replace('"', "'")[:max_len].strip()


def build_new_page(title: str, sidebar_title: str, description: str, icon: str,
                   diataxis: str, body_mdx: str) -> str:
    """Assemble a brand-new feature page.

    Frontmatter is written here rather than by the model so title/description/
    icon/tag always exist, are always quoted the way the rest of the site quotes
    them, and cannot break out of their own block.
    """
    fm = "\n".join([
        f'title: "{sanitize_scalar(title)}"',
        f'sidebarTitle: "{sanitize_scalar(sidebar_title, 40)}"',
        f'description: "{sanitize_scalar(description)}"',
        f'icon: "{re.sub(r"[^a-z0-9-]", "", icon.lower()) or "file-text"}"',
        'tag: "NEW"',
    ])
    return f"---\n{fm}\n---\n\n{{/* diataxis: {diataxis} */}}\n\n{body_mdx.strip()}\n"
