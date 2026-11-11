"""Theodore Director v2 domain layer.

The legacy HTTP application imports this package as a compatibility-safe
sidecar. No v2 module starts services or mutates files at import time.
"""

from .api import DirectorV2API

__all__ = ["DirectorV2API"]
