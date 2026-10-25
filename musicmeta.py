#!/usr/bin/env python3

import os
import re
import json
import sys

from mutagen.flac import FLAC
from mutagen.dsf import DSF
from mutagen.id3 import ID3, ID3NoHeaderError


AUDIO_EXTENSIONS = {".flac", ".dsf", ".mp3"}


def clean(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value).strip()


def first_nonempty(*values):
    for value in values:
        value = clean(value)
        if value:
            return value
    return ""


# ------------------------------------------------------------
# Embedded metadata
# ------------------------------------------------------------

def read_flac(path):
    result = {
        "artist": "",
        "album_artist": "",
        "album": "",
        "title": "",
        "track": "",
        "disc": "",
        "date": "",
        "genre": "",
        "composer": "",
        "conductor": "",
        "comment": "",
    }

    try:
        audio = FLAC(path)

        result["artist"] = clean(audio.get("artist"))
        result["album_artist"] = first_nonempty(
            audio.get("albumartist"),
            audio.get("album artist"),
        )
        result["album"] = clean(audio.get("album"))
        result["title"] = clean(audio.get("title"))
        result["track"] = clean(audio.get("tracknumber"))
        result["disc"] = clean(audio.get("discnumber"))
        result["date"] = first_nonempty(
            audio.get("date"),
            audio.get("year"),
        )
        result["genre"] = clean(audio.get("genre"))
        result["composer"] = clean(audio.get("composer"))
        result["conductor"] = clean(audio.get("conductor"))
        result["comment"] = clean(audio.get("comment"))

    except Exception as e:
        print(f"WARNING: cannot read FLAC tags: {path}: {e}")

    return result


def read_id3(path):
    result = {
        "artist": "",
        "album_artist": "",
        "album": "",
        "title": "",
        "track": "",
        "disc": "",
        "date": "",
        "genre": "",
        "composer": "",
        "conductor": "",
        "comment": "",
    }

    try:
        if os.path.splitext(path)[1].lower() == ".dsf":
            # In DSF the ID3 tag is not at the start of the file.
            tags = DSF(path).tags
            if tags is None:
                return result
        else:
            tags = ID3(path)

    except ID3NoHeaderError:
        # A DSF/MP3 file without an ID3 header is valid.
        return result

    except Exception as e:
        print(f"WARNING: cannot read ID3 tags: {path}: {e}")
        return result

    def text(frame_id):
        frame = tags.get(frame_id)
        if frame is None:
            return ""
        return clean(getattr(frame, "text", ""))

    result["artist"] = text("TPE1")
    result["album_artist"] = text("TPE2")
    result["album"] = text("TALB")
    result["title"] = text("TIT2")
    result["track"] = text("TRCK")
    result["disc"] = text("TPOS")
    result["date"] = text("TDRC")
    result["genre"] = text("TCON")
    result["composer"] = text("TCOM")
    result["conductor"] = text("TPE3")

    # COMM frames are keyed "COMM:<desc>:<lang>", so use getall().
    for comment in tags.getall("COMM"):
        try:
            value = clean(comment.text)
        except Exception:
            continue
        if value:
            result["comment"] = value
            break

    return result


def read_embedded(path):
    ext = os.path.splitext(path)[1].lower()

    if ext == ".flac":
        return read_flac(path)

    if ext in (".dsf", ".mp3"):
        return read_id3(path)

    return {}


# ------------------------------------------------------------
# CUE
# ------------------------------------------------------------

def cue_time_to_seconds(value):
    try:
        mm, ss, ff = value.split(":")
        return int(mm) * 60 + int(ss) + int(ff) / 75.0
    except Exception:
        return None


