#!/usr/bin/env python3
"""
apply_metadata.py

Write the "metadata" block of a (corrected) metadata.json into the tags
of the audio files (MP3, FLAC, DSF).

Safe by default:
  - dry run: only prints what would change, nothing is written
  - --apply is required to modify files
  - only fields whose value differs from the file's current tag are touched
  - other tags and cover art are preserved
  - written files are re-read and checked

Usage:
  python apply_metadata.py metadata_corrected.json
  python apply_metadata.py metadata_corrected.json --apply
  python apply_metadata.py metadata_corrected.json --apply --backup-dir ./backup
  python apply_metadata.py metadata_corrected.json --formats mp3
  python apply_metadata.py metadata_corrected.json --fields artist,album_artist,album,title,track
  python apply_metadata.py metadata_corrected.json --fields all

By default artist, album_artist, album, title, date and genre are written.
track, disc, composer, conductor and comment need --fields.
"""

import argparse
import os
import shutil
import sys

from mutagen.dsf import DSF
from mutagen.flac import FLAC
from mutagen.id3 import (
    COMM,
    ID3,
    ID3NoHeaderError,
    TALB,
    TCON,
    TCOM,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPE3,
    TPOS,
    TRCK,
)

from musicmeta import clean, read_embedded


FIELDS = [
    "artist",
    "album_artist",
    "album",
    "title",
    "track",
    "disc",
    "date",
    "genre",
    "composer",
    "conductor",
    "comment",
]

# Written by default. Everything else is opt-in with --fields.
CORE_FIELDS = [
    "artist",
    "album_artist",
    "album",
    "title",
    "date",
    "genre",
]

# Fields written for a single-file CUE image (one FLAC = whole album):
# per-track fields like title/track would be wrong for the whole file.
ALBUM_FIELDS = ["artist", "album_artist", "album", "date", "genre"]

ID3_FRAMES = {
    "artist": ("TPE1", TPE1),
    "album_artist": ("TPE2", TPE2),
    "album": ("TALB", TALB),
    "title": ("TIT2", TIT2),
    "track": ("TRCK", TRCK),
    "disc": ("TPOS", TPOS),
    "date": ("TDRC", TDRC),
    "genre": ("TCON", TCON),
    "composer": ("TCOM", TCOM),
    "conductor": ("TPE3", TPE3),
}

FLAC_KEYS = {
    "artist": "artist",
    "album_artist": "albumartist",
    "album": "album",
    "title": "title",
    "track": "tracknumber",
    "disc": "discnumber",
    "date": "date",
    "genre": "genre",
    "composer": "composer",
    "conductor": "conductor",
    "comment": "comment",
}


# ------------------------------------------------------------
# Writers
# ------------------------------------------------------------

def apply_id3(tags, changes):
    for field, value in changes.items():
        if field == "comment":
            tags.delall("COMM")
            if value:
                tags.add(COMM(encoding=3, lang="eng", desc="", text=[value]))
            continue

        frame_id, frame_cls = ID3_FRAMES[field]
        tags.delall(frame_id)
        if value:
            tags.add(frame_cls(encoding=3, text=[value]))


def write_mp3(path, changes):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    apply_id3(tags, changes)
    # ID3v2.3 is the most compatible version for media servers/players.
    tags.save(path, v2_version=3)


def write_dsf(path, changes):
    audio = DSF(path)
    if audio.tags is None:
        audio.add_tags()

    apply_id3(audio.tags, changes)
    audio.save()


def write_flac(path, changes):
    audio = FLAC(path)

    for field, value in changes.items():
        key = FLAC_KEYS[field]
        if value:
            audio[key] = [value]
        elif key in audio:
            del audio[key]

    audio.save()


WRITERS = {
    ".mp3": write_mp3,
    ".dsf": write_dsf,
    ".flac": write_flac,
}


# ------------------------------------------------------------
# Planning
# ------------------------------------------------------------

def desired_for(track, selected):
    """Return (metadata dict, fields to write) for one JSON entry."""
    nested = track.get("tracks")

    if nested:
        # Single-file CUE image: album-level tags only.
        first = nested[0].get("metadata") or {}
        return first, [f for f in ALBUM_FIELDS if f in selected]

    return track.get("metadata"), [f for f in FIELDS if f in selected]


def plan_changes(path, metadata, fields):
    current = read_embedded(path)
    changes = {}

    for field in fields:
        wanted = clean(metadata.get(field, ""))
        have = clean(current.get(field, ""))

        if wanted != have:
            changes[field] = wanted

    return current, changes


def verify(path, changes):
    after = read_embedded(path)
    bad = []

    for field, wanted in changes.items():
        got = clean(after.get(field, ""))
        if got != wanted:
            bad.append((field, wanted, got))

    return bad


def backup_file(path, backup_dir):
    relative = os.path.abspath(path).lstrip(os.sep)
    target = os.path.join(backup_dir, relative)

    if os.path.exists(target):
        return

    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(path, target)


# ------------------------------------------------------------
# Report
# ------------------------------------------------------------

