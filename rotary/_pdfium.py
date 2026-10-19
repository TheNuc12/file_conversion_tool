"""PDFium is not thread-safe, and the web server handles requests on several threads.

Every use of pypdfium2 anywhere in Rotary must hold this lock, or two page
renders at once can crash the whole process.
"""

import threading

LOCK = threading.RLock()
