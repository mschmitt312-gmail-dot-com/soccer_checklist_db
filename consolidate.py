#!/usr/bin/env python3
"""
consolidate.py -- merge child set folders into their base set folder.

Run this AFTER scraper.py has downloaded sets.

Child folders (those matching _(02), _(03)_-_PlayerName, etc.) are merged
into their base folder, then hollowed out and replaced with a _merged.txt
marker file so the scraper won't re-download them on subsequent runs.

Child types:
  - Player/team children   -> checklist entries + images merged into base
  - Numbered-only children -> same as player/team
  - Reference/physical     -> source URL + images merged, but checklist skipped
                              (a warning is printed if a reference child has cards)

Usage:
    python consolidate.py            # live run
    python consolidate.py --dry-run  # preview only, no changes written
"""

import os
import re
import json
import shutil
import sys
from datetime import datetime

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soccer_checklists")

DRY_RUN = "--dry-run" in sys.argv

# Keywords in the child suffix that flag it as reference/physical material.
# These children contribute source URLs and images to the base but their
# checklist entries are NOT merged (to keep the player list clean).
REFERENCE_KEYWORDS = [
    "wrapper", "album", "packet", "uncut", "badge", "booklet",
    "box", "display", "header", "panel", "scratch",
    "insert", "envelope", "variation", "reprint",
]

# Image file extensions to copy from child to base folder
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def get_base_name(folder_name):
    """Strip _(0N)... suffix to get the base set name."""
    return re.sub(r"_\(0\d+\).*$", "", folder_name)


def is_reference_suffix(suffix):
    """Return True if the child suffix indicates reference/physical material."""
    if not suffix:
        return False
    s = suffix.lower()
    return any(k in s for k in REFERENCE_KEYWORDS)


