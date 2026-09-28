"""Training-data curation for SFT and preference (chosen/rejected) datasets.

``curator`` is this fork's own package. It lives next to the upstream
``text_dedup`` package in the same distribution and reuses it for
near-duplicate detection instead of re-implementing MinHash/LSH.

Importing ``curator`` must stay cheap: heavy dependencies (``datasets``,
``polars``, ``numpy``, ...) are imported lazily by the submodules that need
them, so the CLI starts fast and the package boundary stays explicit.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

DISTRIBUTION_NAME = "sft-data-curator"

try:
    __version__ = _distribution_version(DISTRIBUTION_NAME)
except PackageNotFoundError:  # pragma: no cover - only when run from an uninstalled source tree
    __version__ = "0.0.0+unknown"

__all__ = ["DISTRIBUTION_NAME", "__version__"]
