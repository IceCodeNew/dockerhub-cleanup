"""Application exceptions."""


class CleanupError(RuntimeError):
    """A safe, user-facing cleanup failure."""


class ReferencedManifestError(CleanupError):
    """A manifest cannot be deleted while another manifest references it."""
