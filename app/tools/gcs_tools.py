"""GCS tools for reading and writing incident summary files.

Reads past incidents from the environment's incident-summaries bucket to inform
investigations, and writes new incident summaries after each pipeline run.
"""

import functools
import json
import os

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import storage

_MAX_READ_CHARS = 6_000


@functools.cache
def _bucket_name() -> str:
    """The environment's incident corpus bucket.

    Per-environment by design: the agent WRITES here, so a shared bucket would let a
    test run's output surface as real precedent in a prod investigation's
    historical-context lookup.

    INCIDENT_SUMMARIES_BUCKET overrides. Otherwise derived from GOOGLE_CLOUD_PROJECT,
    matching the IaC in infrastructure/<env>/us/storage_buckets/
    incident-summaries (<app>-<env>-incident-summaries). The dev bucket predates that
    convention and is bare `incident-summaries`, so dev is special-cased rather than
    renaming a live bucket.
    """
    if explicit := os.environ.get("INCIDENT_SUMMARIES_BUCKET"):
        return explicit
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if project == "aiops-dev-project":
        return "incident-summaries"
    env = project.removeprefix("aiops-").removesuffix("-project")
    # Matches the IaC name: <app>-<env>-storage-<description>-<continent>, the
    # convention the gateway-object-store buckets already use.
    return f"aiops-{env}-storage-incident-summaries-us" if env else "incident-summaries"


def list_incident_files(
    prefix: str = "", limit: int = 20
) -> str:
    """List incident summary files in the GCS bucket, optionally filtered by prefix.

    Use this to find past incidents by category, service, or severity.
    File names follow the convention: {date}_sev{N}_{category}_{service}_{description}.json

    Args:
        prefix: Filter files whose names start with this prefix (e.g., '2026-06' for June 2026,
                or use empty string to list all). Partial date prefixes work well.
        limit: Maximum number of files to return. Default is 20.

    Returns:
        JSON string containing a list of matching file names sorted by name descending (newest first).
    """
    try:
        client = storage.Client()
        bucket = client.bucket(_bucket_name())

        blobs = list(bucket.list_blobs(prefix=prefix, max_results=limit * 2))
        files = sorted(
            [{"name": b.name, "size": b.size, "updated": b.updated.isoformat() if b.updated else None} for b in blobs],
            key=lambda f: f["name"],
            reverse=True,
        )[:limit]

        return json.dumps({"bucket": _bucket_name(), "count": len(files), "files": files})
    except GoogleAPICallError as e:
        return json.dumps({"error": f"list_incident_files failed: {e}"})
    except Exception as e:
        return json.dumps({"error": f"list_incident_files unexpected error: {type(e).__name__}: {e}"})


def read_incident_file(object_name: str) -> str:
    """Read the contents of a specific incident summary JSON file from GCS.

    Use this to load a past incident's details including root_cause, timeline,
    lessons_learned, and key_metrics for context in the current investigation.

    Args:
        object_name: The full object name/path in the bucket
                     (e.g., '2026-06-15_sev3_deploy-release_cloud-run_api-west-clamav-probe-failure.json').

    Returns:
        JSON string containing the incident summary data.
    """
    try:
        client = storage.Client()
        bucket = client.bucket(_bucket_name())
        blob = bucket.blob(object_name)

        content = blob.download_as_text()
        if len(content) > _MAX_READ_CHARS:
            content = content[:_MAX_READ_CHARS] + f"\n... [TRUNCATED — {len(content):,} chars, showing first {_MAX_READ_CHARS:,}]"

        return content
    except GoogleAPICallError as e:
        return json.dumps({"error": f"read_incident_file failed: {e}"})
    except Exception as e:
        return json.dumps({"error": f"read_incident_file unexpected error: {type(e).__name__}: {e}"})


def write_incident_file(object_name: str, content: str) -> str:
    """Write an incident summary JSON file to the GCS bucket.

    Use this to persist the investigation results for future reference.
    The file name should follow the naming convention:
    {date}_sev{N}_{category}_{service}_{description}.json

    Args:
        object_name: The file name to write (e.g., '2026-06-15_sev2_sql-database_webcore_high-cpu-blocking.json').
        content: The JSON content to write (must be valid JSON string).

    Returns:
        JSON string confirming the write with the object name and size.
    """
    try:
        client = storage.Client()
        bucket = client.bucket(_bucket_name())
        blob = bucket.blob(object_name)

        blob.upload_from_string(content, content_type="application/json")

        return json.dumps({
            "status": "success",
            "bucket": _bucket_name(),
            "object": object_name,
            "size_bytes": len(content.encode("utf-8")),
        })
    except GoogleAPICallError as e:
        return json.dumps({"error": f"write_incident_file failed: {e}"})
    except Exception as e:
        return json.dumps({"error": f"write_incident_file unexpected error: {type(e).__name__}: {e}"})
