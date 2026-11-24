"""Backwards-compatible module alias for ``xiaoba_review.review_store`` (W4-P3).

Exposes the exact same module object under both ``xiaoba_review.review_store``
(primary name) and ``xiaoba_review.store`` (legacy alias) so imports and
attribute patches (e.g. ``PENDING_REFERENCE_BATCH``) always target one source.
"""
import sys

from . import review_store as _review_store

sys.modules[__name__] = _review_store
