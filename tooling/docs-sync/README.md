# docs-sync pipeline

Reviews each production release of `Kairos-rest/app` and opens a PR here with
the feature pages that release made stale, plus the changelog entry. Runs from
cron on `sophios-vps`. Never pushes to `main`, never auto-merges.

## How a run works

1. **Cursor** — `state.json` on the VPS holds the last documented `app` `main`
   SHA. Compare that to current `main`; no new commits means no work. If a
   drafted PR is still open, the comparison starts from *that PR's* base
   (`pr_base_sha`) instead — see "Why an open PR changes the base" below.
2. **Diff selection** (`docs_diff.py`) — the compare response is reduced to the
   customer-facing surface (`messages/en.json`/`es.json`, `lib/services/`,
   `app/`, `components/`, `lib/schemas/`, `docs/FEATURES.md`) and ranked with UI
   copy first. Tests, tooling, generated clients, `prisma/`, internal handbooks
   and lockfiles are dropped. Budget: 6k chars per file, 120k total.
3. **Triage pass** — one NAN call gets the commit subjects, the budgeted diff,
   the list of existing page slugs and the nav group names. It returns the
   changelog entry plus the pages that need editing or creating, each with the
   diff paths that justify it.
4. **Per-page pass** — one NAN call per target page, given only that page's
   current MDX and the slice of the diff behind it. It returns section-level
   actions, not a page. Capped at 4 pages and 20 minutes per run; anything over
   is deferred and named in the PR body.
5. **Apply** (`docs_mdx.py`) — actions are spliced into the parsed page, so
   frontmatter, the Diátaxis marker and unmentioned sections are untouched. New
   pages also get a `docs.json` nav entry (`docs_nav.py`).
6. **Changelog + PR** — the entry is prepended before the newest existing
   `<Update>`, with links to the pages it covers. One branch
   (`docs-sync/auto`), force-pushed, so repeated deploys update the open PR.

## Why two passes

A single call asked to summarise a release *and* rewrite prose does neither
well, and its page edits can't be attributed to anything. Splitting them means
the changelog is drafted from the whole release while each page edit is
grounded only in the files that touch that page.

## Why an open PR changes the base

The bot branch is recut from `main` on every run and force-pushed. On a
30-minute cron, two deploys landing before anyone reviews the PR is the normal
case, not the exception — and if run 2 drafted only its own range, the
force-push would erase run 1's page edits while the cursor had already moved
past them. That range would never be drafted again, and nothing in the PR would
say so.

So `state.json` records `pr_base_sha`: the commit the currently-open PR was
drafted from. While that PR is open *and there are new commits*, every run
re-compares from there, redrafts the whole undocumented range, and force-pushes a
PR that always describes the full picture. With no new commits the run is a
no-op — otherwise it would redraft an identical range every 30 minutes and
force-push a slightly different draft out from under whoever is mid-review.

Two consequences worth knowing:

- **Closing the bot PR without merging means that range is intentionally never
  documented.** The cursor has already advanced; the next run finds no open PR
  and starts from the cursor. That is the designed way to say "don't document
  this" — but it is deliberate, not recoverable.
- **Before the first run of this version**, merge or close any open
  `docs-sync/auto` PR. State written by the previous version has no
  `pr_base_sha`, so the first tick would fall back to the cursor and force-push
  that PR's content away — the exact bug this fixes, replayed once. Alternatively,
  hand-write `pr_base_sha` into `state.json`. (At the time this shipped there was
  no open bot PR, so nothing to do.)

## What stops a bad edit reaching main

The model never writes a file. It names a section and supplies a body, and
`docs_mdx.validate_body` gates every body before it lands — **including the
changelog entry**, which is the most-written model-authored text on the site:

- **Internal vocabulary** — Prisma, Postgres, Redis, Clerk, Vercel, cron,
  webhook, endpoint, database, SQL, repo paths, source extensions, code fences.
  Every term carries its Spanish form too: the pages are Spanish, so an
  English-only list would miss "migración" and "bases de datos".
- **Headings inside a section body** — a `## ` line would be invisible to the
  per-section check but becomes a real heading the next time the page is parsed.
  That is how a model can duplicate a heading it was never allowed to create,
  or split a Mintlify component's opener from its closer across two runs.
  Allowed only for a whole new page, which is *made* of sections.
- **Code fences** — the marker itself, not a list of languages. A bare ``` is
  still a code block, and `parse_page` has no fence awareness, so an accepted
  fence containing `## ` is the way a heading sneaks in.
- **Unbalanced Mintlify components** — an unclosed `<Tabs>` breaks the build,
  and the build runs *after* merge, so it has to be caught here.
- **Forged changelog entries** — a literal `<Update>` tag in the summary is
  rejected; that block is ours to write.
- **Placeholders** — TODO/TBD/FIXME/lorem ipsum.
- **Unknown headings** — `replace_section` must match an existing level-2
  heading character for character.

Frontmatter for a new page is assembled from sanitised scalars, never
interpolated raw: a `description` carrying a newline plus `---` would otherwise
close the block early, and the round-trip parse check would not notice because
the frontmatter regex is non-greedy.

A rejection is per-section and never fails the run: the page keeps whatever
passed, and the PR body says what was dropped and why. A rejected changelog
summary falls back to terse generic text rather than blocking the PR. Human
review is still the last gate on every PR.

