#!/usr/bin/env python3
"""
ai_fill.py

Fill (and optionally correct) the "metadata" block of a metadata.json with a
local Ollama model, following the rules in promt.txt.

The input file is never modified: the result goes to a new JSON file that has
the same structure as the AI-corrected file (changes / review / unresolved per
track plus a top-level "albums" list), so it can be passed straight to
apply_metadata.py.

Tracks are sent to the model one album (folder) at a time.

Modes:
  default     only EMPTY fields are filled; existing values are never touched
  --correct   the model may also change existing values (typos, case, junk)

Usage:
  python ai_fill.py test/metadata.json
  python ai_fill.py test/metadata.json --model qwen2.5:14b
  python ai_fill.py test/metadata.json --correct -o metadata_corrected.json
  python ai_fill.py test/metadata.json --think show   # watch the reasoning
  python ai_fill.py test/metadata.json --think hide   # think, but print only the answer (default)

Needs a running Ollama (ollama serve) and a pulled model (ollama pull <name>).
No extra Python packages are required.
"""

import argparse
import collections
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

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

# Only these six fields are sent to the model and changed. track, disc,
# composer, conductor and comment are left exactly as they are.
REQUIRED = ["artist", "album_artist", "album", "title", "date", "genre"]

# Must be the same for every track of an album.
ALBUM_LEVEL = ["album_artist", "album", "date", "genre"]

HERE = os.path.dirname(os.path.abspath(__file__))

# Replaces the OUTPUT section of promt.txt: the model sees one album at a
# time, so it must not return the whole document.
OUTPUT_RULES = """
=====================================================================
OUTPUT (overrides any other output description)
=====================================================================
You receive ONE album as {"folder": "...", "tracks": [ ... ]}.
Each track has an "i" number, "file", "embedded", "path_info",
"filename_info", "cue" and the current "metadata".

If the input has "album_context", those album-level values were already decided
for earlier tracks of the same album: use exactly these values for album_artist,
album, date and genre.

Work ONLY on these six fields: artist, album_artist, album, title, date, genre.
Ignore every rule above about track, disc, composer, conductor and comment:
do not output them and do not change them.

Return ONE JSON object and nothing else:

{
  "tracks": [
    {
      "i": <same number as in the input>,
      "metadata": { "artist": "", "album_artist": "", "album": "",
                    "title": "", "date": "", "genre": "" },
      "changes": [ {"field": "...", "from": "...", "to": "...", "reason": "..."} ],
      "review": [ "short note", ... ],
      "unresolved": [ "field", ... ]
    }
  ],
  "issues": [ "short note about the album", ... ]
}

Return every track of the input, in the same order. All values are strings.
"""


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def load_prompt(path):
    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    # Keep the rules, drop the whole-document OUTPUT / FINAL CHECK sections.
    parts = re.split(r"=+\s*\nOUTPUT\s*\n=+", text, maxsplit=1)
    return parts[0].rstrip() + "\n" + OUTPUT_RULES


def normalize(field, value):
    value = clean(value)

    if field == "date":
        match = re.search(r"(?:19|20)\d{2}", value)
        return match.group(0) if match else value

    return value


# ------------------------------------------------------------
# Ollama
# ------------------------------------------------------------

def http_json(url, payload=None, timeout=600):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check_ollama(host, model):
    """Return the model name to use (the first installed one if none given)."""
    try:
        tags = http_json(f"{host}/api/tags", timeout=10)
    except (urllib.error.URLError, OSError) as e:
        sys.exit(
            f"Cannot reach Ollama at {host}: {e}\n"
            "Start it with `ollama serve` (or open the Ollama app)."
        )

    names = [m.get("name", "") for m in tags.get("models", [])]

    if not model:
        if not names:
            sys.exit("No Ollama models installed. Run: ollama pull qwen2.5:14b")
        return names[0]

    wanted = model if ":" in model else model + ":latest"

    if wanted not in names:
        available = ", ".join(names) if names else "none"
        sys.exit(
            f"Model '{model}' is not installed. Run: ollama pull {model}\n"
            f"Installed models: {available}"
        )

    return model


