"""SEMify — click-to-mark SEM diagrams in academic PDFs, with scoped docling per pick."""

import base64
import concurrent.futures
import difflib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pypdfium2 as pdfium
import requests
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageDraw
from requests.adapters import HTTPAdapter
from streamlit_image_coordinates import streamlit_image_coordinates
from urllib3.util.retry import Retry

import sem_state

INPUT_DIR = Path("input_pdfs")
DISREGARD_DIR = Path("disregard")
PROCESSED_DIR = Path("processed_data")
COMPLETED_DIR = Path("completed")
LOG_PATH = PROCESSED_DIR / "log.jsonl"
# Resolved against this file, not the working directory, so the logo still
# loads when Streamlit is launched from elsewhere.
_ASSETS = Path(__file__).resolve().parent / "docs"
LOGO_PATH = _ASSETS / "logo.png"
LOGO_ICON_PATH = _ASSETS / "logo-icon.png"

ZOOM_LEVELS = {"75%": 0.75, "100%": 1.0, "150%": 1.5, "200%": 2.0}
MARKER_RADIUS_PX = 14
FALLBACK_MATCH_DISTANCE_PT = 40
FALLBACK_CROP_HALF_SIZE_PT = 100
VIEWER_HEIGHT_PX = 833  # ~3cm taller than the old 720px, to fit a whole page
# Uniform canvas every gallery candidate is letterboxed into, so all thumbnails
# render at identical size no matter the figure's aspect ratio. Rendered at
# natural size (never stretched to the column), so this is the size on screen.
GALLERY_THUMB_SIZE = (320, 240)
# Fixed column count: sizing the row to the number of candidates would give a
# lone candidate a full-page-width column.
GALLERY_COLUMNS = 4
# Rendered pages kept in memory per session (~4 MB each at 200%). Holds the
# current page plus its prefetched neighbours at a couple of zoom levels.
PAGE_CACHE_MAX_ENTRIES = 12
# The component defaults to 0 (no compression), which puts ~1.4 MB on the
# wire per page at 100% zoom and ~5.6 MB at 200%. Level 1 is 5-8x smaller
# and no slower to encode, which is what made paging feel laggy.
PAGE_PNG_COMPRESSION = 1
# Resolution the manual re-crop is cut from, independent of viewing zoom.
RECROP_OUTPUT_SCALE = 3.0
# Ignore drags smaller than this (in displayed pixels) as accidental clicks.
RECROP_MIN_DRAG_PX = 12
# Minimum title similarity before a search hit is accepted as this paper.
TITLE_MATCH_THRESHOLD = 0.75

# How each crop was obtained, and how it's labelled in the gallery.
STATUS_LABELS = {
    "ok": "",
    "fallback": " (nearest figure)",
    "no_match": " (rough crop — press R to redraw)",
    "manual": " (manual crop)",
    "error": " (extraction failed — press R to redraw)",
}

# All docling .convert() calls funnel through a single background worker
# thread (max_workers=1) so the model is never called concurrently from two
# threads, and so a click's processing (or a PDF's final commit) never blocks
# the main Streamlit thread — the user can keep working while it runs.
#
# The caches and the in-flight set live in sem_state (an imported module)
# rather than here: Streamlit re-executes THIS script on every rerun, which
# would reset anything defined at module level. See sem_state.py.
_docling_cache = sem_state.docling_cache
_docling_cache_lock = sem_state.docling_cache_lock
_doi_cache = sem_state.doi_cache
_doi_cache_lock = sem_state.doi_cache_lock
_in_flight_lock = sem_state.in_flight_lock


@st.cache_resource
def get_executor() -> concurrent.futures.ThreadPoolExecutor:
    return concurrent.futures.ThreadPoolExecutor(max_workers=1)


def ensure_dirs() -> None:
    for d in (INPUT_DIR, DISREGARD_DIR, PROCESSED_DIR, COMPLETED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


@st.cache_resource
def get_converter():
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    # generate_picture_images defaults to False; without it PictureItem.get_image()
    # returns None even though the picture regions are still detected.
    pipeline_options = PdfPipelineOptions()
    pipeline_options.generate_picture_images = True
    pipeline_options.images_scale = 2.0
    # OCR is the dominant cost of a single-page conversion (~21s -> ~2.5s
    # measured locally when disabled) and academic PDFs from real publishers
    # virtually always have an embedded text layer already.
    pipeline_options.do_ocr = False

    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )


def extract_title(document) -> str | None:
    texts = getattr(document, "texts", [])
    for item in texts:
        label = str(getattr(item, "label", "")).lower()
        if "title" in label:
            text = getattr(item, "text", "").strip()
            if text:
                return text
    # Docling's layout model sometimes labels a paper's title as
    # "section_header" instead of "title" (observed on real academic PDFs),
    # mixed in with page-1 masthead/journal-name headers. Take the longest
    # section_header among the first few page-1 items — titles are reliably
    # longer than journal names/mastheads.
    candidates = []
    for item in texts[:10]:
        label = str(getattr(item, "label", "")).lower()
        try:
            page_no = item.prov[0].page_no
        except (AttributeError, IndexError):
            page_no = None
        if "section_header" in label and page_no in (1, None):
            text = getattr(item, "text", "").strip()
            if text:
                candidates.append(text)
    if candidates:
        return max(candidates, key=len)
    return None


def _picture_page_no(picture) -> int | None:
    try:
        return picture.prov[0].page_no
    except (AttributeError, IndexError):
        return None


def _load_dotenv() -> None:
    """Read key=value pairs from a local, gitignored .env into the environment.
    Existing environment variables always win. Kept dependency-free on purpose."""
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except OSError as exc:
        print(f"[config] could not read .env: {exc}")


@st.cache_resource
def _crossref_session() -> requests.Session:
    """Session with automatic retry/backoff. Crossref returns 429 readily when
    several lookups fire at once (e.g. one per selected marker), which was the
    main cause of DOIs coming back 'unknown'."""
    _load_dotenv()
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    # Crossref's "polite pool" is faster and far less rate-limited. Set
    # CROSSREF_MAILTO to your email address to opt in.
    mailto = os.environ.get("CROSSREF_MAILTO", "").strip()
    ua = "SEMify/1.0 (https://github.com/janhh011/SEMify_Preprocessing)"
    if mailto:
        ua += f" mailto:{mailto}"
    session.headers.update({"User-Agent": ua})
    return session


def _normalize_doi(candidate: str) -> str | None:
    """Trim/validate a raw DOI-looking string. Returns None if implausible."""
    doi = candidate.strip().rstrip(").,;:>]}'\"").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:", "doi "):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix) :].strip()
    if not doi.lower().startswith("10."):
        return None
    slash = doi.find("/")
    if slash == -1:
        return None
    registrant = doi[3:slash]
    if not registrant.isdigit() or not 4 <= len(registrant) <= 9:
        return None
    if len(doi) - slash < 2:
        return None
    return doi