## Files

| File | Purpose |
|---|---|
| `docs-sync.py` | the job: cursor, NAN passes, commit, PR |
| `docs_diff.py` | release-diff selection, ranking and budgeting |
| `docs_mdx.py` | MDX parse/render, section surgery, safety gates |
| `docs_nav.py` | `docs.json` navigation edits |
| `docs_changelog.py` | changelog entry + PR-body report |
| `docs_prompts.py` | the two passes' prompts + the shared style rules |
| `bootstrap.sh` | cron entry point; copy of what runs on the VPS |
| `test_docs_sync.py` | unit tests, stdlib only |

Run the tests (no install step, no network):

```bash
cd tooling/docs-sync && python3 -m unittest test_docs_sync
```

## VPS layout

```
~/kairos-docs-pipeline/
  bootstrap.sh            cron entry point (copy of tooling/docs-sync/bootstrap.sh)
  .env-kairos-docs        the two GitHub PATs, chmod 600
  state.json              last_sha (cursor), pr_base_sha (open PR's base), triage_failures
  .docs-sync.lock         single-flight lock
  tooling-checkout/       read-only clone of this repo — the code that runs
  kairos-docs-checkout/   working clone the job commits and force-pushes from
```

Two separate clones on purpose: `bootstrap.sh` fast-forwards `tooling-checkout`
and then execs `python3` from it, so the force-push into `kairos-docs-checkout`
can never disturb the code mid-run. `bootstrap.sh` updates the checkout and then
execs Python rather than another shell script from that checkout — bash reads a
script incrementally, so rewriting a running `.sh` underneath itself gives you a
half-old, half-new run.

Crontab:

```cron
*/30 * * * * /home/sophios/kairos-docs-pipeline/bootstrap.sh >> /home/sophios/logs/kairos-docs-sync.log 2>&1
```

Editing the pipeline means merging a PR here — the next cron tick picks it up.
Nothing to deploy by hand.

## Credentials

Two fine-grained GitHub PATs in `~/kairos-docs-pipeline/.env-kairos-docs`
(`chmod 600`, never in this repo — it is public):

- `GH_APP_READ_TOKEN` — `Kairos-rest/app`, `Contents: Read-only`.
- `GH_DOCS_WRITE_TOKEN` — `Kairos-rest/kairos-docs`, `Contents: Read and write`
  + `Pull requests: Read and write`.

`NAN_API_URL` / `NAN_API_KEY` come from `~/.hermes/.env.pulgita`, the shared NAN
credential every Kairos script on that box already sources.

## Failure modes, by design

| Situation | Behaviour |
|---|---|
| GitHub unreachable | skip run, cursor NOT advanced, retried next tick |
| NAN unreachable during triage | skip run, cursor NOT advanced |
| NAN unreachable during one page | that page reported as failed, rest of run continues |
| Triage response unusable (non-JSON, missing keys) | cursor NOT advanced, range retried; after `DOCS_SYNC_MAX_TRIAGE_FAILURES` consecutive failures the range is abandoned with a loud log |
| Compare base no longer exists in the app repo (404) | cursor re-bootstrapped to current `main` with a loud log — one undocumented range beats a permanent wedge |
| A section body fails validation | that action dropped, reported in the PR body |
| Changelog summary fails validation | replaced with generic text, reported in the PR body |
| Open PR unreviewed when the next deploy lands | run re-drafts from the PR's base so no range is lost |
| Open PR unreviewed and no new commits | no-op; the PR already covers its range |
| Bot PR closed without merging | that range is intentionally never documented (see above) |
| More than 4 target pages | extras deferred and named in the PR body |
| Run exceeds 20 minutes | remaining pages deferred and named in the PR body |
| Compare exceeds 300 files | GitHub's ceiling; flagged in the PR body as possibly incomplete |
| Overlapping runs | the later one no-ops via `flock`, does not queue |
| Docs repo push fails | cursor NOT advanced, retried next tick |

Every cap is logged and repeated in the PR body. A run that covered less than
the full release says so.

## Tuning

| Env var | Default | Meaning |
|---|---|---|
| `DOCS_SYNC_MAX_COMMITS` | 60 | newest commits kept from a long range |
| `DOCS_SYNC_MAX_PAGES` | 4 | page edits attempted per run |
| `DOCS_SYNC_MAX_TRIAGE_FAILURES` | 5 | unusable triage responses before a range is abandoned |
| `DOCS_SYNC_NAN_TIMEOUT` | 180 | seconds per NAN call |
| `DOCS_SYNC_DEADLINE` | 1200 | whole-run wall clock ceiling, seconds |
| `DOCS_SYNC_BRANCH` | `docs-sync/auto` | branch the PR comes from |
| `DOCS_SYNC_HOME` | `~/kairos-docs-pipeline` | state + checkouts location |

## History

- **KAI-246** — first version. Fed the model commit subject lines only and
  wrote `changelog.mdx` exclusively; it asked for `touched_feature_slugs` and
  then discarded the field, and `git add changelog.mdx` was hard-coded, so no
  feature page could ever change. Pages sat untouched after the initial content
  drop while the changelog kept claiming to say "qué página lo cubre".
- **KAI-403** — this version. Reads the release diff, edits feature pages by
  section, drafts new pages with a nav entry, and moved the code into this repo
  so it is reviewable instead of a loose file on a VPS.
