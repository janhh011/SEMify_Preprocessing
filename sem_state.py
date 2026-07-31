"""Process-wide mutable state, shared by the UI thread and the background worker.

This deliberately lives outside `semify_app.py`. Streamlit re-executes the
main script top-to-bottom on every rerun, so anything defined at module level
*there* is re-initialised on every interaction — which silently emptied the
in-flight set (making a just-finished PDF reappear) and wiped the caches
(causing repeat Crossref lookups and the 429s behind "unknown" DOIs).

An imported module is cached in `sys.modules` and executed only once per
process, so state defined here survives reruns.
"""

import threading

# Cached docling conversions, keyed by (pdf_path, page_range | None).
docling_cache: dict = {}
docling_cache_lock = threading.Lock()

# Resolved publication metadata per PDF, so selecting N figures from one paper
# issues a single Crossref lookup instead of N identical ones.
doi_cache: dict = {}
doi_cache_lock = threading.Lock()

# PDFs whose finalize is running in the background — still physically in
# input_pdfs/ until it completes, but must not be re-offered as the next PDF.
in_flight_pdfs: set[str] = set()
in_flight_lock = threading.Lock()

# Page count + per-page point sizes per PDF: {path: (n_pages, [(w, h), ...])}.
# Only immutable plain data is cached, never the pdfium document object, which
# is not safe to share across threads.
pdf_meta_cache: dict = {}
pdf_meta_lock = threading.Lock()
