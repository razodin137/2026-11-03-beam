# Beam

Drop a link, PDF, note, or text file into `inbox/`. Beam asks a decision model where it belongs, then asks again how much model to spend, then builds a self-contained, mobile-responsive HTML brief beside the source.

Built for turning long videos and articles into skimmable visual documents: exact quotes, key figures, the through-line, why it matters, and next steps — without rewatching the whole thing.

```
inbox/ ──▶ classify (deepseek) ──▶ sorted/<category>/
                │
                ├──▶ route (deepseek) ──▶ cheap  (glm-5.3-flash:cloud)
                │                     └──▶ premium (deepseek-v4.1-flash:1017-cloud)
                │
                └──▶ extract ──▶ <slug>.html + <slug>.json + source + cover
```

The library index lives at `index.html` in this folder.

## Quick start

```bash
./beam.py init
./beam.py doctor --test        # checks Ollama, models, yt-dlp, pdftotext

./beam.py add "https://www.youtube.com/watch?v=T-D1OfcDW1M"
./beam.py add "https://en.wikipedia.org/wiki/Retrieval-augmented_generation"
./beam.py add --text --name my-note "Paste a thought, a transcript, or a quote here."
./beam.py add ~/Documents/paper.pdf

./beam.py watch                # keep running: process anything new in inbox/
./beam.py watch --once         # process the queue once and exit
```

Open `index.html`, or open a page directly under `sorted/<category>/`.

Watch it work in the background:

```bash
./beam.py watch
# ... drop files into inbox/ from another window
```

## Commands

| Command | What it does |
| --- | --- |
| `./beam.py init` | Create the folder tree and `config.json` if missing. |
| `./beam.py add <url\|path\|text>` | Copy an input into `inbox/`. `--name` sets the filename stem; `--text` forces text mode; `-` reads stdin. |
| `./beam.py watch` | Poll `inbox/` and process stable new files. |
| `./beam.py watch --once` | Process everything currently waiting, then exit. |
| `./beam.py process <path>` | Process one file now (copies it into `inbox/` first unless it is already there). |
| `./beam.py rebuild` | Re-render every saved page from its JSON metadata. No model calls. Use after editing `theme.css`. |
| `./beam.py index` | Rebuild `index.html` from `sorted/` metadata. |
| `./beam.py status` | Show inbox, failed, and per-category counts. |
| `./beam.py doctor [--test]` | Check the Ollama endpoint, configured models, tools, and permissions. `--test` sends one tiny request per model. |

## Folder layout

```
inbox/                  drop zone; watched
processing/             in-flight claims
sorted/
  research/             one folder per category
    <slug>.html         the visual brief
    <slug>.json         every decision, token count, and the extracted content
    <slug>.<ext>        the original source (or .url for links)
    <slug>-cover.webp   downloaded cover/thumbnail, when available
    <slug>-reading-copy.txt  extracted source text (web, video, PDF, DOCX)
  code/
  finance/
  writing/
  admin/
  health/
  energy/
  content-creation/
  other/
failed/                 failed inputs + <name>.error.txt with a traceback
logs/beam.log           append-only run log
index.html              browsable library
theme.css               shared page and index styling
prompts/                editable model instructions
config.json             models, categories, limits
```

Every brief is a standalone HTML file with inlined CSS. It works offline, prints cleanly, respects `prefers-color-scheme`, and collapses to a single column on phones.

## What goes in

- **YouTube links** — metadata, chapters, and captions via `yt-dlp` (manual captions preferred, then auto-captions). Cover art is downloaded locally.
- **Web articles** — fetched and read with a dependency-free reader that keeps headings, lists, and prose. PDF links are handled as PDFs.
- **PDFs** — text via `pdftotext -layout`.
- **DOCX** — paragraph text extracted from the OOXML.
- **Text and notes** — `.txt`, `.md`, `.rst`, `.csv`, JSON, YAML, source-code files, and a bare URL inside a text file.
- **Bookmark files** — `.url`, `.webloc`, `.desktop`.
- **Images** are not supported yet; they will land in `failed/` with a clear error.

One resource per file. For a list of links, add them one at a time (or split the list into separate files).

## How the two routing calls work