def shorten(value, width):
    value = value.replace("\n", " ")
    if len(value) <= width:
        return value
    return value[: width - 1] + "…"


def print_table(rows):
    """Print rows [(album, file, field, old, new)] as an aligned table."""
    headers = ("Album", "File", "Field", "Was", "Now")
    limits = (30, 38, 13, 34, 34)

    shown = []
    previous = (None, None)
    for album, name, field, old, new in rows:
        # Repeat album/file only when they change, to keep the table calm.
        a = album if album != previous[0] else ""
        f = name if (album, name) != previous else ""
        previous = (album, name)
        shown.append(
            (
                shorten(a, limits[0]),
                shorten(f, limits[1]),
                field,
                shorten(old if old else "(empty)", limits[3]),
                shorten(new if new else "(removed)", limits[4]),
            )
        )

    widths = [
        max(len(headers[i]), *(len(r[i]) for r in shown)) for i in range(len(headers))
    ]

    def line(cells):
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"

    print(line(headers))
    print("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for r in shown:
        print(line(r))


def write_csv(path, rows):
    import csv

    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["album", "file", "field", "was", "now"])
        writer.writerows(rows)


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Write metadata.json values into audio file tags."
    )
    parser.add_argument("json_file")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="really write tags (default is a dry run)",
    )
    parser.add_argument(
        "--formats",
        default="mp3,flac,dsf",
        help="comma separated list: mp3,flac,dsf (default: mp3,flac,dsf)",
    )
    parser.add_argument(
        "--fields",
        default=",".join(CORE_FIELDS),
        help="comma separated fields to write, or 'all' "
        f"(default: {','.join(CORE_FIELDS)}; available: {','.join(FIELDS)})",
    )
    parser.add_argument(
        "--csv",
        help="also save the table of changes as a CSV file (opens in Numbers/Excel)",
    )
    parser.add_argument(
        "--backup-dir",
        help="copy each file here before modifying it (needs free space)",
    )
    args = parser.parse_args()

    import json

    with open(args.json_file, encoding="utf-8") as handle:
        data = json.load(handle)

    formats = {"." + f.strip().lower() for f in args.formats.split(",") if f.strip()}
    unknown = formats - set(WRITERS)
    if unknown:
        sys.exit(f"Unknown formats: {', '.join(sorted(unknown))}")

    if args.fields.strip().lower() == "all":
        selected = list(FIELDS)
    else:
        selected = [f.strip() for f in args.fields.split(",") if f.strip()]
        bad = [f for f in selected if f not in FIELDS]
        if bad:
            sys.exit(f"Unknown fields: {', '.join(bad)}")

    mode = "APPLY" if args.apply else "DRY RUN (nothing is written)"
    print(f"Mode: {mode}")
    print(f"Formats: {', '.join(sorted(f[1:] for f in formats))}")
    print(f"Fields:  {', '.join(selected)}")
    print()

    stats = {"changed": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    rows = []

    for track in data.get("tracks", []):
        path = track.get("path", "")
        ext = os.path.splitext(path)[1].lower()
        name = os.path.basename(path)

        if ext not in formats:
            stats["skipped"] += 1
            continue

        if not os.path.isfile(path):
            print(f"ERROR   {name}: file not found: {path}")
            stats["errors"] += 1
            continue

        metadata, fields = desired_for(track, selected)

        if not metadata:
            print(f"SKIP    {name}: no metadata block")
            stats["skipped"] += 1
            continue

        try:
            current, changes = plan_changes(path, metadata, fields)
        except Exception as e:
            print(f"ERROR   {name}: cannot read tags: {e}")
            stats["errors"] += 1
            continue

        if not changes:
            stats["unchanged"] += 1
            continue

        album_name = os.path.basename(os.path.dirname(path))
        file_label = name + (" [CUE image]" if track.get("tracks") else "")
        file_rows = [
            (album_name, file_label, field, clean(current.get(field, "")), value)
            for field, value in changes.items()
        ]

        if args.apply:
            try:
                if args.backup_dir:
                    backup_file(path, args.backup_dir)

                WRITERS[ext](path, changes)

                problems = verify(path, changes)
            except Exception as e:
                print(f"ERROR   {name}: write failed: {e}")
                stats["errors"] += 1
                continue

            if problems:
                for field, wanted, got in problems:
                    print(f"ERROR   {name}: {field} expected {wanted!r}, found {got!r}")
                stats["errors"] += 1
                continue

        stats["changed"] += 1
        rows.extend(file_rows)

    if rows:
        print_table(rows)
        if args.csv:
            write_csv(args.csv, rows)
            print(f"\nCSV saved: {args.csv}")

    print()
    verb = "written" if args.apply else "to change"
    print(
        f"Files {verb}: {stats['changed']}, already up to date: {stats['unchanged']}, "
        f"skipped: {stats['skipped']}, errors: {stats['errors']}"
    )

    if not args.apply and stats["changed"]:
        print("Dry run only. Re-run with --apply to write the tags.")

    if stats["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
