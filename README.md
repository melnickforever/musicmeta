# Music metadata pipeline

Scan a music folder, let an AI fill/correct the tags, then write them into the
files (MP3, FLAC, DSF).

```
musicmeta.py  ->  metadata.json  ->  AI (ai_fill.py or any chat AI + promt.txt)
              ->  metadata_corrected.json  ->  apply_metadata.py  ->  tags in files
```

| File | What it does |
|------|--------------|
| `musicmeta.py` | Scans a folder, reads tags / CUE / folder names, writes `<folder>/metadata.json`. Read-only. |
| `promt.txt` | Rules for the AI (required fields, formats, review flags). |
| `ai_fill.py` | Sends `metadata.json` to a **local Ollama** model and saves a corrected JSON. Never touches audio files. |
| `apply_metadata.py` | Writes the JSON values into the audio tags. **Dry run by default.** |

## Install

Only one Python package is needed: **mutagen**. Python 3.9+ is required.
`ai_fill.py` uses only the standard library. For AI you also need
[Ollama](https://ollama.com) (not a Python package).

### macOS

The system Python blocks `pip install` (PEP 668), so use a virtual environment:

```bash
cd metadata
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install mutagen
```

Activate the virtual environment once before running scripts. All scripts can
then be run with `python3`.

### Linux (Debian/Ubuntu)

```bash
sudo apt install python3 python3-venv python3-pip
cd metadata
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install mutagen
```

Other distros: install `python3` and `python3-venv` with your package manager,
the rest is the same. (`sudo apt install python3-mutagen` also works without a venv.)
If you use the system package instead of a virtual environment, no activation is
needed.

### Ollama (for `ai_fill.py`)

Install from https://ollama.com (macOS app, or on Linux
`curl -fsSL https://ollama.com/install.sh | sh`), then pull a model:

```bash
ollama pull gpt-oss:20b
ollama list
```

Ollama must be running (the app, or `ollama serve`). "Expose Ollama to the
network" is **not** needed: the script talks to `localhost:11434`.

## Usage

### 1. Scan

```bash
python3 musicmeta.py /path/to/music
```

Writes `/path/to/music/metadata.json`. Folders like `Artist/2009 - Album`,
`Artist - Album (Release) DSD256`, CUE images and embedded tags are understood.

### 2. Fill / correct with AI

Option A, local Ollama:

```bash
python3 ai_fill.py /path/to/music/metadata.json --model gpt-oss:20b -o metadata_corrected.json
```

- Only the six required fields are processed: artist, album_artist, album, title, date, genre. Track numbers, disc, composer, conductor and comment are not sent to the model and are never changed.
- Default: fills only **empty** fields.
- **Recommended model: `gpt-oss:20b`**, it gave good results on this data.
- `--correct`: the model may also change existing values (typos, case).
- The model output is shown live. `--quiet` hides it.
- `--think show|hide`: `hide` (default) = the model thinks but only the answer is printed; `show` = the thinking is printed too. The model must support thinking (e.g. `gpt-oss:20b`).
- If the answer is cut off or the JSON is invalid: `--batch 15` (fewer tracks per request; by default a whole album is sent at once so its values stay consistent) or a bigger `--num-ctx`.
- Other options: `--model`, `--host`, `--prompt`, `--retries`, `--timeout`.

Option B, any chat AI: paste `promt.txt` followed by `metadata.json`, save the
JSON answer as `metadata_corrected.json`.

Always read `review` / `unresolved` in the result and the `albums` list.
Small local models can invent composers or change capitalisation.

### 3. Write tags

Dry run first (nothing is written, prints a table of changes):

```bash
python3 apply_metadata.py metadata_corrected.json
```

Write for real:

```bash
python3 apply_metadata.py metadata_corrected.json --apply --backup-dir ./backup
```

Options:

| Option | Meaning |
|--------|---------|
| `--apply` | really write (otherwise dry run) |
| `--backup-dir DIR` | copy each file there before changing it (needs free space) |
| `--formats mp3,flac,dsf` | which formats to touch (default: all three) |
| `--fields a,b,c` or `all` | default: artist, album_artist, album, title, date, genre. Add track, disc, composer, conductor, comment if needed |
| `--csv changes.csv` | save the table of changes (opens in Numbers/Excel) |

How it writes: only fields that differ are changed; cover art and other tags
are kept; the file is re-read and checked after writing. MP3 is saved as
ID3v2.3, DSF as ID3v2.4. A single-file CUE image gets album-level fields only.

## Safety checklist

1. Back up the library (or use `--backup-dir`).
2. Run the dry run and read the table.
3. Try one album first (copy it to a test folder and scan that).
4. Then run the full `--apply`.

Wrong tags from bad AI data are the main risk, not broken audio. The scripts
only edit tags; audio data is not rewritten.

## Troubleshooting

- `No module named 'mutagen'`: activate the venv with `source .venv/bin/activate`,
  or install `python3-mutagen` with your distro package manager.
- `Cannot reach Ollama`: start the Ollama app or `ollama serve`.
- `Model '...' is not installed`: `ollama pull <model>`.
- `invalid answer` / `answer was cut off`: lower `--batch`, raise `--num-ctx`.
- DSF/MP3 tag not updated in the media server: rescan the library in the server.
