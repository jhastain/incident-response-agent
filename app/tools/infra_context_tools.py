"""Infrastructure context tools — loads service catalog and system overview from GCS.

The infrastructure-context repo files are synced to the environment's incident-summaries bucket under context/
and read at runtime to provide service → GCP project, hosting, Datadog tags,
Sentry project, and dependency mappings for investigation agents.

The full infrastructure-context repo is synced, organized as:
  /system-overview.md        — Index: lists all domain/pattern/system docs
  /service-catalog.md        — Per-service reference (GCP projects, hosting, etc.)
  /domains/*.md              — Domain knowledge (observability, networking, etc.)
  /systems/*.md              — System-specific architecture docs
  /patterns/**/*.md          — Operational patterns (compute, data-layer, security, etc.)
"""

import json

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import storage

from .gcs_tools import _bucket_name

# Same per-environment bucket as the incident corpus (see gcs_tools._bucket_name).
#
# Unlike the incident summaries the agent writes, everything under context/ is READ-ONLY
# reference data synced from the infrastructure-context repo — identical in every environment. A
# per-env bucket therefore needs that sync pointed at it too; until then a fresh test or
# prod bucket has an empty context/ prefix and these tools return "no files found" rather
# than erroring. Set INCIDENT_SUMMARIES_BUCKET to read another environment's copy.
_CONTEXT_PREFIX = "context/"
_MAX_READ_CHARS = 12_000


def list_context_files(prefix: str = "") -> str:
    """List available infrastructure context files in GCS.

    Use this to discover what context files are available. Start with no prefix
    to see top-level files and directories, then drill into subdirectories.

    Args:
        prefix: Optional subdirectory to list (e.g., 'domains/', 'patterns/service/compute/').
                Empty string lists the top level.

    Returns:
        JSON list of file paths available under the given prefix.
    """
    try:
        client = storage.Client()
        bucket = client.bucket(_bucket_name())
        full_prefix = f"{_CONTEXT_PREFIX}{prefix}"
        blobs = bucket.list_blobs(prefix=full_prefix)

        files = []
        for blob in blobs:
            rel_path = blob.name[len(_CONTEXT_PREFIX):]
            if rel_path and not rel_path.endswith("/"):
                files.append(rel_path)

        if not files:
            return json.dumps({"files": [], "hint": f"No files found under prefix '{prefix}'"})

        return json.dumps({"files": files, "count": len(files)})
    except GoogleAPICallError as e:
        return json.dumps({"error": f"list_context_files failed: {e}"})
    except Exception as e:
        return json.dumps({"error": f"list_context_files unexpected error: {type(e).__name__}: {e}"})


def load_infra_context(file_path: str) -> str:
    """Load an infrastructure context file from the GCS context prefix.

    The full infrastructure-context repo is synced to GCS. Use system-overview.md as the
    index to discover which domain/pattern/system files are relevant, then load
    them by path.

    Args:
        file_path: Path relative to the context prefix. Examples:
            - 'system-overview.md' (index — start here)
            - 'service-catalog.md' (per-service lookup)
            - 'domains/observability.md'
            - 'systems/webcore-monolith.md'
            - 'patterns/service/compute/cloud-run.md'
            - 'patterns/service/data-layer/sql-2022.md'

    Returns:
        The file content as a string, truncated to 12,000 chars if necessary.
    """
    try:
        client = storage.Client()
        bucket = client.bucket(_bucket_name())
        blob = bucket.blob(f"{_CONTEXT_PREFIX}{file_path}")

        if not blob.exists():
            return json.dumps({
                "error": f"Context file '{file_path}' not found",
                "hint": "Use list_context_files() to discover available files, or start with system-overview.md",
            })

        content = blob.download_as_text()
        if len(content) > _MAX_READ_CHARS:
            content = content[:_MAX_READ_CHARS] + (
                f"\n... [TRUNCATED — {len(content):,} chars, showing first {_MAX_READ_CHARS:,}]"
            )
        return content
    except GoogleAPICallError as e:
        return json.dumps({"error": f"load_infra_context failed: {e}"})
    except Exception as e:
        return json.dumps({"error": f"load_infra_context unexpected error: {type(e).__name__}: {e}"})


def get_service_context(service_name: str) -> str:
    """Look up a specific service's infrastructure context from the service catalog.

    Searches the service catalog for the named service and returns its section,
    including GCP project, hosting, pipelines, Sentry/Datadog tags, and dependencies.

    Args:
        service_name: Service to look up (e.g., 'WebCore', 'SubscriptionService',
                      'AccountingService'). Case-insensitive partial match.

    Returns:
        The relevant section from the service catalog, or a 'not found' message.
    """
    try:
        client = storage.Client()
        bucket = client.bucket(_bucket_name())
        blob = bucket.blob(f"{_CONTEXT_PREFIX}service-catalog.md")

        if not blob.exists():
            return json.dumps({"error": "service-catalog.md not found in GCS context prefix"})

        content = blob.download_as_text()

        search_lower = service_name.lower()
        sections = content.split("\n### ")

        matching_sections = []
        for section in sections:
            header = section.split("\n")[0].lower()
            if search_lower in header:
                text = "### " + section if not section.startswith("#") else section
                if len(text) > 3000:
                    text = text[:3000] + "\n... [TRUNCATED]"
                matching_sections.append(text)

        if matching_sections:
            return "\n---\n".join(matching_sections)

        return json.dumps({
            "error": f"Service '{service_name}' not found in service catalog",
            "hint": "Try exact service names: WebCore, BillingService, SubscriptionService, etc.",
        })
    except GoogleAPICallError as e:
        return json.dumps({"error": f"get_service_context failed: {e}"})
    except Exception as e:
        return json.dumps({"error": f"get_service_context unexpected error: {type(e).__name__}: {e}"})