1. **Classify** (`models.decision`, default `deepseek-v4.1-flash:1017-cloud`) reads a 6,000-character preview and returns `{category, title, slug, confidence, rationale}`. The category must be one of the keys in `config.json`.
2. **Route** (same decision model) reads a 4,000-character preview plus the classification and returns `{tier, reason, effort}`:
   - `cheap` → `models.cheap`, default `glm-5.3-flash:cloud`.
   - `premium` → `models.premium`, default `deepseek-v4.1-flash:1017-cloud`.
   - The prompt prefers premium for long transcripts, dense technical/financial/medical material, and quote-heavy jobs; it prefers cheap for short notes and routine admin.
   - If the model returns something invalid, a length/type heuristic takes over.
3. **Extract** (the chosen tier) returns structured JSON: headline, standfirst, summary, 5–10 verbatim quotes with context and timestamps, key figures, thematic sections, why it matters, actions, tags, and a reading-time estimate.
4. **Verify** every quote against the source with five-word shingles. Quotes that match are marked `✓ verified`; the match ratio is stored in the metadata. The page is rendered from a fixed template, not model-authored HTML, so layout and escaping are deterministic.

## Configuration

Edit `config.json`:

```json
{
  "models": {
    "decision": "deepseek-v4.1-flash:1017-cloud",
    "premium": "deepseek-v4.1-flash:1017-cloud",
    "cheap": "glm-5.3-flash:cloud",
    "think": false,
    "decision_num_predict": 500,
    "extract_num_predict": 8000,
    "temperature": 0.2
  },
  "categories": {
    "research": { "label": "Research", "accent": "#2563eb", "blurb": "..." }
  },
  "limits": {
    "max_source_chars": 160000,
    "max_quotes": 10
  },
  "watch": { "interval_seconds": 2.0, "settle_seconds": 1.2 }
}
```

- Add or remove categories freely; the classifier prompt is generated from this object. Keep the `other` key as a fallback.
- `accent` drives the category color across the page and index.
- `max_source_chars` controls how much of a long transcript is sent to the extraction model. Longer sources keep the beginning and sample evenly through the middle. `truncated: true` is recorded in metadata.
- `think: false` asks reasoning-capable models to skip their thinking pass. If a model ignores it, Beam still parses the JSON out of prose and retries with a larger budget when the response was cut off.
- Editable prompts live in `prompts/classify.md`, `prompts/route.md`, and `prompts/extract.md`.

### Using different models

Any model that the Ollama endpoint can serve works. Set `decision`, `premium`, and `cheap` to model names from `ollama list` or your Ollama Cloud account. `./beam.py doctor --test` checks all three.

If you change the endpoint, set `api.base_url` (default `http://127.0.0.1:11434`). The client uses the native `POST /api/chat` API with `format: "json"`, so no SDK or API key is needed when the local Ollama service is already authenticated to Ollama Cloud.

## Run as a user service

A unit template is included. Edit the two paths, then:

```bash
mkdir -p ~/.config/systemd/user
cp beam.service.example ~/.config/systemd/user/beam.service
$EDITOR ~/.config/systemd/user/beam.service

systemctl --user daemon-reload
systemctl --user enable --now beam.service
journalctl --user -u beam.service -f
```

`./beam.py watch` takes a lock file (`.beam.lock`), so a second watcher will refuse to start.

## Troubleshooting

- **"Ollama unreachable"** — check `curl http://127.0.0.1:11434/api/version` and that the Ollama app/service is running.
- **"model not listed"** — cloud models often respond without appearing in `/api/tags`. Use `./beam.py doctor --test` to confirm.
- **A file disappeared** — look in `failed/`; the matching `.error.txt` has the traceback. `logs/beam.log` has the run history.
- **Thin page for a website** — the reader is not a browser. JavaScript-rendered pages, paywalls, and login walls produce little text and a warning on the page. Add the text manually with `./beam.py add --text --name ...`.
- **YouTube has no transcript** — the page is built from the description and chapters only, with a visible warning. Some videos disable captions.
- **Model returned zero quotes** — quote verification and extraction are best-effort. Open the JSON sidecar to see what the model actually returned, then rerun after adjusting the prompt.
- **Bad layout after a theme edit** — run `./beam.py rebuild`; it re-renders every saved page without calling a model.

## Design notes and limits

- Beam uses only the Python standard library, plus `yt-dlp` and `pdftotext` when those input types appear.
- Source material is explicitly marked as untrusted in the extraction prompt, and the page is rendered from structured JSON rather than raw model HTML.
- Quote verification catches paraphrasing, but it cannot catch a quote that is verbatim yet misleading out of context. Read the context line on each quote.
- The HTML brief is a generated interpretation, not a copy of the source. The original is always kept next to it, along with the full decision record in the JSON sidecar.