def _extract_doi_from_text(text: str) -> str | None:
    """Find a DOI printed in the paper itself. Plain string scanning (no regex),
    per the project's original 'no regex for DOIs' constraint."""
    # Also break on markdown/URL delimiters: docling exports DOIs as markdown
    # links, e.g. "[10.1016/x](https://doi.org/10.1016/x)", which would
    # otherwise be swallowed whole. '(' and ')' stay allowed because old-style
    # DOIs legitimately contain them (e.g. "10.1002/(SICI)..."); a trailing
    # one is removed by _normalize_doi.
    terminators = set(" \t\n\r\f\v[]<>\"'")
    search_from = 0
    while True:
        pos = text.find("10.", search_from)
        if pos == -1:
            return None
        search_from = pos + 3
        # Must start a token (avoid matching inside e.g. "110.25").
        if pos > 0 and (text[pos - 1].isdigit() or text[pos - 1] == "."):
            continue
        end = pos
        while end < len(text) and text[end] not in terminators:
            end += 1
        doi = _normalize_doi(text[pos:end])
        if doi:
            return doi


def _parse_crossref_item(item: dict) -> dict:
    container = item.get("container-title") or []
    names = []
    for a in item.get("author") or []:
        full = f"{a.get('given', '')} {a.get('family', '')}".strip()
        if full:
            names.append(full)
    titles = item.get("title") or []
    return {
        "doi": item.get("DOI"),
        "journal": container[0] if container else None,
        "authors": "; ".join(names) if names else None,
        "title": titles[0] if titles else None,
    }


