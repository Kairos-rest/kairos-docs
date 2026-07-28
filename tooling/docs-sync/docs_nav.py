"""docs.json navigation edits for the docs sync pipeline.

A new feature page that is not in `docs.json` is invisible on the site, so
drafting the page and wiring the nav have to happen in the same commit. The
model picks a group by its display name; everything else here is mechanical so
a bad guess relocates the page rather than corrupting the config.
"""

from __future__ import annotations

import json

FALLBACK_NOTE = "nav group not recognised, appended to the last group"


def load_nav(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def save_nav(path: str, config: dict) -> None:
    # Mintlify's own formatting is 2-space indented JSON with a trailing
    # newline; match it so the diff stays to the lines that changed.
    with open(path, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")


def group_names(config: dict) -> list[str]:
    return [
        g.get("group", "")
        for g in config.get("navigation", {}).get("pages", [])
        if isinstance(g, dict) and "group" in g
    ]


def page_exists(config: dict, page_ref: str) -> bool:
    for g in config.get("navigation", {}).get("pages", []):
        if isinstance(g, dict) and page_ref in g.get("pages", []):
            return True
    return False


def add_page(config: dict, page_ref: str, requested_group: str) -> tuple[dict, str | None]:
    """Insert `page_ref` into the nav under `requested_group`.

    Returns `(config, note)` where a non-None note means the requested group did
    not exist and the page landed in the last group instead — surfaced in the PR
    body so a human re-homes it rather than the page quietly sitting in the
    wrong place.
    """
    groups = config.get("navigation", {}).get("pages", [])
    dict_groups = [g for g in groups if isinstance(g, dict) and "group" in g]
    if not dict_groups:
        raise RuntimeError("docs.json has no navigation groups to add a page to")

    if page_exists(config, page_ref):
        return config, None

    target = next((g for g in dict_groups if g.get("group") == requested_group), None)
    note = None
    if target is None:
        target = dict_groups[-1]
        note = f"{FALLBACK_NOTE} ('{requested_group}' -> '{target.get('group')}')"

    target.setdefault("pages", []).append(page_ref)
    return config, note
