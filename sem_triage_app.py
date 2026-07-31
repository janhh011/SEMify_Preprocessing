"""HITL Streamlit app: triage SEM diagrams extracted from academic PDFs via docling."""

import base64
import html
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import requests
import streamlit as st

INPUT_DIR = Path("input_pdfs")
DISREGARD_DIR = Path("disregard")
PROCESSED_DIR = Path("processed_data")
COMPLETED_DIR = Path("completed")
OUTPUT_DIR = Path("output")
TRACKER_PATH = PROCESSED_DIR / "tracker.json"
BATCH_PATH = OUTPUT_DIR / "batch_requests.jsonl"

# Images with both dimensions below this (logos, icons, decorative marks)
# are auto-disregarded at ingestion and never enter the triage queue.
MIN_IMAGE_DIMENSION = 100

TRIAGE_IMAGE_HEIGHT_PX = 480
TRIAGE_CONTEXT_HEIGHT_PX = 160


def ensure_dirs() -> None:
    for d in (INPUT_DIR, DISREGARD_DIR, PROCESSED_DIR, COMPLETED_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_tracker() -> dict:
    if TRACKER_PATH.exists():
        return json.loads(TRACKER_PATH.read_text(encoding="utf-8"))
    return {}


def save_tracker(tracker: dict) -> None:
    tmp_path = TRACKER_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(tracker, indent=2), encoding="utf-8")
    tmp_path.replace(TRACKER_PATH)


def _log(entry: dict, message: str) -> None:
    entry.setdefault("log", []).append(f"{datetime.now(timezone.utc).isoformat()} {message}")


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

    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )


def find_new_pdfs(tracker: dict) -> list[Path]:
    return [p for p in sorted(INPUT_DIR.glob("*.pdf")) if p.name not in tracker]


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


def _ordered_body_items(document):
    try:
        return [item for item, _level in document.iterate_items(traverse_pictures=True)]
    except AttributeError:
        return None


def find_context_text(document, picture_item) -> str:
    items = _ordered_body_items(document)
    if items is not None:
        try:
            idx = items.index(picture_item)
        except ValueError:
            idx = None
        if idx is not None:
            preceding_texts: list[str] = []
            for item in reversed(items[:idx]):
                text = getattr(item, "text", None)
                if text:
                    preceding_texts.append(text)
                    if len(text) > 40:
                        break
                elif hasattr(item, "get_image"):
                    # hit another picture while scanning backwards; stop
                    break
            if preceding_texts:
                return "\n\n".join(reversed(preceding_texts))
    return _find_context_via_markdown_string_search(document, picture_item)


def _candidate_anchor_string(document, picture_item) -> str | None:
    try:
        caption = picture_item.caption_text(document)
    except Exception:
        caption = None
    if caption:
        return caption
    return getattr(picture_item, "self_ref", None) or None


def _context_by_ordinal_paragraph(document, picture_item) -> str:
    try:
        ordinal = document.pictures.index(picture_item)
    except (ValueError, AttributeError):
        return ""
    markdown_text = document.export_to_markdown()
    paragraphs = [p for p in markdown_text.split("\n\n") if p.strip()]
    image_gaps = [i for i, p in enumerate(paragraphs) if p.strip().startswith("!")]
    if ordinal < len(image_gaps):
        gap_idx = image_gaps[ordinal]
        if gap_idx > 0:
            return paragraphs[gap_idx - 1]
    return ""


def _find_context_via_markdown_string_search(document, picture_item) -> str:
    markdown_text = document.export_to_markdown()
    anchor = _candidate_anchor_string(document, picture_item)
    if anchor:
        pos = markdown_text.find(anchor)
        if pos != -1:
            preceding = markdown_text[:pos].rstrip()
            paragraphs = [p for p in preceding.split("\n\n") if p.strip()]
            if paragraphs:
                return paragraphs[-1]
    return _context_by_ordinal_paragraph(document, picture_item)


def _best_effort_section_text(document, keywords: tuple[str, ...]) -> str:
    items = getattr(document, "texts", [])
    collecting = False
    collected: list[str] = []
    for item in items:
        label = str(getattr(item, "label", "")).lower()
        text = getattr(item, "text", "") or ""
        is_heading = "title" in label or "section_header" in label
        if is_heading:
            if collecting:
                break
            # Academic PDFs commonly render section headings letter-spaced
            # (e.g. "A B S T R A C T"), so match on whitespace-stripped text.
            normalized = text.lower().replace(" ", "")
            if any(kw in normalized for kw in keywords):
                collecting = True
            continue
        if collecting and text:
            collected.append(text)
    return "\n\n".join(collected)


