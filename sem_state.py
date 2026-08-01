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

# pdfium is a C library and is not safe to call from two threads at once.
# The UI thread rasterises pages while the worker thread crops and reads page
# sizes, which segfaulted the process. Every pdfium call takes this lock.
#
# It must be *docling's* lock, not one of our own. docling drives pypdfium2
# too, and serialises its own calls with this object — so a private lock here
# would guard our calls against each other while leaving them free to run
# concurrently with docling's. Two locks around one library is the same as no
# lock: it segfaulted in pdfium's process-global font cache (CFX_Face) when a
# background finalize loaded a page while the UI rendered the next PDF. The
# global cache is why two *different* documents still corrupt each other.
#
# Never acquire this re-entrantly: docling's is a plain Lock, not an RLock.
# Every caller in semify_app takes it, uses pdfium, and releases it.
try:
    from docling.utils.locks import pypdfium2_lock as pdfium_lock
except Exception as exc:  # pragma: no cover - docling missing or moved the lock
    pdfium_lock = threading.Lock()
    print(
        "[sem_state] WARNING: could not import docling's pypdfium2 lock "
        f"({exc}). Falling back to a private lock, which does NOT serialise "
        "against docling's own pdfium calls — expect segfaults under load. "
        "Check whether docling.utils.locks moved."
    )

# Page count + per-page point sizes per PDF: {path: (n_pages, [(w, h), ...])}.
# Only immutable plain data is cached, never the pdfium document object, which
# is not safe to share across threads.
pdf_meta_cache: dict = {}
pdf_meta_lock = threading.Lock()
