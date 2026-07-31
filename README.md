# SEM Diagram Triage

Extracts Structural Equation Model diagrams from academic PDFs, with a person deciding which figures
matter.

![A folder of PDFs, triaged by hand, into cropped figures with DOI, authors, journal and section text](docs/overview.png)

---

## Workflow

One PDF at a time. Confirming loads the next paper immediately; saving, the DOI lookup and text
extraction finish in the background.

![Workflow: mark, compare, save, with optional recrop](docs/workflow.png)

| Key | Action |
| --- | --- |
| **←** / **→** | Previous / next page |
| **Enter** | Advance (marking → compare → save) |
| **R** | Recrop — drag a box when the automatic crop is wrong |
| **D** | Disregard the PDF → `disregard/` |
| **Esc** | Cancel a recrop |

---

## Output

```
processed_data/
├── paper_m1.png     cropped figure
├── paper_m1.txt     DOI, authors, journal, page-1 text, section text
├── paper_m3.png     second figure from the same paper
├── paper_m3.txt
└── log.jsonl        one line per saved figure
```

The PDF then moves to `completed/`, or `disregard/` if skipped.

| Directory | Contents |
| --- | --- |
| `input_pdfs/` | Queue — drop PDFs here |
| `processed_data/` | Output PNG + TXT pairs, and `log.jsonl` |
| `completed/` | PDFs a figure was kept from |
| `disregard/` | Skipped PDFs |

---

## How the DOI is found

A title search can return a different paper, so sources are tried in order of trustworthiness and the
one used is recorded in the `DOI source:` line:

1. **DOI printed in the paper**, confirmed against Crossref.
2. **DOI printed in the paper, unconfirmed** — if Crossref is unreachable or does not know it.
3. **Crossref title search** — accepted only above 0.75 title similarity.
4. **OpenAlex title search** — same gate; covers papers Crossref's title index misses.
5. Otherwise `unknown`. Some papers have no DOI.

Lookups are cached per paper: five figures from one PDF cause one request.

---

## Notes

- `docling` only runs on pages that are clicked, in a background thread, so the UI does not block.
- Markers live in memory. Refreshing mid-paper restarts that PDF; confirmed papers are already on disk.
- Shortcuts do not fire while typing in a field.
- OCR is off: it took a page from ~2.5 s to ~21 s, and published PDFs carry a text layer. Scanned
  PDFs will not yield text.
- `.env` is gitignored.

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run sem_triage_app.py
```

Put PDFs in `input_pdfs/` and reload. Optionally add a local `.env` (gitignored) to use Crossref's
polite pool, which is faster and less rate-limited:

```
CROSSREF_MAILTO=your@email.address
```

---

## Worked example

Two real papers, showing the tool's actual output.

### One figure — DOI resolved by the OpenAlex fallback

`MISQ_1990_14_3_1.pdf`, figure on page 4:

<img src="docs/example-1990-fig1.png" width="380"
     alt="Extracted research model, Bergeron et al. 1990">

<sub>Bergeron, Rivard & de Serre (1990), *MIS Quarterly*,
<a href="https://doi.org/10.2307/248887">10.2307/248887</a></sub>

```text
Title: Investigating the Support Role of the Information Center
Authors: François Bergeron; Suzanne Rivard; Lyne de Serre
Journal: MIS Quarterly
DOI: 10.2307/248887
DOI source: openalex title match

--- Section containing the figure (page 4) ---
Location

According to several authors, an appropriate location for the IC is critical. …
```

Crossref did not match this 1990 paper confidently, so step 4 resolved it — otherwise the line would
read `unknown`. The section starts at its real heading, **Location**, not a page boundary.

### Two figures from one paper

`MISQ_1991_15_1_7.pdf`, figures on pages 3 and 7, both selected — each saved with its own section,
sharing one DOI lookup.

| `…_m1.png` — page 3 | `…_m2.png` — page 7 |
| --- | --- |
| <img src="docs/example-1991-fig1.png" width="320" alt="Conceptual model of PC utilization"> | <img src="docs/example-1991-fig2.png" width="260" alt="Research model of PC utilization"> |

<sub>Thompson, Higgins & Howell (1991), *MIS Quarterly*,
<a href="https://doi.org/10.2307/249443">10.2307/249443</a></sub>

The resulting `log.jsonl` lines:

```json
{"timestamp": "…", "pdf": "MISQ_1990_14_3_1.pdf", "action": "completed", "image": "MISQ_1990_14_3_1_m1.png", "page": 4, "chosen_marker_number": 1, "doi": "10.2307/248887", "doi_source": "openalex title match", "journal": "MIS Quarterly"}
{"timestamp": "…", "pdf": "MISQ_1991_15_1_7.pdf", "action": "completed", "image": "MISQ_1991_15_1_7_m1.png", "page": 3, "chosen_marker_number": 1, "doi": "10.2307/249443", "doi_source": "crossref title match", "journal": "MIS Quarterly"}
{"timestamp": "…", "pdf": "MISQ_1991_15_1_7.pdf", "action": "completed", "image": "MISQ_1991_15_1_7_m2.png", "page": 7, "chosen_marker_number": 2, "doi": "10.2307/249443", "doi_source": "crossref title match", "journal": "MIS Quarterly"}
```
