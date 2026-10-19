# PagerDuty → Incident Response Agent Integration

Wires PagerDuty SRE alerts to the Incident Response Agent and returns the synthesized
triage back onto the originating incident. Implements ADO story
[#276333](https://dev.azure.com/acme/Infrastructure/_workitems/edit/276333).

## Flow

```
PD service incident.triggered
  → V3 webhook_subscription (HTTPS) → this receiver (Cloud Run)
  → verify X-PagerDuty-Signature → ACK 202 immediately (200 for ack+ignore)
  → [background] fetch alert body → invoke IR agent (Vertex Agent Engine, streamQuery)
  → post synthesized report back as a note on the SAME incident
```

The receiver is a **separate Cloud Run service**. The agent itself stays on Vertex AI
Agent Engine — this service only *calls* it, so it is intentionally outside the
`incident_response_agent/` package (which is bundled into the agent deployment).

## Which alerts trigger it

The Platform-Alerts Teams channel is fed by PagerDuty services on the **SRE team**
(escalation policy `PESCAL1`). The two confirmed, active feeders are:

| Service | ID | Source |
|---|---|---|
| Platform - Datadog - High | `PEXAMP1` | Datadog (prod) |
| Platform - Datadog - Low | `PEXAMP2` | Datadog (support hrs / lower envs) |

Subscribe the webhook to these two services (minimum, matches the Teams channel), or to
the whole SRE team (`team_reference`) to auto-cover new services. See below.

## Setup

### 1. Secrets (GCP Secret Manager, project aiops-dev-project)

| Secret | Value |
|---|---|
| `pagerduty-webhook-signing-secret` | Returned when the webhook subscription is created |
| `pagerduty-rest-api-token` | PagerDuty REST API token with incident **write** scope |
| `pagerduty-from-email` | A real PagerDuty user email (required in the `From` header) |

### 2. Runtime service account

`ir-pd-receiver-sa@aiops-dev-project.iam.gserviceaccount.com` needs:
- `roles/aiplatform.user` on `aiops-dev-project` (call reasoningEngines streamQuery)
- `secretmanager.secretAccessor` on the three secrets above

### 3. Deploy

```powershell
cd integrations/pagerduty
./deploy.ps1
```

Reads the current engine id from `deployment_metadata.json`, deploys to Cloud Run with
`--min-instances 1` (avoid cold start on the webhook path).

### 4. Create the PagerDuty webhook subscription

```
POST https://api.pagerduty.com/webhook_subscriptions
Authorization: Token token=<PD_REST_API_TOKEN>
Content-Type: application/json

{
  "webhook_subscription": {
    "type": "webhook_subscription",
    "description": "IR Agent incident receiver",
    "delivery_method": { "type": "http_delivery_method",
      "url": "https://<cloud-run-url>/pagerduty/webhook" },
    "events": ["incident.triggered"],
    "filter": { "type": "service_reference", "id": "PEXAMP1" }
  }
}
```

Repeat for `PEXAMP2` (one subscription per service), or use
`"filter": {"type":"team_reference","id":"PTEAMSR"}` for the whole SRE team.
**Capture the `secret` from the response** and store it as `pagerduty-webhook-signing-secret`.

## Security

- The endpoint may be public (PagerDuty posts are public); **signature verification is the
  security boundary** — every request is HMAC-SHA256 verified against the signing secret
  before any work happens.
- The receiver only ADDS notes and only subscribes to `incident.triggered`, so it cannot
  loop on its own output. Self-origin events are additionally skipped by `From` identity.

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI app: verify → ACK → background investigate → note back |
| `agent_client.py` | Vertex Agent Engine session-create + streamQuery + report extraction |
| `pagerduty_client.py` | Signature verify, alert-body fetch, note-back (REST) |
| `test_pagerduty_client.py` | Unit tests for signature + message building |
| `deploy.ps1` / `Dockerfile` / `requirements.txt` | Cloud Run deploy |

## Testing

```bash
python -m pytest integrations/pagerduty/test_pagerduty_client.py -q
```

## Notes / caveats

- The V3 webhook payload does **not** include the triggering alert body — only incident
  metadata. `fetch_alert_details` pulls `GET /incidents/{id}/alerts` to get the diagnostic
  `custom_details`/`body` the agent needs.
- Agent runs take ~200–300s (up to the ~600s streamQuery deadline). The webhook is ACKed
  immediately and the investigation runs in a background task — never inside the request.
- PagerDuty note cap is 65,535 chars; long reports are truncated at 60k.
