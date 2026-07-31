# SEM Diagram Triage

A small Human-In-The-Loop tool for pulling **Structural Equation Model diagrams** out of academic
PDFs. You flip through each paper in the browser, click the figures you care about, pick the ones you
want to keep, and the tool saves each figure as an image plus a text file with the paper's DOI,
authors, journal and the surrounding section text.

The slow part (`docling` layout analysis) only ever runs on the pages you actually click — never on
the whole corpus up front — and it runs in the background so you're never left waiting.

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run sem_triage_app.py
```

Then drop your PDFs into `input_pdfs/` and reload the page.

> **Optional but recommended.** Create a local `.env` file (it's gitignored) to join Crossref's
> "polite pool", which is noticeably faster and far less likely to rate-limit you:
> ```
> CROSSREF_MAILTO=your@email.address
> ```

---

## The workflow in one picture

```mermaid
flowchart LR
    A[input_pdfs/] --> B[1 · Mark<br/>click figures]
    B --> C[2 · Compare<br/>pick the keepers]
    C -->|confirm| D[processed_data/<br/>image + text]
    C -->|nothing worth keeping| E[disregard/]
    D --> F[completed/]
    B -.->|press D| E
```

You handle one PDF at a time. When you finish one, the next loads **immediately** — the saving,
DOI lookup and text extraction finish quietly in the background while you're already working on the
next paper.

---

## Step 1 — Mark the figures

One page at a time, centred. Move with the **←** / **→** arrow keys (or the buttons). Click anywhere
on a figure and a numbered blue circle appears — that's a candidate.

![Marking view](docs/01-marking.png)

Each click quietly kicks off a `docling` pass on **that one page** to find the figure's exact
boundaries. You don't wait for it: keep flipping pages and clicking. If a marker is still being
worked out it shows as a grey `…` and turns blue when it's ready.

| Key | Action |
| --- | --- |
| **←** / **→** | Previous / next page |
| **Enter** | Done marking → go to compare |
| **D** | Disregard this PDF entirely |

Nothing worth keeping in this paper? Just press **D** and it moves to `disregard/` — no analysis is
run at all.

---

## Step 2 — Compare and choose

All your candidates side by side, each cropped to the figure's real boundaries and captioned with the
figure caption `docling` found. Every thumbnail is the same size, so a wide path diagram doesn't
dwarf a small one.

![Comparison gallery](docs/02-compare.png)

**Click an image to select it** (blue border + ✓). You can select as many as you like — each one gets
saved separately with its own context. If you only marked one candidate, it's selected automatically,
so you can just hit **Enter**.

| Key | Action |
| --- | --- |
| **Enter** | Confirm the selection and save |
| **R** | Recrop the selected figure (see below) |
| **D** | Disregard this PDF |

---

## Step 3 — Recrop, when the automatic crop misses

Automatic figure detection is good but not perfect — multi-panel figures and unusual layouts can trip
it up. Press **R** (or the ✂ Recrop button under any candidate) and simply **drag a box** around what
you actually want:

![Recrop view](docs/03-recrop.png)

The crop is always cut from a fresh high-resolution render, so it looks identical whether you drew
the box at 75% or 200% zoom. A candidate whose automatic crop failed outright is labelled
*extraction failed — press R to redraw*, so nothing is silently lost.

---

## What you get

Confirming writes one image + one text file **per selected figure** into `processed_data/`:

```
processed_data/
├── smith2024_m1.png     ← the cropped figure
├── smith2024_m1.txt     ← its metadata + context
├── smith2024_m3.png     ← a second figure from the same paper
├── smith2024_m3.txt
└── log.jsonl            ← one line per paper handled
```

The `.txt` looks like this:

```text
Title: Trust in Automation: Integrating Empirical Evidence on Factors That Influence Trust
Authors: Kevin Anthony Hoff; Masooda Bashir
Journal: Human Factors: The Journal of the Human Factors and Ergonomics Society
DOI: 10.1177/0018720814547570
DOI source: printed-in-pdf (Crossref-confirmed)

--- First page text ---
(the whole of page 1 — title, authors, abstract …)

--- Section containing the figure (page 7) ---
(the complete section the figure sits in, from its heading to the next one,
 however many pages it spans)
