"""Release-diff selection and budgeting for the docs sync pipeline.

The GitHub compare API hands back every file touched between two SHAs. Most of
it is invisible to a restaurant operator — tests, tooling, generated clients,
lockfiles — and feeding it to the model just burns context and invites the
model to document internals. This module reduces a compare response to the
customer-facing surface, ranked by how much it tells you about product
behaviour, and caps it so a large release cannot blow the context budget.

Nothing here talks to the network; `select_release_diff` takes the parsed
compare payload so it stays unit-testable.
"""

from __future__ import annotations

# Highest signal first. i18n message catalogues are literally the strings the
# operator reads on screen, so a copy change there is the most direct evidence
# that user-visible behaviour moved. Route handlers and services come next
# (they define what the product does), then the React surface.
PRIORITY_PREFIXES: tuple[tuple[str, int], ...] = (
    ("messages/", 0),
    ("docs/FEATURES.md", 1),
    ("lib/services/", 2),
    ("app/api/", 3),
    ("app/", 4),
    ("components/", 5),
    ("lib/schemas/", 6),
)

# A path must match one of these to be considered at all.
INCLUDE_PREFIXES: tuple[str, ...] = tuple(p for p, _ in PRIORITY_PREFIXES)

# Checked before the include list — an excluded path is dropped even if it
# lives under an included prefix (e.g. `lib/services/foo/__tests__/`).
EXCLUDE_SUBSTRINGS: tuple[str, ...] = (
    "/__tests__/",
    "/__mocks__/",
    "/__snapshots__/",
    ".test.",
    ".spec.",
    ".snap",
    "lib/generated/",
    "/node_modules/",
)

EXCLUDE_PREFIXES: tuple[str, ...] = (
    "tests/",
    "tooling/",
    "scripts/",
    ".github/",
    ".claude/",
    ".agents/",
    "graphify-out/",
    "prisma/",
    "docs/HARNESS/",
    "docs/agent/",
    "docs/archive/",
    "docs/decisions/",
)

# Only these two message catalogues are real UI copy; anything else under
# `messages/` (metadata, per-locale config) is noise.
ALLOWED_MESSAGE_FILES: tuple[str, ...] = ("messages/en.json", "messages/es.json")

MAX_PATCH_CHARS_PER_FILE = 6_000
MAX_TOTAL_PATCH_CHARS = 120_000


def is_customer_facing(path: str) -> bool:
    """True when a changed file could plausibly explain a user-visible change."""
    if any(path.startswith(p) for p in EXCLUDE_PREFIXES):
        return False
    if any(s in path for s in EXCLUDE_SUBSTRINGS):
        return False
    if path.startswith("messages/"):
        return path in ALLOWED_MESSAGE_FILES
    # Internal handbooks are excluded above; docs/FEATURES.md is the one
    # markdown file that describes the product rather than the repo.
    if path.endswith(".md") or path.endswith(".mdx"):
        return path == "docs/FEATURES.md"
    return any(path.startswith(p) for p in INCLUDE_PREFIXES)


def _priority(path: str) -> int:
    for prefix, rank in PRIORITY_PREFIXES:
        if path.startswith(prefix):
            return rank
    return len(PRIORITY_PREFIXES)


def select_release_diff(
    files: list[dict],
    max_total_chars: int = MAX_TOTAL_PATCH_CHARS,
    max_file_chars: int = MAX_PATCH_CHARS_PER_FILE,
) -> dict:
    """Reduce compare `files[]` to a budgeted, prioritised diff.

    Returns a dict with:
      `text`            — the diff to hand the model, one block per file
      `included_paths`  — paths whose patch made it in (ordered by priority)
      `dropped_paths`   — customer-facing paths cut by the budget
      `ignored_count`   — files rejected as not customer-facing
      `no_patch_paths`  — customer-facing files GitHub gave no patch for
                          (binary, or too large for the compare API)
    """
    candidates: list[dict] = []
    ignored_count = 0
    no_patch_paths: list[str] = []

    for f in files:
        path = f.get("filename", "")
        if not path or not is_customer_facing(path):
            ignored_count += 1
            continue
        patch = f.get("patch")
        if not patch:
            # GitHub omits `patch` for binaries and very large files. Record the
            # path so the model still learns the file moved, and so the PR body
            # can say so out loud.
            no_patch_paths.append(path)
            continue
        candidates.append({"path": path, "patch": patch, "status": f.get("status", "modified")})

    candidates.sort(key=lambda c: (_priority(c["path"]), c["path"]))

    blocks: list[str] = []
    included: list[str] = []
    dropped: list[str] = []
    used = 0

    for c in candidates:
        patch = c["patch"]
        truncated = False
        if len(patch) > max_file_chars:
            patch = patch[:max_file_chars]
            truncated = True
        header = f"--- {c['path']} ({c['status']})"
        if truncated:
            header += " [patch truncated]"
        block = f"{header}\n{patch}\n"
        if used + len(block) > max_total_chars:
            dropped.append(c["path"])
            continue
        blocks.append(block)
        included.append(c["path"])
        used += len(block)

    return {
        "text": "\n".join(blocks),
        "included_paths": included,
        "dropped_paths": dropped,
        "ignored_count": ignored_count,
        "no_patch_paths": no_patch_paths,
    }


def scope_diff_to_paths(diff_text: str, paths: list[str]) -> str:
    """Narrow an already-budgeted diff to the blocks for `paths`.

    Used for the per-page editing pass: the model only needs the files the
    triage pass tied to that page. Falls back to the full diff when nothing
    matches, so a bad path guess degrades to more context rather than none.
    """
    if not paths:
        return diff_text
    wanted = set(paths)
    kept: list[str] = []
    current_path: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current_path is not None and current_path in wanted:
            kept.append("".join(buffer))

    for line in diff_text.splitlines(keepends=True):
        if line.startswith("--- "):
            flush()
            buffer = [line]
            # Header shape: `--- <path> (<status>)[ [patch truncated]]`
            current_path = line[4:].split(" (")[0].strip()
        else:
            buffer.append(line)
    flush()

    return "".join(kept) if kept else diff_text