def find_extended_context(document, picture_item) -> dict:
    return {
        "surrounding_paragraph": find_context_text(document, picture_item),
        "abstract": _best_effort_section_text(document, ("abstract",)),
        "conclusion": _best_effort_section_text(document, ("conclusion", "discussion")),
    }


def run_ingestion(tracker: dict) -> dict:
    converter = get_converter()
    for pdf_path in find_new_pdfs(tracker):
        result = converter.convert(str(pdf_path))
        document = result.document
        stem = pdf_path.stem
        out_dir = PROCESSED_DIR / stem
        img_dir = out_dir / "images"
        img_dir.mkdir(parents=True, exist_ok=True)

        markdown_text = document.export_to_markdown()
        md_path = out_dir / f"{stem}.md"
        md_path.write_text(markdown_text, encoding="utf-8")

        title = extract_title(document) or stem

        images_meta = []
        skip_messages = []
        for idx, picture in enumerate(document.pictures):
            image_path = img_dir / f"{stem}_img_{idx}.png"
            try:
                pil_image = picture.get_image(document)
            except Exception:
                pil_image = None
            if pil_image is None:
                continue
            width, height = pil_image.size
            if width < MIN_IMAGE_DIMENSION and height < MIN_IMAGE_DIMENSION:
                skip_messages.append(f"skipped logo-sized image_{idx} ({width}x{height})")
                continue
            pil_image.save(image_path)
            images_meta.append(
                {
                    "image_path": str(image_path),
                    "decision": None,
                    "page_no": _picture_page_no(picture),
                    "picture_index": idx,
                    "context_text": find_context_text(document, picture),
                    "context_extended": find_extended_context(document, picture),
                }
            )

        entry = {
            "pdf_filename": pdf_path.name,
            "title": title,
            "markdown_path": str(md_path),
            "images": images_meta,
            "phase3_done": False,
            "log": [],
        }
        _log(entry, f"ingested {len(images_meta)} images")
        for message in skip_messages:
            _log(entry, message)

        if len(images_meta) == 0:
            dest = DISREGARD_DIR / pdf_path.name
            shutil.move(str(pdf_path), str(dest))
            entry["phase3_done"] = True
            _log(entry, "zero images -> moved to disregard/")

        tracker[pdf_path.name] = entry
    return tracker


def build_pending_queue(tracker: dict) -> list[dict]:
    queue = []
    for pdf_filename, entry in tracker.items():
        for img in entry["images"]:
            if img["decision"] is None:
                queue.append(
                    {
                        "pdf_filename": pdf_filename,
                        "image_path": img["image_path"],
                    }
                )
    return queue


def _record_decision(tracker: dict, current: dict, decision: str) -> None:
    entry = tracker[current["pdf_filename"]]
    for img in entry["images"]:
        if img["image_path"] == current["image_path"]:
            img["decision"] = decision
    save_tracker(tracker)
    if st.session_state.pending_queue and st.session_state.current_index < len(
        st.session_state.pending_queue
    ):
        st.session_state.pending_queue.pop(st.session_state.current_index)
    if st.session_state.current_index >= len(st.session_state.pending_queue):
        st.session_state.current_index = max(0, len(st.session_state.pending_queue) - 1)
    st.rerun()


