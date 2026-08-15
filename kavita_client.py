"""Placeholder for a future Kavita API push.

Deliberately not implemented in this pass. The pipeline currently relies
on embedded PDF XMP metadata (see pdf_writer.py) and lets Kavita's own
library scan pick it up, accepting Kavita's known PDF-metadata-parsing
flakiness as a separate problem to revisit later.

When this is built out, it should push metadata directly via Kavita's
REST API (auth, series/volume DTOs, field locking) to work around that
flakiness — without requiring changes to matcher.py, pdf_writer.py, or
review.py.
"""
