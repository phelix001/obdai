#!/usr/bin/env python3
"""Build real store + how-to links for a part, from a name / part number / query.

The AI must never hand-write store URLs — it hallucinates them. It supplies a plain
part name (and a part number when confident); these helpers turn that into valid
RockAuto / NAPA / YouTube links. They are *search* links (robust), not deep product
URLs, so they keep working when the sites change.

Shared by the one-shot diagnosis report (obd_diagnose.render_diagnosis) and the chat's
suggest_parts / full_diagnosis tools — one place to fix if a store's URL format changes.
"""

from urllib.parse import quote


def store_links(name, part_number=""):
    """{'rockauto', 'napa'} search URLs for a part. Part-number search when we have one."""
    name = (name or "").strip()
    pn = (part_number or "").strip()
    napa = f"https://www.napaonline.com/en/search?text={quote(name)}&referer=v2"
    if pn:
        rock = f"https://www.rockauto.com/en/partsearch/?partnum={quote(pn)}"
    else:
        rock = f"https://www.rockauto.com/en/partsearch/?partname={quote(name)}"
    return {"rockauto": rock, "napa": napa}


def youtube_link(query):
    """A YouTube search URL for a how-to query, or '' if there is no query."""
    q = (query or "").strip()
    return f"https://www.youtube.com/results?search_query={quote(q)}" if q else ""


def part_label(part):
    name = (part.get("name") or "").strip()
    pn = (part.get("part_number") or "").strip()
    return name + (f"  [PN {pn}]" if pn else "")


def parts_markdown(parts, video_search="", header="Parts to buy"):
    """Markdown: each part with its label + RockAuto/NAPA links, plus a YouTube how-to.

    `parts` = [{name, part_number?}]. Returns '' if there is nothing to show.
    """
    lines = []
    real = [p for p in (parts or []) if (p.get("name") or "").strip()]
    if real:
        lines.append(f"**{header}:**")
        for p in real:
            links = store_links(p.get("name"), p.get("part_number"))
            lines.append(f"- {part_label(p)}\n"
                         f"  - [RockAuto]({links['rockauto']}) · [NAPA]({links['napa']})")
    yt = youtube_link(video_search)
    if yt:
        lines.append(("\n" if lines else "") + f"**How-to:** [{video_search.strip()}]({yt})")
    return "\n".join(lines)


def plain_parts_lines(parts):
    """Plain-text parts block (no markdown) for saved reports."""
    out = []
    for p in parts or []:
        if not (p.get("name") or "").strip():
            continue
        links = store_links(p.get("name"), p.get("part_number"))
        out.append(f"  {part_label(p)}\n    RockAuto: {links['rockauto']}\n    NAPA:     {links['napa']}")
    return out