def _crossref_by_doi(doi: str) -> dict | None:
    """Confirm a DOI exists and pull its authoritative metadata."""
    try:
        resp = _crossref_session().get(f"https://api.crossref.org/works/{doi}", timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _parse_crossref_item(resp.json().get("message", {}))
    except (requests.RequestException, ValueError) as exc:
        print(f"[crossref] DOI lookup failed for {doi!r}: {exc}")
        return None


def _crossref_by_title(title: str) -> dict | None:
    """Search by title, but only accept a result whose title actually matches —
    Crossref happily returns an unrelated paper for a near-miss query."""
    try:
        resp = _crossref_session().get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": title, "rows": 5},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
    except (requests.RequestException, ValueError) as exc:
        print(f"[crossref] title search failed for {title!r}: {exc}")
        return None

    best, best_score = None, 0.0
    for item in items:
        parsed = _parse_crossref_item(item)
        if not parsed["title"]:
            continue
        score = _title_similarity(title, parsed["title"])
        if score > best_score:
            best, best_score = parsed, score
    if best is None or best_score < TITLE_MATCH_THRESHOLD:
        print(f"[crossref] no confident title match for {title!r} (best={best_score:.2f})")
        return None
    return best


def _title_similarity(wanted: str, got: str) -> float:
    a = " ".join(wanted.lower().split())
    b = " ".join(got.lower().split())
    return difflib.SequenceMatcher(None, a, b).ratio()


def _openalex_by_title(title: str) -> dict | None:
    """Second opinion when Crossref finds nothing. OpenAlex indexes many works
    Crossref's title search misses, needs no API key, and returns the DOI.
    Same similarity gate applies — its top hit is often unrelated."""
    try:
        params = {"search": title, "per-page": 5}
        mailto = os.environ.get("CROSSREF_MAILTO", "").strip()
        if mailto:
            params["mailto"] = mailto
        resp = _crossref_session().get(
            "https://api.openalex.org/works", params=params, timeout=15
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except (requests.RequestException, ValueError) as exc:
        print(f"[openalex] search failed for {title!r}: {exc}")
        return None

    best, best_score = None, 0.0
    for work in results:
        got_title = work.get("title") or work.get("display_name")
        raw_doi = work.get("doi")
        if not got_title or not raw_doi:
            continue
        score = _title_similarity(title, got_title)
        if score > best_score:
            names = [
                (a.get("author") or {}).get("display_name")
                for a in (work.get("authorships") or [])
            ]
            names = [n for n in names if n]
            source = ((work.get("primary_location") or {}).get("source") or {})
            best = {
                "doi": _normalize_doi(raw_doi),
                "journal": source.get("display_name"),
                "authors": "; ".join(names) if names else None,
                "title": got_title,
            }
            best_score = score
    if best is None or not best["doi"] or best_score < TITLE_MATCH_THRESHOLD:
        print(f"[openalex] no confident match for {title!r} (best={best_score:.2f})")
        return None
    return best


def resolve_publication_metadata(pdf_path_str: str, doc_text: str, fallback_title: str) -> dict:
    """DOI resolution, most-reliable source first:
      1. the DOI printed in the paper itself, confirmed against Crossref
      2. an unconfirmed printed DOI (network down / not registered)
      3. a Crossref title search, accepted only on a confident title match
    Cached per PDF so N selected markers cause one lookup, not N."""
    with _doi_cache_lock:
        cached = _doi_cache.get(pdf_path_str)
    if cached is not None:
        return cached

    printed_doi = _extract_doi_from_text(doc_text)
    result = None
    if printed_doi:
        confirmed = _crossref_by_doi(printed_doi)
        if confirmed and confirmed.get("doi"):
            result = {**confirmed, "doi_source": "printed-in-pdf (Crossref-confirmed)"}
        else:
            # Trust the paper's own printed DOI even if Crossref can't confirm.
            result = {
                "doi": printed_doi,
                "journal": None,
                "authors": None,
                "title": fallback_title,
                "doi_source": "printed-in-pdf (unconfirmed)",
            }

    if result is None:
        matched = _crossref_by_title(fallback_title)
        if matched and matched.get("doi"):
            result = {**matched, "doi_source": "crossref title match"}

    if result is None:
        # Second opinion: OpenAlex indexes works Crossref's title search misses.
        matched = _openalex_by_title(fallback_title)
        if matched and matched.get("doi"):
            result = {**matched, "doi_source": "openalex title match"}

    if result is None:
        result = {
            "doi": None,
            "journal": None,
            "authors": None,
            "title": fallback_title,
            "doi_source": "not found",
        }
    result.setdefault("title", fallback_title)

    with _doi_cache_lock:
        _doi_cache[pdf_path_str] = result
    return result


# ---------------------------------------------------------------------------
# Page rendering / docling scoping helpers
# ---------------------------------------------------------------------------


def _pdf_meta(pdf_path_str: str) -> tuple[int, list[tuple[float, float]]]:
    """(page_count, per-page point sizes), cached process-wide.

    Only plain immutable data is cached — never the pdfium document itself,
    which isn't safe to share between the UI and worker threads. Without this,
    every rerun and every click re-opened and re-parsed the whole PDF.
    """
    with sem_state.pdf_meta_lock:
        cached = sem_state.pdf_meta_cache.get(pdf_path_str)
    if cached is not None:
        return cached
    with pdfium.PdfDocument(pdf_path_str) as pdf_doc:
        meta = (len(pdf_doc), [page.get_size() for page in pdf_doc])
    with sem_state.pdf_meta_lock:
        sem_state.pdf_meta_cache[pdf_path_str] = meta
    return meta


def render_pdf_page(pdf_path_str: str, page_no: int, scale: float):
    """Rasterise a single page. Safe to call from any thread (opens its own
    document); callers that want caching should use get_rendered_page."""
    with pdfium.PdfDocument(pdf_path_str) as pdf_doc:
        # to_pil() copies the bitmap, so the image outlives the document.
        return pdf_doc[page_no - 1].render(scale=scale).to_pil()


def get_rendered_page(pdf_path: Path, page_no: int, scale: float):
    key = (str(pdf_path), page_no, scale)
    cache = st.session_state.page_render_cache
    if key not in cache:
        cache[key] = render_pdf_page(str(pdf_path), page_no, scale)
        # Bounded LRU: a full page at 200% is ~4 MB, so caching every page of
        # a long paper at several zoom levels could hold hundreds of MB.
        while len(cache) > PAGE_CACHE_MAX_ENTRIES:
            cache.pop(next(iter(cache)))
    else:
        cache[key] = cache.pop(key)  # mark as most recently used
    return cache[key]


def prefetch_neighbour_pages(pdf_path: Path, page_no: int, n_pages: int, scale: float) -> None:
    """Warm the cache for the pages either side of this one.

    Called after the current page has been sent to the browser, so the raster
    cost lands between clicks instead of inside one. Main thread only — pdfium
    is not safe to drive from several threads at once.
    """
    for neighbour in (page_no + 1, page_no - 1):
        if 1 <= neighbour <= n_pages:
            key = (str(pdf_path), neighbour, scale)
            if key not in st.session_state.page_render_cache:
                try:
                    get_rendered_page(pdf_path, neighbour, scale)
                except Exception as exc:  # never let a prefetch break the page
                    print(f"[prefetch] page {neighbour}: {exc}")
                    return


def get_page_point_size(pdf_path: Path, page_no: int) -> tuple[float, float]:
    _n_pages, sizes = _pdf_meta(str(pdf_path))
    return sizes[page_no - 1]


def get_num_pages(pdf_path: Path) -> int:
    n_pages, _sizes = _pdf_meta(str(pdf_path))
    return n_pages


def _convert_cached(pdf_path_str: str, page_range: tuple[int, int] | None):
    """Thread-safe, process-wide cache for docling conversions. Pure function
    (no st.* / session_state access) so it's safe to call from the background
    worker thread as well as the main Streamlit thread."""
    key = (pdf_path_str, page_range)
    with _docling_cache_lock:
        cached = _docling_cache.get(key)
    if cached is not None:
        return cached
    converter = get_converter()
    if page_range is not None:
        result = converter.convert(pdf_path_str, page_range=page_range)
    else:
        result = converter.convert(pdf_path_str)
    document = result.document
    with _docling_cache_lock:
        _docling_cache[key] = document
    return document


def get_docling_single_page(pdf_path: Path, page_no: int):
    return get_executor().submit(_convert_cached, str(pdf_path), (page_no, page_no)).result()


def get_docling_full_document(pdf_path: Path):
    return get_executor().submit(_convert_cached, str(pdf_path), None).result()


def extract_section_text(document, page_no: int, x_pt: float, y_pt: float, page_height_pt: float) -> str:
    """Full text of the section (between the nearest preceding and following
    headings, in reading order) that contains the picture at the given point."""
    picture, _status = find_picture_at_point(document, page_no, x_pt, y_pt, page_height_pt)
    if picture is None:
        return ""

    items = [item for item, _level in document.iterate_items(traverse_pictures=True)]
    try:
        idx = items.index(picture)
    except ValueError:
        return ""

    def is_heading(item) -> bool:
        label = str(getattr(item, "label", "")).lower()
        return "section_header" in label or "title" in label

    start = idx
    while start > 0 and not is_heading(items[start - 1]):
        start -= 1
    if start > 0:
        start -= 1  # include the heading itself

    end = idx + 1
    while end < len(items) and not is_heading(items[end]):
        end += 1

    texts = [getattr(item, "text", None) for item in items[start:end]]
    return "\n\n".join(t for t in texts if t)


def draw_markers_on_page(raw_img, pdf_path: Path, page_no: int):
    markers_on_page = [m for m in st.session_state.markers if m["page_no"] == page_no]
    if not markers_on_page:
        return raw_img
    img = raw_img.copy()
    draw = ImageDraw.Draw(img)
    for marker in markers_on_page:
        px = marker["x_frac"] * img.width
        py = marker["y_frac"] * img.height
        r = MARKER_RADIUS_PX
        pending = marker["status"] == "pending"
        color = (150, 150, 150) if pending else (30, 100, 230)
        draw.ellipse([px - r, py - r, px + r, py + r], fill=color, outline="white", width=2)
        text = "…" if pending else str(marker["number"])
        draw.text((px, py), text, fill="white", anchor="mm")
    return img


def find_picture_at_point(document, page_no: int, x_pt: float, y_pt: float, page_height_pt: float):
    # picture.prov[0].bbox is in PDF-native BOTTOMLEFT origin; our click point
    # (from image pixel coords) is in TOPLEFT origin — convert before comparing.
    candidates = [p for p in document.pictures if _picture_page_no(p) == page_no]
    if not candidates:
        return None, "no_match"

    def top_left_bbox(picture):
        try:
            return picture.prov[0].bbox.to_top_left_origin(page_height_pt)
        except (AttributeError, IndexError):
            return None

    def contains(bbox) -> bool:
        return bbox.l <= x_pt <= bbox.r and bbox.t <= y_pt <= bbox.b

    for picture in candidates:
        bbox = top_left_bbox(picture)
        if bbox is not None and contains(bbox):
            return picture, "ok"

    best_picture, best_dist = None, None
    for picture in candidates:
        bbox = top_left_bbox(picture)
        if bbox is None:
            continue
        clamped_x = min(max(x_pt, bbox.l), bbox.r)
        clamped_y = min(max(y_pt, bbox.t), bbox.b)
        dist = ((clamped_x - x_pt) ** 2 + (clamped_y - y_pt) ** 2) ** 0.5
        if best_dist is None or dist < best_dist:
            best_picture, best_dist = picture, dist
    if best_picture is not None and best_dist <= FALLBACK_MATCH_DISTANCE_PT:
        return best_picture, "fallback"
    return None, "no_match"


def _rough_crop_around(pdf_path_str: str, page_no: int, x_pt: float, y_pt: float):
    """Fixed-size crop straight off a high-res raster, no docling involved."""
    raw = render_pdf_page(pdf_path_str, page_no, RECROP_OUTPUT_SCALE)
    cx, cy = x_pt * RECROP_OUTPUT_SCALE, y_pt * RECROP_OUTPUT_SCALE
    half = FALLBACK_CROP_HALF_SIZE_PT * RECROP_OUTPUT_SCALE
    return raw.crop(
        (
            max(0, int(cx - half)),
            max(0, int(cy - half)),
            min(raw.width, int(cx + half)),
            min(raw.height, int(cy + half)),
        )
    )


def _locate_and_crop_job(pdf_path_str: str, page_no: int, x_pt: float, y_pt: float, page_h_pt: float):
    """Runs in the background worker thread — no st.* / session_state access allowed here.

    Never raises: a failure here used to propagate through future.result() into
    the render on every rerun, which bricked the session with no way out. On
    failure the marker is still usable — the user can redraw it with Recrop.
    """
    try:
        document = _convert_cached(pdf_path_str, (page_no, page_no))
        picture, status = find_picture_at_point(document, page_no, x_pt, y_pt, page_h_pt)

        caption = None
        crop_image = None
        if picture is not None:
            crop_image = picture.get_image(document)
            try:
                caption = picture.caption_text(document) or None
            except Exception:
                caption = None

        if crop_image is None:
            # Found nothing usable — fall back to a rough box around the click.
            status = "no_match"
            crop_image = _rough_crop_around(pdf_path_str, page_no, x_pt, y_pt)

        return {"crop_image": crop_image, "status": status, "caption": caption}
    except Exception as exc:
        print(f"[locate] page {page_no} of {pdf_path_str}: {exc}")
        try:
            return {
                "crop_image": _rough_crop_around(pdf_path_str, page_no, x_pt, y_pt),
                "status": "no_match",
                "caption": None,
            }
        except Exception as exc2:
            print(f"[locate] rough crop also failed: {exc2}")
            return {"crop_image": None, "status": "error", "caption": None}


def submit_click(pdf_path: Path, page_no: int, px: int, py: int, scale: float) -> None:
    """Main-thread only: records a marker immediately (so the numbered circle
    appears right away) and hands the slow docling lookup to the background
    worker, so the user can keep clicking/navigating without waiting."""
    x_pt, y_pt = px / scale, py / scale
    page_w_pt, page_h_pt = get_page_point_size(pdf_path, page_no)

    future = get_executor().submit(
        _locate_and_crop_job, str(pdf_path), page_no, x_pt, y_pt, page_h_pt
    )
    marker = {
        "number": len(st.session_state.markers) + 1,
        "page_no": page_no,
        "x_frac": x_pt / page_w_pt,
        "y_frac": y_pt / page_h_pt,
        "crop_image": None,
        "status": "pending",
        "caption": None,
        "future": future,
    }
    st.session_state.markers.append(marker)


def reconcile_pending_markers(block: bool = False) -> None:
    """Main-thread only: merges results from any finished background jobs
    into their markers. If block=True, waits for all still-pending jobs."""
    for marker in st.session_state.markers:
        future = marker.get("future")
        if future is None:
            continue
        if block or future.done():
            # Defensive: _locate_and_crop_job already swallows its own errors,
            # but an unexpected raise here would otherwise re-fire on every
            # rerun and leave the session permanently stuck.
            try:
                result = future.result()
            except Exception as exc:
                print(f"[reconcile] marker #{marker['number']} failed: {exc}")
                result = {"crop_image": None, "status": "error", "caption": None}
            marker["crop_image"] = result["crop_image"]
            marker["status"] = result["status"]
            marker["caption"] = result["caption"]
            marker["future"] = None


# ---------------------------------------------------------------------------
# PDF queue / session reset
# ---------------------------------------------------------------------------


def init_session_state() -> None:
    st.session_state.setdefault("current_pdf", None)
    st.session_state.setdefault("last_finished_pdf", None)
    st.session_state.setdefault("zoom_scale", 1.0)
    st.session_state.setdefault("markers", [])
    st.session_state.setdefault("selected_marker_numbers", set())
    st.session_state.setdefault("last_gallery_click", {})
    st.session_state.setdefault("recrop_marker_number", None)
    st.session_state.setdefault("last_recrop_drag", None)
    st.session_state.setdefault("pending_shortcuts", [])
    st.session_state.setdefault("last_click_by_page", {})
    st.session_state.setdefault("page_render_cache", {})
    st.session_state.setdefault("stage", "marking")
    st.session_state.setdefault("current_marking_page", 1)


def reset_per_pdf_state() -> None:
    st.session_state.markers = []
    st.session_state.selected_marker_numbers = set()
    st.session_state.last_gallery_click = {}
    st.session_state.recrop_marker_number = None
    st.session_state.last_recrop_drag = None
    st.session_state.last_click_by_page = {}
    st.session_state.page_render_cache = {}
    st.session_state.stage = "marking"
    st.session_state.current_marking_page = 1


def get_or_advance_current_pdf() -> Path | None:
    current = st.session_state.current_pdf
    if current is not None and current.exists():
        return current
    with _in_flight_lock:
        in_flight = set(sem_state.in_flight_pdfs)
    # Never immediately re-offer the PDF this session just finished/disregarded
    # (belt-and-suspenders alongside the in-flight check above).
    just_finished = st.session_state.last_finished_pdf
    remaining = [
        p
        for p in sorted(INPUT_DIR.glob("*.pdf"))
        if str(p) not in in_flight and str(p) != just_finished
    ]
    if not remaining:
        # Nothing left except possibly the one we just finished (e.g. its
        # background finalize failed and dropped it back into the queue) —
        # allow it back in rather than getting permanently stuck.
        remaining = [p for p in sorted(INPUT_DIR.glob("*.pdf")) if str(p) not in in_flight]
    if not remaining:
        st.session_state.current_pdf = None
        return None
    st.session_state.current_pdf = remaining[0]
    reset_per_pdf_state()
    return remaining[0]


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def bind_shortcut(key_name: str, button_key: str) -> None:
    """Queue a keyboard shortcut for this rerun. Emitted by render_shortcuts()."""
    st.session_state.pending_shortcuts.append({"key": key_name, "button": button_key})


@st.cache_data(show_spinner=False)
def _favicon_data_uri() -> str | None:
    if not LOGO_ICON_PATH.exists():
        return None
    return "data:image/png;base64," + base64.standard_b64encode(
        LOGO_ICON_PATH.read_bytes()
    ).decode("utf-8")


def apply_favicon() -> None:
    """Swap the tab icon by editing the <link> tags directly.

    st.set_page_config(page_icon=...) updates the favicon from JavaScript
    after load, which Safari ignores — it keeps showing the Streamlit icon
    baked into the served index.html. Removing the existing links and adding
    a fresh one (as a data URI, so no caching or media endpoint is involved)
    gets Safari to pick it up. Runs once per browser session.
    """
    uri = _favicon_data_uri()
    if not uri:
        return
    components.html(
        f"""
        <script>
        (function () {{
            const win = window.parent, doc = win.document;
            if (win.__semifyFavicon) return;
            doc.querySelectorAll(
                "link[rel~='icon'], link[rel='shortcut icon'], link[rel='apple-touch-icon']"
            ).forEach(function (el) {{ el.remove(); }});
            const link = doc.createElement('link');
            link.rel = 'icon';
            link.type = 'image/png';
            link.href = {json.dumps(uri)};
            doc.head.appendChild(link);
            win.__semifyFavicon = true;
        }})();
        </script>
        """,
        height=0,
        width=0,
    )


def enable_drag_preview() -> None:
    """Draw the selection rectangle while dragging in the recrop view.

    The click-capture component only reports the box on mouse-up and shows
    nothing during the drag, so there is no way to see what is being selected.
    Its iframe is same-origin, so an overlay can be attached to the <img>
    inside it. Purely visual — the crop still comes from the coordinates the
    component reports.
    """
    components.html(
        """
        <script>
        (function () {
            const doc = window.parent.document;

            function attach() {
                let found = false;
                doc.querySelectorAll('iframe').forEach(function (frame) {
                let d;
                try { d = frame.contentDocument; } catch (err) { return; }
                if (!d) return;
                const img = d.getElementById('image');
                if (!img) return;
                found = true;
                if (img.dataset.semifyDrag) return;
                img.dataset.semifyDrag = '1';

                const box = d.createElement('div');
                box.style.cssText = 'position:fixed; display:none; z-index:9999;' +
                    'border:2px solid #1E64E6; background:rgba(30,100,230,0.14);' +
                    'pointer-events:none; border-radius:2px;';
                d.body.appendChild(box);

                let sx = 0, sy = 0, active = false;
                const draw = function (e) {
                    box.style.left = Math.min(sx, e.clientX) + 'px';
                    box.style.top = Math.min(sy, e.clientY) + 'px';
                    box.style.width = Math.abs(e.clientX - sx) + 'px';
                    box.style.height = Math.abs(e.clientY - sy) + 'px';
                };
                const stop = function () { active = false; box.style.display = 'none'; };

                img.addEventListener('mousedown', function (e) {
                    active = true; sx = e.clientX; sy = e.clientY;
                    box.style.display = 'block'; draw(e);
                });
                d.addEventListener('mousemove', function (e) { if (active) draw(e); });
                // The drag often ends outside the iframe, so listen in both.
                d.addEventListener('mouseup', stop);
                doc.addEventListener('mouseup', stop);
                });
                return found;
            }

            // The component's iframe loads asynchronously, so its <img> is
            // usually not there yet on the first pass. Keep looking briefly
            // instead of giving up.
            if (attach()) return;
            let tries = 0;
            const timer = setInterval(function () {
                if (attach() || ++tries > 50) clearInterval(timer);
            }, 200);
        })();
        </script>
        """,
        height=0,
        width=0,
    )


def render_shortcuts() -> None:
    """Install this page's keyboard shortcuts. Call once, after the buttons render.

    Hand-rolled rather than using streamlit-shortcuts because that library
    (a) does `el.click(); el.focus()` — and the focus() scrolls the page down to
    the button and leaves it with a focus ring, and (b) never un-registers a
    binding when its button leaves the DOM, while its listener bails at the
    first shortcut match whose element is gone, so one page's binding could
    permanently shadow another's.

    All bindings go out in a single script so they coexist, and listeners are
    also attached inside same-origin child iframes — the PDF click-capture
    component is one, and a keypress while focus sits in an iframe never
    reaches the parent document.
    """
    bindings = st.session_state.pending_shortcuts
    if not bindings:
        return
    components.html(
        f"""
        <script>
        (function () {{
            const doc = window.parent.document;
            const win = window.parent;
            const bindings = {json.dumps(bindings)};

            if (win.__semShortcutCleanup) {{ win.__semShortcutCleanup(); }}

            // Tear down any leftover streamlit-shortcuts listener still live in
            // an already-open tab; its handler calls focus() and would scroll.
            if (win.__streamlitShortcutsListener) {{
                doc.removeEventListener('keydown', win.__streamlitShortcutsListener);
                win.__streamlitShortcutsListener = null;
                win.__streamlitShortcutsMap = {{}};
                win.__streamlitShortcutsInitialized = false;
            }}

            const handler = function (e) {{
                if (e.ctrlKey || e.altKey || e.metaKey) return;
                // Don't hijack keys while the user is typing.
                const t = e.target;
                if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) {{
                    return;
                }}
                const pressed = (e.key || '').toLowerCase();
                const match = bindings.find(function (b) {{
                    return b.key.toLowerCase() === pressed;
                }});
                if (!match) return;
                const el = doc.querySelector('.st-key-' + match.button + ' button');
                if (!el || el.disabled) return;
                e.preventDefault();
                e.stopPropagation();
                // click() only — never focus(), which scrolls the button into
                // view and leaves it looking preselected.
                el.click();
            }};

            const targets = [doc];
            doc.querySelectorAll('iframe').forEach(function (frame) {{
                try {{
                    if (frame.contentDocument) targets.push(frame.contentDocument);
                }} catch (err) {{ /* cross-origin frame: not ours, skip */ }}
            }});
            // Capture phase so this runs before any other listener.
            targets.forEach(function (t) {{ t.addEventListener('keydown', handler, true); }});

            win.__semShortcutCleanup = function () {{
                targets.forEach(function (t) {{
                    try {{ t.removeEventListener('keydown', handler, true); }} catch (err) {{}}
                }});
                win.__semShortcutCleanup = null;
            }};
        }})();
        </script>
        """,
        height=0,
        width=0,
    )


def _compose_gallery_thumbnail(crop_image, selected: bool):
    """Letterbox a crop onto a fixed-size canvas and draw its selection state.

    The border is painted into the image itself rather than via CSS because the
    thumbnail is rendered through the click-capture component, which controls
    its own markup — so surrounding HTML can't style it.
    """
    canvas_w, canvas_h = GALLERY_THUMB_SIZE
    canvas = Image.new("RGB", (canvas_w, canvas_h), (247, 247, 249))
    draw = ImageDraw.Draw(canvas)

    if crop_image is None:
        draw.text(
            (canvas_w // 2, canvas_h // 2),
            "no image\npress R to draw it",
            fill=(140, 140, 148),
            anchor="mm",
            align="center",
        )
    else:
        thumb = crop_image.convert("RGB")
        thumb.thumbnail((canvas_w - 18, canvas_h - 18), Image.LANCZOS)
        canvas.paste(thumb, ((canvas_w - thumb.width) // 2, (canvas_h - thumb.height) // 2))
    if selected:
        for inset in range(5):
            draw.rectangle(
                [inset, inset, canvas_w - 1 - inset, canvas_h - 1 - inset], outline=(30, 100, 230)
            )
    else:
        draw.rectangle([0, 0, canvas_w - 1, canvas_h - 1], outline=(205, 205, 210))
    return canvas


_SKELETON_HTML = f"""
<div style="height:{VIEWER_HEIGHT_PX - 40}px; border-radius:8px;
            background: linear-gradient(90deg, rgba(128,128,128,0.12) 25%,
                        rgba(128,128,128,0.22) 37%, rgba(128,128,128,0.12) 63%);
            background-size: 400% 100%; animation: sem-skeleton-pulse 1.4s ease infinite;">
</div>
<style>
@keyframes sem-skeleton-pulse {{
    0% {{ background-position: 100% 50%; }}
    100% {{ background-position: 0% 50%; }}
}}
</style>
"""


def render_marking_view(pdf_path: Path) -> None:
    reconcile_pending_markers()

    scale = st.session_state.zoom_scale
    n_pages = get_num_pages(pdf_path)
    page_no = max(1, min(st.session_state.current_marking_page, n_pages))
    st.session_state.current_marking_page = page_no

    value = None
    # Fixed-height, bordered container: reserves the same vertical space
    # regardless of page aspect ratio/zoom, so the nav/compare buttons below
    # never shift as pages change or the image loads.
    with st.container(height=VIEWER_HEIGHT_PX, border=True, key="pdf_viewer"):
        left, center, right = st.columns([1, 2, 1])
        with center:
            st.caption(f"Page {page_no} of {n_pages}")
            placeholder = st.empty()
            with placeholder:
                st.markdown(_SKELETON_HTML, unsafe_allow_html=True)

            raw_img = get_rendered_page(pdf_path, page_no, scale)
            marked_img = draw_markers_on_page(raw_img, pdf_path, page_no)

            with placeholder:
                value = streamlit_image_coordinates(
                    marked_img, key=f"page_{page_no}",
                    png_compression_level=PAGE_PNG_COMPRESSION,
                )

    if value is not None:
        # NB: the component's timestamp field is "unix_time", not "time" —
        # without it, two clicks on the exact same pixel look identical and
        # the second one gets swallowed as a duplicate.
        click_id = (value.get("x"), value.get("y"), value.get("unix_time"))
        if st.session_state.last_click_by_page.get(page_no) != click_id:
            st.session_state.last_click_by_page[page_no] = click_id
            submit_click(pdf_path, page_no, value["x"], value["y"], scale)
            st.rerun()

    pending_count = sum(1 for m in st.session_state.markers if m["status"] == "pending")
    if pending_count:
        st.caption(f"Analyzing {pending_count} marked figure(s) in the background…")

    nav_left, nav_right = st.columns(2)
    with nav_left:
        if st.button(
            "◀ Previous Page  `←`",
            disabled=(page_no == 1),
            use_container_width=True,
            key="prev_page_button",
        ):
            st.session_state.current_marking_page = page_no - 1
            st.rerun()
        bind_shortcut("ArrowLeft", "prev_page_button")
    with nav_right:
        if st.button(
            "Next Page ▶  `→`",
            disabled=(page_no == n_pages),
            use_container_width=True,
            key="next_page_button",
        ):
            st.session_state.current_marking_page = page_no + 1
            st.rerun()
        bind_shortcut("ArrowRight", "next_page_button")

    st.divider()
    if st.session_state.markers:
        if st.button(
            "Compare marked images →  `Enter`", type="primary", key="compare_images_button"
        ):
            st.session_state.stage = "comparing"
            st.rerun()
        bind_shortcut("Enter", "compare_images_button")
    else:
        st.caption("Click on a figure above to mark it as a candidate SEM diagram, "
                   "or press D to disregard this PDF.")

    # Last, so this page is already on screen: raster the adjacent pages so
    # the next Previous/Next click reads them straight from cache.
    prefetch_neighbour_pages(pdf_path, page_no, n_pages, scale)


def render_comparison_gallery(pdf_path: Path) -> None:
    markers = st.session_state.markers

    if any(m["status"] == "pending" for m in markers):
        with st.spinner("Finishing analysis of marked figures…"):
            reconcile_pending_markers(block=True)

    if st.button("← Back to marking"):
        st.session_state.stage = "marking"
        st.rerun()

    if not markers:
        st.info("No candidates marked. Use 'Disregard PDF' in the sidebar, or go back to mark some.")
        return

    # If there's only one candidate at all, auto-select it so Enter confirms
    # immediately without an extra click.
    if len(markers) == 1 and not st.session_state.selected_marker_numbers:
        st.session_state.selected_marker_numbers = {markers[0]["number"]}

    selected_numbers = st.session_state.selected_marker_numbers

    st.caption("Click an image to select or deselect it, then press Enter to confirm.")

    for row_start in range(0, len(markers), GALLERY_COLUMNS):
        row = markers[row_start : row_start + GALLERY_COLUMNS]
        # Always GALLERY_COLUMNS wide, so a single candidate keeps a normal
        # thumbnail-sized column instead of spanning the whole page.
        cols = st.columns(GALLERY_COLUMNS)
        for col, marker in zip(cols, row):
            with col:
                number = marker["number"]
                is_selected = number in selected_numbers
                thumbnail = _compose_gallery_thumbnail(marker["crop_image"], is_selected)

                # "auto" = natural size; "always" would stretch it to the full
                # column width and blow the thumbnail up.
                value = streamlit_image_coordinates(
                    thumbnail, key=f"gallery_{number}", use_column_width="auto"
                )
                if value is not None:
                    click_id = (value.get("x"), value.get("y"), value.get("unix_time"))
                    if st.session_state.last_gallery_click.get(number) != click_id:
                        st.session_state.last_gallery_click[number] = click_id
                        if is_selected:
                            st.session_state.selected_marker_numbers.discard(number)
                        else:
                            st.session_state.selected_marker_numbers.add(number)
                        st.rerun()

                label = f"{'✓ ' if is_selected else ''}#{number} — page {marker['page_no']}"
                label += STATUS_LABELS.get(marker["status"], "")
                st.caption(label)
                st.write(marker["caption"] or "_(no caption found)_")

                # Recrop is offered for every candidate, so a bad automatic
                # crop can always be redrawn by hand.
                if st.button("✂ Recrop", key=f"recrop_{number}", use_container_width=True):
                    st.session_state.recrop_marker_number = number
                    st.session_state.last_recrop_drag = None
                    st.session_state.stage = "recropping"
                    st.rerun()

    st.divider()
    selected_markers = [m for m in markers if m["number"] in selected_numbers]
    if selected_markers:
        # R recrops the selected candidate (the first, if several are selected).
        bind_shortcut("r", f"recrop_{selected_markers[0]['number']}")

        unusable = [m for m in selected_markers if m["crop_image"] is None]
        if unusable:
            nums = ", ".join(f"#{m['number']}" for m in unusable)
            st.warning(f"{nums} has no usable image yet — press R to draw the crop by hand.")

        count = len(selected_markers)
        label = f"Confirm {count} image{'s' if count != 1 else ''} & Save  `Enter`"
        if st.button(label, type="primary", key="confirm_selection_button", disabled=bool(unusable)):
            commit_final_choices(pdf_path, selected_markers)
        if not unusable:
            bind_shortcut("Enter", "confirm_selection_button")
    else:
        st.caption("Nothing selected yet — click the image(s) you want to keep.")


def render_recrop_view(pdf_path: Path) -> None:
    """Draw a box by dragging on the page to replace an automatic crop."""
    number = st.session_state.recrop_marker_number
    marker = next((m for m in st.session_state.markers if m["number"] == number), None)
    if marker is None:
        st.session_state.stage = "comparing"
        st.rerun()

    page_no = marker["page_no"]
    scale = st.session_state.zoom_scale

    st.subheader(f"Recrop #{number} — page {page_no}")
    st.caption(
        "Drag a box around the figure you want. Use the sidebar zoom if the page "
        "is too small. Press Esc or use Cancel to keep the current crop."
    )

    if st.button("← Cancel", key="cancel_recrop_button"):
        st.session_state.stage = "comparing"
        st.rerun()
    bind_shortcut("Escape", "cancel_recrop_button")

    page_img = get_rendered_page(pdf_path, page_no, scale)

    with st.container(height=VIEWER_HEIGHT_PX, border=True, key="pdf_viewer"):
        left, center, right = st.columns([1, 2, 1])
        with center:
            value = streamlit_image_coordinates(
                page_img,
                key=f"recrop_canvas_{number}_{page_no}_{scale}",
                click_and_drag=True,
                use_column_width="auto",
                png_compression_level=PAGE_PNG_COMPRESSION,
            )

    # After the component exists, so its iframe can be found.
    enable_drag_preview()

    if not value or value.get("x1") is None:
        return

    drag_id = (value.get("x1"), value.get("y1"), value.get("x2"), value.get("y2"),
               value.get("unix_time"))
    if st.session_state.last_recrop_drag == drag_id:
        return
    st.session_state.last_recrop_drag = drag_id

    # Component coordinates are relative to the *displayed* image; rescale to
    # the rendered page in case the browser sized it differently.
    shown_w = value.get("width") or page_img.width
    shown_h = value.get("height") or page_img.height
    fx = page_img.width / shown_w if shown_w else 1.0
    fy = page_img.height / shown_h if shown_h else 1.0

    x1, x2 = sorted((value["x1"] * fx, value["x2"] * fx))
    y1, y2 = sorted((value["y1"] * fy, value["y2"] * fy))
    if (x2 - x1) < RECROP_MIN_DRAG_PX or (y2 - y1) < RECROP_MIN_DRAG_PX:
        st.warning("That drag was too small — draw a box around the figure.")
        return

    # Rendered-page pixels -> PDF points -> a fresh high-resolution crop, so
    # the saved image doesn't inherit the viewing zoom's resolution.
    out = RECROP_OUTPUT_SCALE / scale
    hires = render_pdf_page(str(pdf_path), page_no, RECROP_OUTPUT_SCALE)
    box = (
        max(0, int(x1 * out)),
        max(0, int(y1 * out)),
        min(hires.width, int(x2 * out)),
        min(hires.height, int(y2 * out)),
    )
    marker["crop_image"] = hires.crop(box)
    marker["status"] = "manual"
    st.session_state.selected_marker_numbers.add(number)
    st.session_state.stage = "comparing"
    st.rerun()


# ---------------------------------------------------------------------------
# Commit actions
# ---------------------------------------------------------------------------


def _finalize_marker_job(
    pdf_path_str: str, stem: str, page: int, x_frac: float, y_frac: float, marker_number: int, crop_image
) -> None:
    """Runs in the background worker thread — no st.* / session_state access allowed here.
    Saves ONE marker's image + text + log entry. Does NOT move the PDF (that
    happens once, after every selected marker's job, via _finalize_move_job).
    Fire-and-forget: exceptions are caught and logged rather than raised, so
    one bad marker can't crash the worker or block the others."""
    pdf_path = Path(pdf_path_str)
    try:
        suffix = f"_m{marker_number}"
        image_path = PROCESSED_DIR / f"{stem}{suffix}.png"
        crop_image.save(image_path)

        full_doc = _convert_cached(pdf_path_str, None)
        page1_text = full_doc.export_to_markdown(page_no=1)

        page_w_pt, page_h_pt = get_page_point_size(pdf_path, page)
        x_pt, y_pt = x_frac * page_w_pt, y_frac * page_h_pt
        section_text = extract_section_text(full_doc, page, x_pt, y_pt, page_h_pt)

        extracted_title = extract_title(full_doc) or stem
        # Scan page 1 first (where the DOI is normally printed), then the whole
        # document, before falling back to a Crossref title search.
        meta = resolve_publication_metadata(
            pdf_path_str,
            page1_text + "\n" + full_doc.export_to_markdown(),
            extracted_title,
        )
        doi = meta["doi"]
        journal = meta["journal"]
        authors = meta["authors"]
        title = meta.get("title") or extracted_title

        text_path = PROCESSED_DIR / f"{stem}{suffix}.txt"
        text_path.write_text(
            f"Title: {title}\n"
            f"Authors: {authors or 'unknown'}\n"
            f"Journal: {journal or 'unknown'}\n"
            f"DOI: {doi or 'unknown'}\n"
            f"DOI source: {meta['doi_source']}\n"
            "\n"
            "--- First page text ---\n"
            f"{page1_text}\n"
            "\n"
            f"--- Section containing the figure (page {page}) ---\n"
            f"{section_text}\n",
            encoding="utf-8",
        )

        _append_jsonl(
            LOG_PATH,
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "pdf": pdf_path.name,
                "action": "completed",
                "image": image_path.name,
                "page": page,
                "chosen_marker_number": marker_number,
                "doi": doi,
                "doi_source": meta["doi_source"],
                "journal": journal,
            },
        )
    except Exception as exc:
        print(f"[finalize] marker #{marker_number} failed for {pdf_path.name}: {exc}")


def _finalize_move_job(pdf_path_str: str) -> None:
    """Runs in the background worker thread, submitted after every selected
    marker's job so it executes last (single-worker executor = FIFO order)."""
    pdf_path = Path(pdf_path_str)
    try:
        if pdf_path.exists():
            shutil.move(pdf_path_str, str(COMPLETED_DIR / pdf_path.name))
    except Exception as exc:
        print(f"[finalize] move failed for {pdf_path.name}: {exc}")
    finally:
        with _in_flight_lock:
            sem_state.in_flight_pdfs.discard(pdf_path_str)


def commit_final_choices(pdf_path: Path, markers: list[dict]) -> None:
    """Main-thread only: hands the whole slow finalize (docling + Crossref +
    file writes, once per selected marker, then a single move) to the
    background worker and advances to the next PDF immediately, so the user
    isn't blocked waiting for it."""
    markers = [m for m in markers if m["crop_image"] is not None]
    with _in_flight_lock:
        sem_state.in_flight_pdfs.add(str(pdf_path))
    executor = get_executor()
    for marker in markers:
        executor.submit(
            _finalize_marker_job,
            str(pdf_path),
            pdf_path.stem,
            marker["page_no"],
            marker["x_frac"],
            marker["y_frac"],
            marker["number"],
            marker["crop_image"],
        )
    executor.submit(_finalize_move_job, str(pdf_path))
    st.session_state.last_finished_pdf = str(pdf_path)
    st.session_state.current_pdf = None
    st.rerun()


def commit_disregard(pdf_path: Path) -> None:
    _append_jsonl(
        LOG_PATH,
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pdf": pdf_path.name,
            "action": "disregarded",
        },
    )
    shutil.move(str(pdf_path), str(DISREGARD_DIR / pdf_path.name))
    st.session_state.last_finished_pdf = str(pdf_path)
    st.session_state.current_pdf = None
    st.rerun()


_CHECK_SVG = """
  <svg width="52" height="52" viewBox="0 0 24 24" fill="none"
       stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="10" stroke="#C9DCFA" stroke-width="1.6"/>
    <path d="M7.7 12.4l2.9 2.9 5.7-5.9" stroke="#1E64E6" stroke-width="1.9"/>
  </svg>
"""

_SPINNER_SVG = """
  <svg width="52" height="52" viewBox="0 0 24 24" fill="none"
       style="animation: semify-spin 1.1s linear infinite;">
    <circle cx="12" cy="12" r="10" stroke="#E2E5EC" stroke-width="1.8"/>
    <path d="M12 2a10 10 0 0 1 10 10" stroke="#1E64E6" stroke-width="1.8"
          stroke-linecap="round"/>
  </svg>
  <style>@keyframes semify-spin { to { transform: rotate(360deg); } }</style>
"""


def _html(markup: str) -> str:
    """Flatten indented markup to a single line.

    st.markdown treats any line indented by four or more spaces as a code
    block, so HTML written at the indentation of the surrounding function is
    displayed literally instead of rendered.
    """
    return " ".join(line.strip() for line in markup.splitlines() if line.strip())


def _centred_notice(svg: str, title: str, subtitle: str) -> None:
    """One centred mark with a line of text under it — used for every state
    where there is no page to show."""
    st.markdown(
        _html(
            f"""
            <div style="display:flex; flex-direction:column; align-items:center;
                        justify-content:center; height:calc(100vh - 220px); gap:0.85rem;">
              {svg}
              <div style="color:#3C3C46; font-size:0.98rem; font-weight:500;">{title}</div>
              <div style="color:#8A8A96; font-size:0.82rem;">{subtitle}</div>
            </div>
            """
        ),
        unsafe_allow_html=True,
    )


def render_empty_state() -> None:
    _centred_notice(
        _CHECK_SVG,
        "No PDFs remaining",
        'Add files to <code style="font-size:0.82rem;">input_pdfs/</code> and refresh',
    )


@st.fragment(run_every=1.5)
def _wait_for_background() -> None:
    """Shown when every remaining PDF is still finalizing. Polls itself so the
    app moves on by itself instead of stranding the user on a dead screen."""
    with _in_flight_lock:
        remaining = len(sem_state.in_flight_pdfs)
    if remaining:
        _centred_notice(
            _SPINNER_SVG,
            f"Finishing up {remaining} PDF{'s' if remaining != 1 else ''}",
            "Saving the figures and looking up the DOI",
        )
    else:
        st.rerun(scope="app")


def main() -> None:
    st.set_page_config(page_title="SEMify", page_icon=str(LOGO_ICON_PATH), layout="wide")
    # Presentation only — spacing, weights and muted tones. Colours come from
    # .streamlit/config.toml. Streamlit reserves a large top margin by
    # default; trimming it keeps the viewer high on the page.
    st.markdown(
        _html(
            """
        <style>
        .block-container { padding-top: 0.6rem; padding-bottom: 0.8rem; max-width: 1500px; }

        /* Muted, slightly smaller secondary text (page counts, statuses). */
        [data-testid="stCaptionContainer"] p { color: #6C6C79; font-size: 0.8rem;
             margin-bottom: 0.15rem; }

        /* Section labels in the sidebar read as labels, not headings. */
        [data-testid="stSidebar"] h2 { font-size: 0.78rem; font-weight: 600;
             text-transform: uppercase; letter-spacing: 0.09em; color: #6C6C79; }
        [data-testid="stSidebar"] .block-container { padding-top: 1.6rem; }

        .stButton button { font-weight: 500; }

        hr { margin: 0.5rem 0; border-color: #E8E9EE; }

        /* Size the viewer to the window so the buttons under it stay on
           screen without scrolling. The px height passed to st.container is
           the fallback if this rule ever stops matching. */
        .st-key-pdf_viewer { height: calc(100vh - 155px) !important; }
        .st-key-pdf_viewer > div { height: 100% !important; }
        </style>
            """
        ),
        unsafe_allow_html=True,
    )
    ensure_dirs()
    init_session_state()
    st.session_state.pending_shortcuts = []

    apply_favicon()
    if LOGO_PATH.exists():
        st.logo(str(LOGO_PATH), size="large",
                icon_image=str(LOGO_ICON_PATH) if LOGO_ICON_PATH.exists() else None)

    pdf_path = get_or_advance_current_pdf()

    with _in_flight_lock:
        in_flight_count = len(sem_state.in_flight_pdfs)

    # The sidebar is always rendered — an empty one collapses, which hides the
    # logo that lives in it and leaves the page looking broken.
    remaining_count = len(list(INPUT_DIR.glob("*.pdf"))) - in_flight_count
    with st.sidebar:
        st.header("Pipeline Controls")
        if pdf_path is None:
            # Background progress is shown in the centre of the page instead,
            # so the sidebar does not repeat it.
            st.caption("Queue empty")
        else:
            st.caption(f"{pdf_path.name}")
            st.caption(f"{remaining_count} PDF(s) remaining in queue")
            if in_flight_count:
                st.caption(f"{in_flight_count} PDF(s) finishing up in the background")

        if pdf_path is not None:
            zoom_label = st.selectbox("Zoom", list(ZOOM_LEVELS.keys()), index=1)
            st.session_state.zoom_scale = ZOOM_LEVELS[zoom_label]

            st.divider()
            bind_shortcut("d", "disregard_button")
            if st.button("Disregard PDF  `D`", key="disregard_button"):
                commit_disregard(pdf_path)

    if pdf_path is None:
        if in_flight_count:
            _wait_for_background()
        else:
            render_empty_state()
        return

    if st.session_state.stage == "marking":
        render_marking_view(pdf_path)
    elif st.session_state.stage == "recropping":
        render_recrop_view(pdf_path)
    else:
        render_comparison_gallery(pdf_path)

    # Emitted last, once, so every binding for this page coexists in one script.
    render_shortcuts()


if __name__ == "__main__":
    main()