def parse_cue(path):
    result = {
        "path": path,
        "artist": "",
        "album": "",
        "date": "",
        "genre": "",
        "tracks": [],
    }

    current_track = None

    try:
        with open(path, "rb") as f:
            raw = f.read()

        # CUE files are often not UTF-8 (old Cyrillic rips use cp1251).
        for encoding in (("utf-16",) if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ()) + ("utf-8-sig", "cp1251", "cp1252"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")

        lines = text.splitlines()

    except Exception as e:
        print(f"WARNING: cannot read CUE: {path}: {e}")
        return result

    for raw_line in lines:
        line = raw_line.strip()

        m = re.match(r'^PERFORMER\s+"(.*)"$', line, re.I)
        if m:
            if current_track is None:
                result["artist"] = m.group(1)
            continue

        m = re.match(r'^TITLE\s+"(.*)"$', line, re.I)
        if m:
            if current_track is None:
                result["album"] = m.group(1)
            else:
                current_track["title"] = m.group(1)
            continue

        m = re.match(r'^REM\s+DATE\s+(.+)$', line, re.I)
        if m:
            result["date"] = m.group(1).strip()
            continue

        m = re.match(r'^REM\s+GENRE\s+(.+)$', line, re.I)
        if m:
            result["genre"] = m.group(1).strip()
            continue

        m = re.match(r'^TRACK\s+(\d+)\s+AUDIO$', line, re.I)
        if m:
            current_track = {
                "track": m.group(1),
                "title": "",
                "index_00": "",
                "index_01": "",
                "index_01_seconds": None,
            }
            result["tracks"].append(current_track)
            continue

        m = re.match(r'^INDEX\s+00\s+(.+)$', line, re.I)
        if m and current_track:
            current_track["index_00"] = m.group(1)
            continue

        m = re.match(r'^INDEX\s+01\s+(.+)$', line, re.I)
        if m and current_track:
            current_track["index_01"] = m.group(1)
            current_track["index_01_seconds"] = cue_time_to_seconds(
                m.group(1)
            )
            continue

    return result


def find_cue(path):
    directory = os.path.dirname(path)
    base = os.path.splitext(os.path.basename(path))[0]

    candidates = [
        os.path.join(directory, base + ".cue"),
        os.path.join(directory, base + ".CUE"),
    ]

    try:
        for name in os.listdir(directory):
            if name.lower().endswith(".cue"):
                candidates.append(os.path.join(directory, name))
    except Exception:
        pass

    seen = set()

    for candidate in candidates:
        candidate = os.path.abspath(candidate)

        if candidate in seen:
            continue

        seen.add(candidate)

        if os.path.isfile(candidate):
            return candidate

    return None


# ------------------------------------------------------------
# Path information
# ------------------------------------------------------------

def infer_path_info(path):
    directory = os.path.dirname(path)
    album_folder = os.path.basename(directory)

    result = {
        "artist": "",
        "album": "",
        "year": "",
        "release": "",
        "format_hint": "",
    }

    parent = os.path.basename(os.path.dirname(directory))

    # Artist / 2009 - Album (Release)
    #
    # Must come before "Artist - Album", otherwise the year is taken
    # as the artist.

    match = re.match(
        r"^(?P<year>(?:19|20)\d{2})\s+-\s+"
        r"(?P<album>.+?)"
        r"(?:\s+\((?P<release>[^)]+)\))?$",
        album_folder,
    )

    if match and parent:
        result["artist"] = parent
        result["year"] = match.group("year")
        result["album"] = clean(match.group("album"))
        result["release"] = clean(match.group("release"))

        return result

    # Artist - Album (Release) Format
    #
    # Example:
    # Miles Davis - Kind Of Blue (HDTT13879) DSD256

    pattern = re.compile(
        r"^(?P<artist>.+?)\s+-\s+"
        r"(?P<album>.+?)"
        r"(?:\s+\((?P<release>[^)]+)\))?"
        r"(?:\s+(?P<format>DSD[\w.]+|DXD|PCM.*))?$",
        re.I,
    )

    match = pattern.match(album_folder)

    if match:
        result["artist"] = clean(match.group("artist"))
        result["album"] = clean(match.group("album"))
        result["release"] = clean(match.group("release"))
        result["format_hint"] = clean(match.group("format"))

        return result

    # Artist / 2009 Album

    match = re.match(
        r"^(?P<year>\d{4})\s+(?P<album>.+)$",
        album_folder,
    )

    if match:
        result["artist"] = parent
        result["year"] = match.group("year")
        result["album"] = match.group("album")

        return result

    # Artist / Album

    if parent:
        result["artist"] = parent
        result["album"] = album_folder

    return result


# ------------------------------------------------------------
# Filename information
# ------------------------------------------------------------

def infer_filename_info(path):
    filename = os.path.splitext(os.path.basename(path))[0]

    result = {
        "track": "",
        "title": "",
    }

    # Examples:
    #
    # 01-So What
    # 02-Freddie Freeloader
    # 03_Blue In Green
    # 04. All Blues

    match = re.match(
        r"^(?P<track>\d{1,3})\s*[-_.]\s*(?P<title>.+)$",
        filename,
    )

    if match:
        result["track"] = match.group("track")
        result["title"] = match.group("title").strip()

        return result

    # Example:
    #
    # 01 So What

    match = re.match(
        r"^(?P<track>\d{1,3})\s+(?P<title>.+)$",
        filename,
    )

    if match:
        result["track"] = match.group("track")
        result["title"] = match.group("title").strip()

    return result


# ------------------------------------------------------------
# Metadata merge + source tracking
# ------------------------------------------------------------

def merge_metadata(
    embedded,
    path_info,
    filename_info,
    cue=None,
    cue_track=None,
):
    metadata = {
        "artist": "",
        "album_artist": "",
        "album": "",
        "title": "",
        "track": "",
        "disc": "",
        "date": "",
        "genre": "",
        "composer": "",
        "conductor": "",
        "comment": "",
    }

    sources = {}

    def choose(field, candidates):
        for source, value in candidates:
            value = clean(value)

            if value:
                metadata[field] = value
                sources[field] = source
                return

        sources[field] = None

    choose("artist", [
        ("embedded", embedded.get("artist")),
        ("cue", cue_track.get("artist") if cue_track else ""),
        ("cue", cue.get("artist") if cue else ""),
        ("path", path_info.get("artist")),
    ])

    choose("album_artist", [
        ("embedded", embedded.get("album_artist")),
        ("cue", cue.get("artist") if cue else ""),
        ("path", path_info.get("artist")),
        ("derived", metadata["artist"]),
    ])

    choose("album", [
        ("embedded", embedded.get("album")),
        ("cue", cue.get("album") if cue else ""),
        ("path", path_info.get("album")),
    ])

    choose("title", [
        ("embedded", embedded.get("title")),
        ("cue", cue_track.get("title") if cue_track else ""),
        ("filename", filename_info.get("title")),
    ])

    choose("track", [
        ("embedded", embedded.get("track")),
        ("cue", cue_track.get("track") if cue_track else ""),
        ("filename", filename_info.get("track")),
    ])

    choose("disc", [
        ("embedded", embedded.get("disc")),
    ])

    choose("date", [
        ("embedded", embedded.get("date")),
        ("cue", cue.get("date") if cue else ""),
        ("path", path_info.get("year")),
    ])

    choose("genre", [
        ("embedded", embedded.get("genre")),
        ("cue", cue.get("genre") if cue else ""),
    ])

    choose("composer", [
        ("embedded", embedded.get("composer")),
    ])

    choose("conductor", [
        ("embedded", embedded.get("conductor")),
    ])

    choose("comment", [
        ("embedded", embedded.get("comment")),
    ])

    missing = [
        field
        for field, value in metadata.items()
        if not value
    ]

    core_fields = [
        "artist",
        "album_artist",
        "album",
        "title",
        "track",
    ]

    if all(metadata[field] for field in core_fields):
        confidence = "HIGH"

    elif all(
        metadata[field]
        for field in ["artist", "album", "title"]
    ):
        confidence = "MEDIUM"

    else:
        confidence = "LOW"

    return {
        "metadata": metadata,
        "sources": sources,
        "missing": missing,
        "confidence": confidence,
    }


# ------------------------------------------------------------
# Scan
# ------------------------------------------------------------

def scan(source):
    source = os.path.abspath(source)

    files = []

    for root, dirs, filenames in os.walk(source):
        dirs.sort()
        filenames.sort()

        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()

            if ext in AUDIO_EXTENSIONS:
                files.append(os.path.join(root, filename))

    files.sort()

    print(f"Scanning: {source}")
    print(f"Found {len(files)} audio files")

    tracks = []

    for index, path in enumerate(files, 1):
        print(f"[{index}/{len(files)}] {path}")

        ext = os.path.splitext(path)[1].lower()

        embedded = read_embedded(path)
        path_info = infer_path_info(path)
        filename_info = infer_filename_info(path)

        cue_path = find_cue(path)

        cue = None

        if cue_path:
            cue = parse_cue(cue_path)

        # ----------------------------------------------------
        # CUE image
        # ----------------------------------------------------

        if cue and cue["tracks"]:
            logical_tracks = []

            for cue_track in cue["tracks"]:
                merged = merge_metadata(
                    embedded,
                    path_info,
                    filename_info,
                    cue,
                    cue_track,
                )

                logical_tracks.append({
                    "track": cue_track["track"],
                    "title": cue_track["title"],
                    "index_00": cue_track["index_00"],
                    "index_01": cue_track["index_01"],
                    "index_01_seconds": cue_track["index_01_seconds"],
                    "metadata": merged["metadata"],
                    "sources": merged["sources"],
                    "missing": merged["missing"],
                    "confidence": merged["confidence"],
                })

            item = {
                "path": path,
                "format": ext[1:].upper(),
                "embedded": embedded,
                "path_info": path_info,
                "filename_info": filename_info,
                "cue": cue,
                "metadata": None,
                "sources": None,
                "missing": None,
                "confidence": None,
                "tracks": logical_tracks,
            }

        else:
            merged = merge_metadata(
                embedded,
                path_info,
                filename_info,
                cue,
                None,
            )

            item = {
                "path": path,
                "format": ext[1:].upper(),
                "embedded": embedded,
                "path_info": path_info,
                "filename_info": filename_info,
                "cue": cue,
                "metadata": merged["metadata"],
                "sources": merged["sources"],
                "missing": merged["missing"],
                "confidence": merged["confidence"],
            }

        tracks.append(item)

    return {
        "version": 4,
        "source": source,
        "tracks": tracks,
    }


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    if len(sys.argv) != 2:
        print("Usage:")
        print("  python3 musicmeta.py <folder>")
        sys.exit(1)

    source = sys.argv[1]

    if not os.path.isdir(source):
        print(f"ERROR: folder not found: {source}")
        sys.exit(1)

    result = scan(source)

    output = os.path.join(
        os.path.abspath(source),
        "metadata.json",
    )

    with open(
        output,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            result,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print()
    print(f"JSON written to: {output}")


if __name__ == "__main__":
    main()