```

The original PDF then moves to `completed/`, or to `disregard/` if you skipped it.

---

## How the DOI is found

Getting this wrong is easy — a title search can confidently return *a different paper*. So the tool
tries the most trustworthy source first and records which one it used in the `DOI source:` line:

1. **The DOI printed in the paper itself**, then confirmed against Crossref. Most reliable: it's the
   paper's own identifier.
2. **The printed DOI, unconfirmed** — used if Crossref is unreachable or doesn't know it.
3. **A Crossref title search** — accepted only if the returned title genuinely matches (≥ 0.75
   similarity), so a near-miss doesn't silently give you the wrong paper.
4. **An OpenAlex title search** — a second opinion for papers Crossref's title index misses, behind
   the same similarity gate.
5. Otherwise `unknown` — some papers genuinely have no DOI, and guessing would be worse.

Lookups are cached per paper, so selecting five figures from one PDF makes **one** request, not five.

---

## Directory layout

| Directory | What's in it |
| --- | --- |
| `input_pdfs/` | Your queue — drop PDFs here |
| `processed_data/` | Output: `<paper>_m<n>.png` + `.txt`, and `log.jsonl` |
| `completed/` | PDFs you kept at least one figure from |
| `disregard/` | PDFs you skipped |

---

## Good to know

- **Your work is per-session.** Markers live in memory, so don't refresh mid-paper — a refresh starts
  that PDF over. Confirmed papers are already safely written to disk.
- **Keyboard shortcuts are ignored while you're typing** in a text field, so they won't fire by
  accident.
- **OCR is off.** It made a single page take ~21s instead of ~2.5s, and published PDFs almost always
  carry a real text layer already. Scanned/image-only PDFs won't yield text — that's the trade-off.
- **`.env` is gitignored** so your email never lands in the repository.

---

## A worked example

Two real papers, start to finish. Both were dropped into `input_pdfs/`, marked, and confirmed —
these are the tool's actual outputs, not mock-ups.

### Example 1 — one figure, DOI found by the OpenAlex fallback

`MISQ_1990_14_3_1.pdf` — one figure marked on page 4 and confirmed.

![Extracted research model, Bergeron et al. 1990](docs/example-1990-fig1.png)

<sub>Figure extracted by the tool from Bergeron, Rivard & de Serre (1990), *MIS Quarterly*,
<a href="https://doi.org/10.2307/248887">10.2307/248887</a>. Shown here to illustrate the output.</sub>

`processed_data/MISQ_1990_14_3_1_m1.txt`:

```text
Title: Investigating the Support Role of the Information Center
Authors: François Bergeron; Suzanne Rivard; Lyne de Serre
Journal: MIS Quarterly
DOI: 10.2307/248887
DOI source: openalex title match

--- First page text ---
## Investigating the Support Role of  the
…

--- Section containing the figure (page 4) ---
Location

According to several authors, an appropriate location for the IC is critical. Providing users
with distributed support services was the first recommendation made by Rockart and Flannery
(1983) about support. …
```

Note the `DOI source` line. This paper is old enough that **Crossref's title search didn't confidently
match it**, so the lookup fell through to OpenAlex, which did — exactly the fallback described in
[How the DOI is found](#how-the-doi-is-found). Without it this line would read `unknown`.

Also note the captured section starts at the heading **Location** — not an arbitrary window of
pages — and runs to the next heading.

### Example 2 — two figures from one paper, saved separately

`MISQ_1991_15_1_7.pdf` — two figures marked (pages 3 and 7) and **both** selected in the comparison
step, so each was saved with its own context.

| `…_m1.png` — page 3 | `…_m2.png` — page 7 |
| --- | --- |
| ![Conceptual model of PC utilization](docs/example-1991-fig1.png) | ![Research model of PC utilization](docs/example-1991-fig2.png) |

<sub>Figures extracted by the tool from Thompson, Higgins & Howell (1991), *MIS Quarterly*,
<a href="https://doi.org/10.2307/249443">10.2307/249443</a>. Shown here to illustrate the output.</sub>

Both text files share the same paper-level metadata — resolved with a **single** Crossref lookup, not
one per figure — but each carries the section its own figure sits in:

```text
Title: Personal Computing: Toward a Conceptual Model of Utilization1
Authors: Ronald L. Thompson; Christopher A. Higgins; Jane M. Howell
Journal: MIS Quarterly
DOI: 10.2307/249443
DOI source: crossref title match
```

| File | Figure page | Section captured |
| --- | --- | --- |
| `MISQ_1991_15_1_7_m1.txt` | 3 | the section containing the conceptual model |
| `MISQ_1991_15_1_7_m2.txt` | 7 | the section containing the research model |

(The stray `1` after *Utilization* is a footnote marker sitting in the paper's own title — the tool
records what it actually read rather than tidying it up.)

### What the run left behind

```text
processed_data/
├── MISQ_1990_14_3_1_m1.png   MISQ_1990_14_3_1_m1.txt
├── MISQ_1991_15_1_7_m1.png   MISQ_1991_15_1_7_m1.txt
├── MISQ_1991_15_1_7_m2.png   MISQ_1991_15_1_7_m2.txt
└── log.jsonl

completed/
├── MISQ_1990_14_3_1.pdf
└── MISQ_1991_15_1_7.pdf
```

`log.jsonl` — one line per saved figure:

```json
{"pdf": "MISQ_1990_14_3_1.pdf", "action": "completed", "image": "MISQ_1990_14_3_1_m1.png", "page": 4, "chosen_marker_number": 1, "doi": "10.2307/248887", "doi_source": "openalex title match", "journal": "MIS Quarterly"}
{"pdf": "MISQ_1991_15_1_7.pdf", "action": "completed", "image": "MISQ_1991_15_1_7_m1.png", "page": 3, "chosen_marker_number": 1, "doi": "10.2307/249443", "doi_source": "crossref title match", "journal": "MIS Quarterly"}
{"pdf": "MISQ_1991_15_1_7.pdf", "action": "completed", "image": "MISQ_1991_15_1_7_m2.png", "page": 7, "chosen_marker_number": 2, "doi": "10.2307/249443", "doi_source": "crossref title match", "journal": "MIS Quarterly"}
```

Together these two papers took well under a minute of interaction — the DOI lookups, text extraction
and file writes all happened in the background while the next paper was already on screen.
