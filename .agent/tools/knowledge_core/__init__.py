"""Knowledge Core: Foundation for Agent Knowledge Vault."""

__version__ = "1.0.0"

from .context import ContextBuilder, ContextBundle
from .provenance import SourceReader, SourceRef
from .approval import ApprovalApplier, ApplyConflictError, ApprovalError, ApprovalRequiredError
from .retrieval import KnowledgeRetriever, SearchResponse

__all__ = [
    "ContextBuilder",
    "ContextBundle",
    "KnowledgeRetriever",
    "SearchResponse",
    "SourceReader",
    "SourceRef",
    "ApprovalApplier",
    "ApplyConflictError",
    "ApprovalError",
    "ApprovalRequiredError",
]