def ask(host, model, system, user, num_ctx, timeout, live=True, show_thinking=False):
    """Stream the answer; print thinking (grey) and output live. Returns the output text."""
    payload = {
        "model": model,
        "stream": True,
        "options": {"temperature": 0, "num_ctx": num_ctx},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    # Thinking is always requested. gpt-oss takes a level, others a boolean.
    # No "format": "json" here: a JSON grammar can suppress the reasoning
    # stream, so the answer is parsed leniently instead (see parse_json).
    payload["think"] = "medium" if "gpt-oss" in model else True

    request = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    content = []
    mode = None

    def show(kind, text):
        nonlocal mode
        if not live or not text:
            return
        if kind == "thinking" and not show_thinking:
            return
        if kind != mode:
            label = "thinking" if kind == "thinking" else "answer"
            color = "\033[90m" if kind == "thinking" else "\033[0m"
            sys.stdout.write(f"\033[0m\n    --- {label} ---\n    {color}")
            mode = kind
        sys.stdout.write(text.replace("\n", "\n    "))
        sys.stdout.flush()

    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            raise ValueError("this model does not support thinking, use another model")
        raise

    with response:
        for raw in response:
            if not raw.strip():
                continue
            chunk = json.loads(raw.decode("utf-8"))
            if chunk.get("error"):
                raise ValueError(chunk["error"])
            message = chunk.get("message", {})
            show("thinking", message.get("thinking", ""))
            if chunk.get("done") and chunk.get("done_reason") == "length":
                print("\n    WARNING: answer was cut off (context full). "
                      "Use a smaller --batch or a bigger --num-ctx.")
            piece = message.get("content", "")
            show("answer", piece)
            content.append(piece)

    if live:
        sys.stdout.write("\n")
    return "".join(content)


def parse_json(text):
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


# ------------------------------------------------------------
# Items (one item = one logical track)
# ------------------------------------------------------------

def collect_items(data):
    """Flatten the tracks. A CUE image contributes one item per nested track."""
    items = []

    for parent in data.get("tracks", []):
        entries = parent.get("tracks") or [parent]

        for entry in entries:
            meta = entry.get("metadata")
            if not isinstance(meta, dict):
                meta = {}
                entry["metadata"] = meta
            for field in FIELDS:
                meta.setdefault(field, "")

            items.append({"parent": parent, "entry": entry})

    return items


def group_by_folder(items):
    groups = {}
    for item in items:
        folder = os.path.dirname(item["parent"].get("path", ""))
        groups.setdefault(folder, []).append(item)
    return groups


def only_required(values):
    values = values or {}
    return {f: clean(values.get(f)) for f in REQUIRED}


def payload_for(folder, items, context=None):
    tracks = []

    for number, item in enumerate(items):
        parent, entry = item["parent"], item["entry"]
        is_nested = entry is not parent

        cue = parent.get("cue")
        cue_info = None
        if cue:
            cue_info = {k: cue.get(k, "") for k in ("artist", "album", "date", "genre")}

        tracks.append(
            {
                "i": number,
                "file": os.path.basename(parent.get("path", "")),
                "embedded": only_required(parent.get("embedded")),
                "path_info": parent.get("path_info", {}),
                "filename_info": {
                    "title": clean((entry.get("filename_info") or parent.get("filename_info") or {}).get("title"))
                },
                "cue": cue_info,
                "metadata": only_required(entry["metadata"]),
            }
        )

    payload = {"folder": os.path.basename(folder), "tracks": tracks}
    if context:
        payload["album_context"] = context
    return payload


# ------------------------------------------------------------
# Merge
# ------------------------------------------------------------

def merge_result(items, result, correct):
    """Apply one model answer to the items. Returns (changed_fields, issues)."""
    answers = {}
    for row in result.get("tracks", []):
        try:
            answers[int(row.get("i"))] = row
        except (TypeError, ValueError):
            continue

    changed = 0

    for number, item in enumerate(items):
        entry = item["entry"]
        meta = entry["metadata"]
        row = answers.get(number)

        reasons = {}
        for change in (row or {}).get("changes", []) or []:
            if isinstance(change, dict):
                reasons[change.get("field")] = clean(change.get("reason"))

        proposed = (row or {}).get("metadata") or {}
        changes = []
        sources = entry.setdefault("sources", {})

        for field in REQUIRED:
            old = clean(meta.get(field))
            new = normalize(field, proposed.get(field))

            if not new or new == old:
                continue
            if old and not correct:
                continue

            entry.setdefault("_prev", {})[field] = sources.get(field)
            meta[field] = new
            sources[field] = "ai"
            changes.append(
                {
                    "field": field,
                    "from": old,
                    "to": new,
                    "reason": reasons.get(field) or ("filled" if not old else "corrected"),
                }
            )

        review = [clean(x) for x in (row or {}).get("review", []) or [] if clean(x)]

        unresolved = [
            clean(x) for x in (row or {}).get("unresolved", []) or [] if clean(x) in REQUIRED
        ]

        if row is None:
            review.append("no answer from the model for this track")

        for field in REQUIRED:
            if not clean(meta.get(field)):
                if field not in unresolved:
                    unresolved.append(field)
                review.append(f"{field}: no evidence, please verify")

        entry["missing"] = [f for f in FIELDS if not clean(meta.get(f))]
        entry["changes"] = changes
        entry["review"] = review
        entry["unresolved"] = unresolved

        if any(not clean(meta.get(f)) for f in REQUIRED):
            entry["confidence"] = "LOW"
        elif review:
            entry["confidence"] = "MEDIUM"
        else:
            entry["confidence"] = "HIGH"

        changed += len(changes)

    issues = [clean(x) for x in result.get("issues", []) or [] if clean(x)]
    return changed, issues


def majority(items, field):
    """Most common non-empty value of a field in the items ('' if none)."""
    counts = collections.Counter(
        clean(i["entry"]["metadata"].get(field)) for i in items
    )
    counts.pop("", None)
    return counts.most_common(1)[0][0] if counts else ""


def album_context(items):
    context = {f: majority(items, f) for f in ALBUM_LEVEL}
    return {k: v for k, v in context.items() if v}


def harmonize(items):
    """Make album-level fields identical across an album.

    Only values the model itself set are changed; they are pulled to the
    album's most common value. Returns the number of fixed tracks.
    """
    fixed = 0

    for field in ALBUM_LEVEL:
        common = majority(items, field)
        if not common:
            continue

        for item in items:
            entry = item["entry"]
            meta = entry["metadata"]
            if clean(meta.get(field)) == common or entry["sources"].get(field) != "ai":
                continue

            change = next((c for c in entry["changes"] if c["field"] == field), None)
            meta[field] = common
            fixed += 1

            if change and clean(change["from"]) == common:
                entry["changes"].remove(change)
                entry["sources"][field] = entry.get("_prev", {}).get(field)
            elif change:
                change["to"] = common
                change["reason"] += " (aligned with the rest of the album)"

            entry["review"].append(f"{field}: aligned to the album value '{common}'")
            if entry["confidence"] == "HIGH":
                entry["confidence"] = "MEDIUM"

    return fixed


def has_blank(item):
    return any(not clean(item["entry"]["metadata"].get(f)) for f in REQUIRED)


def init_skipped(item):
    """Bookkeeping for a track that is not sent to the model."""
    entry = item["entry"]
    entry.setdefault("sources", {})
    entry["missing"] = [f for f in FIELDS if not clean(entry["metadata"].get(f))]
    entry["changes"] = []
    entry["review"] = []
    entry["unresolved"] = []
    entry["confidence"] = "HIGH"


def process_group(args, system, folder, items):
    """Ask the model about one album, in batches. Returns (changed, issues)."""
    changed, issues = 0, []

    # Without --correct only tracks with a blank required field need the model.
    if args.correct:
        todo = items
    else:
        todo = [i for i in items if has_blank(i)]
        for item in items:
            if item not in todo:
                init_skipped(item)

    if not todo:
        print("    nothing to fill, skipped")
        return changed, issues

    size = args.batch or len(todo)  # 0 = all tracks in one request

    for start in range(0, len(todo), size):
        part = todo[start : start + size]
        # Values already present or decided for this album (including the
        # tracks that are not sent) keep the album-level fields consistent.
        context = album_context(items) or None
        user = json.dumps(payload_for(folder, part, context), ensure_ascii=False)

        result = None
        for attempt in range(1, args.retries + 1):
            try:
                text = ask(
                    args.host, args.model, system, user, args.num_ctx,
                    args.timeout, not args.quiet, args.think == "show",
                )
                result = parse_json(text)
                break
            except (ValueError, KeyError) as e:
                print(f"    attempt {attempt}: invalid answer ({e})")
            except (urllib.error.URLError, OSError) as e:
                print(f"    attempt {attempt}: request failed ({e})")
                time.sleep(2)

        if result is None:
            for item in part:
                item["entry"].setdefault("review", []).append("AI failed for this track")
            issues.append("AI did not return valid JSON for some tracks")
            continue

        count, notes = merge_result(part, result, args.correct)
        changed += count
        issues.extend(notes)

    fixed = harmonize(items)
    if fixed:
        issues.append(f"{fixed} value(s) aligned to the rest of the album")

    for item in items:
        item["entry"].pop("_prev", None)

    return changed, issues


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fill empty metadata fields with a local Ollama model."
    )
    parser.add_argument("json_file")
    parser.add_argument(
        "-o",
        "--output",
        help="output file (default: <input>_ai.json next to the input)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OLLAMA_MODEL"),
        help="Ollama model (default: $OLLAMA_MODEL, else the first installed model)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("OLLAMA_HOST_URL", "http://localhost:11434"),
        help="Ollama URL (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--prompt",
        default=os.path.join(HERE, "promt.txt"),
        help="prompt file (default: promt.txt next to this script)",
    )
    parser.add_argument(
        "--correct",
        action="store_true",
        help="also let the model change existing values (default: fill empty fields only)",
    )
    parser.add_argument(
        "--think",
        "--thinking",
        dest="think",
        choices=["show", "hide"],
        default="hide",
        help="show: print the model's thinking and its answer; "
        "hide: print only the answer (default). Needs a model with thinking support",
    )
    parser.add_argument("--quiet", action="store_true", help="print neither thinking nor answer live")
    parser.add_argument("--num-ctx", type=int, default=32768, help="context size (default 32768)")
    parser.add_argument(
        "--batch",
        type=int,
        default=4,
        help="max tracks per request (default 4, keeps the context small); "
        "0 = the whole album in one request",
    )
    parser.add_argument("--retries", type=int, default=2, help="tries per request (default 2)")
    parser.add_argument("--timeout", type=int, default=600, help="seconds per request (default 600)")
    args = parser.parse_args()

    output = args.output
    if not output:
        root, ext = os.path.splitext(args.json_file)
        output = f"{root}_ai{ext or '.json'}"

    if os.path.abspath(output) == os.path.abspath(args.json_file):
        sys.exit("Output must differ from the input file.")

    with open(args.json_file, encoding="utf-8") as handle:
        data = json.load(handle)

    system = load_prompt(args.prompt)
    args.model = check_ollama(args.host, args.model)

    items = collect_items(data)
    groups = group_by_folder(items)

    mode = "correct + fill" if args.correct else "fill empty fields only"
    print(f"Model: {args.model}   Mode: {mode}")
    print(f"{len(items)} tracks in {len(groups)} albums\n")

    albums = []
    total = 0

    for number, (folder, group) in enumerate(groups.items(), 1):
        print(f"[{number}/{len(groups)}] {os.path.basename(folder)} ({len(group)} tracks)")
        changed, issues = process_group(args, system, folder, group)
        total += changed
        print(f"    {changed} field(s) changed")

        first = group[0]["entry"]["metadata"]
        for item in group:
            for note in item["entry"].get("review", []):
                if note not in issues:
                    issues.append(note)

        albums.append(
            {
                "folder": os.path.basename(folder),
                "tracks": len(group),
                "album_artist": clean(first.get("album_artist")),
                "album": clean(first.get("album")),
                "date": clean(first.get("date")),
                "issues": issues,
            }
        )

    data["albums"] = albums

    with open(output, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    flagged = sum(1 for a in albums if a["issues"])
    print(f"\nFields changed: {total}. Albums with notes to review: {flagged}.")
    print(f"Saved: {output}")
    print(f"Next:  python apply_metadata.py {output}")


if __name__ == "__main__":
    main()
