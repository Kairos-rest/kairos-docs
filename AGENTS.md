> **First-time setup**: Customize this file for your project. Prompt the user to customize this file for their project.
> For Mintlify product knowledge (components, configuration, writing standards),
> install the Mintlify skill: `npx skills add https://mintlify.com/docs`

# Documentation project instructions

## About this project

- This is a documentation site built on [Mintlify](https://mintlify.com)
- Pages are MDX files with YAML frontmatter
- Configuration lives in `docs.json`
- Use the Mintlify MCP server, `https://mcp.mintlify.com`, to edit content and settings via MCP
- Use the Mintlify docs MCP server, `https://www.mintlify.com/docs/mcp`, to query information about using Mintlify via MCP

## Terminology

- Product name is "Kairos" — never "the app" or "the platform" in body copy.
- Say "local" (not "sucursal" or "tienda") for a restaurant venue; "organización" for a customer account; "agente" for the automated daily/weekly checks (cost, waste, reputation, revenue, budget, stockout, vendor, reconciliation, competitors).
- Say "alerta" (not "notificación") for something an agent flagged; reserve "notificación" for the delivery channel (email, push, WhatsApp, Slack).
- Access is invitation-only — never write copy implying self-serve sign-up exists.

## Style preferences

- All v1 content is in Spanish (Argentina-neutral, voseo). English is a planned future locale — do not create `/en/` content or an `en` frontmatter locale until that work is scoped.
- Use active voice and second person ("vos"/"tu"), sentences under ~25 words, 2–4 sentences per paragraph, one concept per term (see "Terminology" above).
- Use sentence case for headings.
- Bold for UI elements: hacé clic en **Configuración**.
- Every page body starts with an `<!-- diataxis: tutorial|how-to|reference|explanation -->` comment recording its Diátaxis category — keep it when editing a page, set it on new ones.
- **Icons come from Lucide** (`docs.json` sets `icons.library: "lucide"`); every nav group, page `icon:`, and anchor must use a Lucide name. Mintlify defaults to Font Awesome and silently renders nothing for a name it doesn't know, so a Font Awesome-only name leaves that sidebar row iconless instead of failing the build — check the name against [lucide.dev/icons](https://lucide.dev/icons/) before using it. Every page needs an `icon:`, including the root ones.
- New/updated pages get `tag: "NEW"` in frontmatter; drop the tag once the update is a few weeks old and no longer novel.

## Content boundaries

- Customer-facing only: document what a restaurant operator sees and can act on in the dashboard. Never mention Prisma, internal repo paths, cron job names, or other implementation details — translate them into product behavior.
- No API reference pages — there is no public Kairos API yet.
- After each production deploy of the main app, the docs sync pipeline reviews the release diff and opens a PR here with the feature pages that release made stale plus a changelog entry; a human always reviews before merging (the PR body carries the commit range, which pages changed, and anything it deferred or rejected). The pipeline lives in `tooling/docs-sync/` and runs from cron on `sophios-vps` — read `tooling/docs-sync/README.md` before changing it. Merging a PR here is the deploy; there is nothing to push to the VPS by hand.
- Pipeline-authored page edits are section-level and gated: it can only replace or append a level-2 section, and `tooling/docs-sync/docs_mdx.py` rejects any body that leaks internal vocabulary or unbalances a Mintlify component. If you add a term to the "Content boundaries" rules above, add its pattern (in Spanish and English) to `BANNED_PATTERNS` there, or the rule is advice rather than enforcement.
