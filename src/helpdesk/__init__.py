"""IT Help Desk Agent Assistant — shared library.

Everything the classifier, resolver, and orchestrator agents share lives here:
the JSON contracts (:mod:`helpdesk.contracts`), configuration
(:mod:`helpdesk.config`), logging, tracing, the local/remote agent seam, and the
escalation store. Agent entrypoints and scripts stay thin.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