def _render_fixed_height_image(image_path: str, height_px: int) -> None:
    data = base64.standard_b64encode(Path(image_path).read_bytes()).decode("utf-8")
    st.markdown(
        f"""
        <div style="height:{height_px}px; display:flex; align-items:center;
                    justify-content:center; background:rgba(128,128,128,0.08);
                    border-radius:8px;">
            <img src="data:image/png;base64,{data}"
                 style="max-height:100%; max-width:100%; object-fit:contain;" />
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_fixed_height_context(text: str, height_px: int) -> None:
    safe_text = html.escape(text).replace("\n", "<br>")
    st.markdown(
        f"""
        <div style="height:{height_px}px; overflow-y:auto; padding:0.5rem;
                    border:1px solid rgba(128,128,128,0.3); border-radius:8px;">
            {safe_text}
        </div>
        """,
        unsafe_allow_html=True,
    )


def run_triage_ui(tracker: dict) -> None:
    queue = st.session_state.pending_queue
    if not queue:
        st.success("No pending images to triage. Click 'Finish & Generate Batch' when ready.")
        return

    idx = max(0, min(st.session_state.current_index, len(queue) - 1))
    st.session_state.current_index = idx
    current = queue[idx]

    st.progress((idx + 1) / len(queue))
    st.caption(f"Image {idx + 1} of {len(queue)} — {current['pdf_filename']}")

    _render_fixed_height_image(current["image_path"], TRIAGE_IMAGE_HEIGHT_PX)

    entry = tracker[current["pdf_filename"]]
    img_record = next(i for i in entry["images"] if i["image_path"] == current["image_path"])
    st.markdown("**Preceding context:**")
    _render_fixed_height_context(
        img_record.get("context_text") or "(no context found)", TRIAGE_CONTEXT_HEIGHT_PX
    )

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        if st.button("Prior Image", disabled=(idx == 0)):
            st.session_state.current_index = max(0, idx - 1)
            st.rerun()
    with col2:
        if st.button("Next Image", disabled=(idx == len(queue) - 1)):
            st.session_state.current_index = min(len(queue) - 1, idx + 1)
            st.rerun()
    with col3:
        if st.button("Mark as SEM"):
            _record_decision(tracker, current, "SEM")
    with col4:
        if st.button("Mark as NO SEM"):
            _record_decision(tracker, current, "NO_SEM")


def _crossref_lookup(title: str) -> tuple[str | None, str | None]:
    try:
        resp = requests.get(
            "https://api.crossref.org/works",
            params={"query.title": title, "rows": 1},
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
        if not items:
            return None, None
        top = items[0]
        doi = top.get("DOI")
        container = top.get("container-title") or []
        journal = container[0] if container else None
        return doi, journal
    except requests.RequestException as exc:
        print(f"[crossref] lookup failed for title={title!r}: {exc}")
        return None, None
    except (ValueError, KeyError, IndexError) as exc:
        print(f"[crossref] unexpected response shape for title={title!r}: {exc}")
        return None, None


def _build_batch_payload(entry: dict, img: dict, doi: str | None, journal: str | None) -> dict:
    image_path = Path(img["image_path"])
    b64_image = base64.standard_b64encode(image_path.read_bytes()).decode("utf-8")
    context = img.get("context_extended", {})
    custom_id = f"{entry['pdf_filename']}::{image_path.stem}"

    # TODO: PLACEHOLDER payload. The real Anthropic Batch API request shape
    # (model, prompt, output schema for SEM extraction) is deferred and must
    # be filled in later.
    return {
        "custom_id": custom_id,
        "params": {
            "model": "PLACEHOLDER_MODEL",  # TODO: set real model
            "max_tokens": 1024,  # TODO: tune
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64_image,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "TODO: real prompt goes here. Context follows.\n"
                                f"DOI: {doi}\nJournal: {journal}\n"
                                f"Abstract: {context.get('abstract', '')}\n"
                                f"Conclusion: {context.get('conclusion', '')}\n"
                                f"Surrounding text: {context.get('surrounding_paragraph', '')}\n"
                            ),
                        },
                    ],
                }
            ],
        },
    }


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def run_finalize(tracker: dict) -> None:
    for pdf_filename, entry in tracker.items():
        if entry.get("phase3_done"):
            continue
        images = entry["images"]
        if not images:
            continue

        all_decided = all(img["decision"] is not None for img in images)
        if not all_decided:
            continue

        sem_images = [img for img in images if img["decision"] == "SEM"]
        pdf_path_input = INPUT_DIR / pdf_filename

        if not sem_images:
            if pdf_path_input.exists():
                shutil.move(str(pdf_path_input), str(DISREGARD_DIR / pdf_filename))
            entry["phase3_done"] = True
            _log(entry, "all images NO_SEM -> moved to disregard/")
            continue

        doi, journal = _crossref_lookup(entry["title"])
        if doi:
            st.info(f"DOI found for **{pdf_filename}**: `{doi}` ({journal or 'journal unknown'})")
        else:
            st.warning(f"No DOI found for **{pdf_filename}**.")

        for img in sem_images:
            payload = _build_batch_payload(entry, img, doi, journal)
            _append_jsonl(BATCH_PATH, payload)

        if pdf_path_input.exists():
            shutil.move(str(pdf_path_input), str(COMPLETED_DIR / pdf_filename))
        entry["phase3_done"] = True
        _log(entry, f"{len(sem_images)} SEM image(s) -> batch_requests.jsonl; moved to completed/")

    save_tracker(tracker)
    st.success("Finalization complete.")


def main() -> None:
    st.set_page_config(page_title="SEM Triage", layout="wide")
    ensure_dirs()
    tracker = load_tracker()

    st.title("SEM Diagram Triage")

    with st.sidebar:
        st.header("Pipeline Controls")
        if st.button("Process New PDFs"):
            with st.spinner("Converting PDFs with docling..."):
                tracker = run_ingestion(tracker)
                save_tracker(tracker)
            st.session_state.pending_queue = build_pending_queue(tracker)
            st.session_state.current_index = 0
            st.success("Ingestion complete.")

        st.divider()
        if st.button("Finish & Generate Batch", type="primary"):
            run_finalize(tracker)

    st.session_state.setdefault("current_index", 0)
    if "pending_queue" not in st.session_state:
        st.session_state.pending_queue = build_pending_queue(tracker)

    run_triage_ui(tracker)


if __name__ == "__main__":
    main()
