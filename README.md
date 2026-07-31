# SEM Diagram Triage

A minimal Human-In-The-Loop (HITL) Streamlit app for processing academic PDFs, manually classifying
Structural Equation Model (SEM) diagrams, and preparing payloads for the Anthropic Batch API.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run sem_triage_app.py
```

## Pipeline

The app has three phases, each triggered from the sidebar:

1. **Ingestion** ("Process New PDFs") — runs `docling` on every new PDF in `input_pdfs/`, saving a
   Markdown export and extracted figures to `processed_data/`. PDFs that yield zero images, or only
   logo/icon-sized images (both dimensions under 100px), are moved straight to `disregard/` without
   entering triage.
2. **Triage UI** (main view) — shows one extracted image at a time with the paragraph of text that
   precedes it in the source document. Mark each image `SEM` or `NO SEM`; the app auto-advances.
3. **Post-processing** ("Finish & Generate Batch") — for each fully-triaged PDF: if every image was
   `NO SEM`, the PDF moves to `disregard/`. If at least one image was `SEM`, the app looks up the
   paper's DOI and journal via the Crossref API (shown in the UI as it runs), base64-encodes each SEM
   image with its surrounding text/abstract/conclusion, appends one entry per image to
   `output/batch_requests.jsonl`, and moves the PDF to `completed/`.

All decisions and extracted context are persisted in `processed_data/tracker.json`, so the app can be
closed and reopened without re-running `docling` or losing triage progress.

## Directory layout

| Directory | Purpose |
|---|---|
| `input_pdfs/` | Drop raw PDFs here |
| `disregard/` | PDFs with no SEM diagrams (zero images, or all images marked NO SEM) |
| `processed_data/` | Docling Markdown exports, extracted images, and `tracker.json` |
| `completed/` | PDFs with at least one SEM diagram, fully processed |
| `output/batch_requests.jsonl` | Final output: one line per SEM image |

## Known limitation

The entries in `output/batch_requests.jsonl` use a **placeholder** Anthropic Batch API payload — the
model ID, `max_tokens`, and prompt text are stubs marked `TODO` in `_build_batch_payload()`. The real
prompt/model for the downstream SEM-extraction task is not yet decided.