def load_json_safe(path):
    """
    Load a JSON file. Handles the case where the file accidentally contains
    multiple concatenated JSON objects (takes the first one).
    Returns (data_dict, warning_string_or_None).
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        return json.loads(content), None
    except json.JSONDecodeError:
        try:
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(content.strip())
            return obj, "multi-object JSON -- only first object used"
        except Exception as e:
            return None, str(e)


def copy_images(src_folder, dst_folder, prefix):
    """
    Copy image files from src_folder into dst_folder.
    Files are prefixed with <prefix>_ to avoid name collisions.
    Returns list of new filenames copied.
    """
    copied = []
    for fname in os.listdir(src_folder):
        ext = os.path.splitext(fname)[1].lower()
        if ext in IMAGE_EXTS:
            src = os.path.join(src_folder, fname)
            dst_name = prefix + "_" + fname
            dst = os.path.join(dst_folder, dst_name)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
                copied.append(dst_name)
    return copied


# --------------------------------------------------
# MERGE LOGIC
# --------------------------------------------------

def merge_child_into_base(base_data, child_data, child_folder_name, suffix):
    """
    Merge a child's data into base_data (mutates base_data in place).

    Reference children: source URL + images noted, checklist skipped.
    All other children: source URL + checklist entries merged in.

    Returns (cards_added: int, is_reference: bool).
    cards_added < 0 means the reference child had cards that were intentionally skipped.
    """
    ref = is_reference_suffix(suffix)

    # Record source URL in merged_from list
    if "merged_from" not in base_data:
        base_data["merged_from"] = []
    child_url = child_data.get("source_url", "")
    already_listed = any(m.get("url") == child_url for m in base_data["merged_from"])
    if child_url and not already_listed:
        base_data["merged_from"].append({
            "url":    child_url,
            "folder": child_folder_name,
            "type":   "reference" if ref else "checklist",
        })

    child_cards = child_data.get("checklist", [])

    if ref:
        # Don't merge checklist, but warn if cards were skipped
        return (-len(child_cards) if child_cards else 0), True

    if not child_cards:
        return 0, False

    # Merge checklist -- deduplicate by lower-cased player_name
    existing = {c.get("player_name", "").strip().lower()
                for c in base_data.get("checklist", [])}
    added = 0
    for card in child_cards:
        name = card.get("player_name", "").strip()
        if name.lower() not in existing and name:
            base_data.setdefault("checklist", []).append(card)
            existing.add(name.lower())
            added += 1

    return added, False


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def consolidate():
    if not os.path.isdir(BASE_DIR):
        print("ERROR: soccer_checklists folder not found: " + BASE_DIR)
        return

    folders = sorted(f for f in os.listdir(BASE_DIR)
                     if os.path.isdir(os.path.join(BASE_DIR, f)))

    # Build groups: base_name -> [child_folder_name, ...]
    groups = {}
    for folder in folders:
        base = get_base_name(folder)
        if base != folder:                       # folder IS a child
            groups.setdefault(base, []).append(folder)

    if not groups:
        print("No child folders found. Nothing to consolidate.")
        return

    print("consolidate.py")
    print("DRY_RUN = {}".format(DRY_RUN))
    print("Base dir: " + BASE_DIR)
    print("Found {} base sets that have child folders.".format(len(groups)))
    print()

    total_groups_done  = 0
    total_orphans      = 0
    total_children     = 0
    total_cards_added  = 0
    warnings           = []

    for base_name, children in sorted(groups.items()):
        base_folder = os.path.join(BASE_DIR, base_name)
        base_json   = os.path.join(base_folder, "export.json")

        # --- Orphan: child exists but base folder doesn't ---
        if not os.path.isdir(base_folder):
            print("[ORPHAN] Base folder missing: {}".format(base_name))
            for c in children:
                print("         child: {}".format(c))
            total_orphans += 1
            print()
            continue

        if not os.path.exists(base_json):
            print("[SKIP] Base has no export.json: {}".format(base_name))
            print()
            continue

        base_data, load_warn = load_json_safe(base_json)
        if base_data is None:
            print("[ERROR] Cannot parse base JSON for: {}".format(base_name))
            print("        Reason: {}".format(load_warn))
            print()
            continue
        if load_warn:
            warnings.append("Base JSON repaired ({}): {}".format(load_warn, base_name))

        cards_before     = len(base_data.get("checklist", []))
        group_cards_added = 0
        base_modified    = False

        print("[SET] {}".format(base_name))

        for child_folder in sorted(children):
            child_path = os.path.join(BASE_DIR, child_folder)
            child_json = os.path.join(child_path, "export.json")
            marker     = os.path.join(child_path, "_merged.txt")

            # Already merged on a previous run?
            if os.path.exists(marker):
                print("  [SKIP already merged] {}".format(child_folder))
                continue

            # Extract suffix text after _(0N)
            m = re.search(r"_\(0\d+\)(?:_-_(.+))?$", child_folder)
            suffix = m.group(1) if m else None

            if not os.path.exists(child_json):
                print("  [SKIP no json] {}".format(child_folder))
                continue

            child_data, _ = load_json_safe(child_json)
            if child_data is None:
                print("  [ERROR cannot parse] {}".format(child_folder))
                continue

            # Copy images before hollowing out
            images_copied = []
            if not DRY_RUN:
                safe_prefix = re.sub(r"[^A-Za-z0-9_\-]", "", child_folder)[:40]
                images_copied = copy_images(child_path, base_folder, safe_prefix)

            added, is_ref = merge_child_into_base(
                base_data, child_data, child_folder, suffix)
            base_modified = True

            # Reporting
            ref_tag = " [REF]" if is_ref else ""
            if added > 0:
                group_cards_added += added
                print("  [MERGE{}] +{} cards | {}".format(ref_tag, added, child_folder))
            elif added < 0:
                skipped_count = -added
                warnings.append(
                    "Reference child had {} cards not added to checklist: {}".format(
                        skipped_count, child_folder))
                print("  [MERGE REF] 0 checklist cards added "
                      "(reference type, {} cards skipped) | {}".format(
                          skipped_count, child_folder))
            else:
                print("  [MERGE{}] 0 cards | {}".format(ref_tag, child_folder))

            if images_copied:
                print("    Images copied: {}".format(", ".join(images_copied)))

            # Hollow out child folder
            if not DRY_RUN:
                for fname in ["export.json", "summary.txt"]:
                    fp = os.path.join(child_path, fname)
                    if os.path.exists(fp):
                        os.remove(fp)
                # Delete images now that they've been copied to the base folder
                images_deleted = []
                for fname in os.listdir(child_path):
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in IMAGE_EXTS:
                        fp = os.path.join(child_path, fname)
                        os.remove(fp)
                        images_deleted.append(fname)
                if images_deleted:
                    print("    Images deleted from child: {}".format(
                        ", ".join(images_deleted)))
                with open(marker, "w", encoding="utf-8") as f:
                    f.write("Merged into: {}\n".format(base_name))
                    f.write("Merged at:   {}Z\n".format(datetime.utcnow().isoformat()))
                    f.write("Child type:  {}\n".format("reference" if is_ref else "checklist"))
                    if images_copied:
                        f.write("Images moved to parent: {}\n".format(
                            ", ".join(images_copied)))

            total_children += 1

        # Write updated base JSON
        if base_modified and not DRY_RUN:
            base_data["cards_found"] = len(base_data.get("checklist", []))
            with open(base_json, "w", encoding="utf-8") as f:
                json.dump(base_data, f, ensure_ascii=False, indent=2)

        cards_after = cards_before + group_cards_added
        print("  Cards: {} -> {} (+{}) | {} children processed".format(
            cards_before, cards_after, group_cards_added, len(children)))
        print()

        total_groups_done += 1
        total_cards_added += group_cards_added

    # Final summary
    print("=" * 60)
    print("Consolidation complete{}.".format(" [DRY RUN]" if DRY_RUN else ""))
    print("  Base sets processed:  {}".format(total_groups_done))
    print("  Orphan groups:        {}".format(total_orphans))
    print("  Children merged:      {}".format(total_children))
    print("  Checklist cards added: {}".format(total_cards_added))
    if warnings:
        print("\nWarnings ({}):".format(len(warnings)))
        for w in warnings:
            print("  ! " + w)


if __name__ == "__main__":
    consolidate()
