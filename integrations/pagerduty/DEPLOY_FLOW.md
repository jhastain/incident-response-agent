# PagerDuty Webhook Receiver — Deploy & Runtime Flow

The `ir-pagerduty-receiver` is a small FastAPI service on **Cloud Run** that turns a
PagerDuty `incident.triggered` event into an IR Agent investigation and posts the
resulting report back onto the same incident as a note.

It is a **separate deployable** from the agent itself — the agent runs on Vertex AI
Agent Engine (`reasoningEngines/1000000000000000002`); this receiver only calls it.

---

## Deploy process

`deploy.ps1` runs a single `gcloud run deploy --source` against
`aiops-dev-project / us-central1`. Cloud Build packages the source into a container,
pushes it to Artifact Registry, and Cloud Run creates/updates a public service that runs
as `ir-pd-receiver-sa`, with the engine URL as an env var and three secrets mounted from
Secret Manager.

```mermaid
flowchart TD
    subgraph LOCAL["Developer machine (deploy.ps1)"]
        SRC["Source dir<br/>main.py, agent_client.py,<br/>pagerduty_client.py"]
        META["../../deployment_metadata.json<br/>remote_agent_runtime_id"]
        META -->|"build IR_AGENT_ENGINE_URL"| ENVVAR["IR_AGENT_ENGINE_URL=<br/>.../reasoningEngines/1000000000000000002"]
    end

    SRC -->|"gcloud run deploy --source"| CB["Cloud Build<br/>builds container image"]
    CB --> AR["Artifact Registry<br/>image"]
    AR --> CR["Cloud Run service<br/>ir-pagerduty-receiver<br/>aiops-dev-project / us-central1"]

    subgraph FLAGS["Deploy flags on the service"]
        F1["--allow-unauthenticated<br/>(public endpoint)"]
        F2["--no-cpu-throttling<br/>(CPU always allocated)"]
        F3["--min-instances 1<br/>(no cold start)"]
        F4["--service-account<br/>ir-pd-receiver-sa"]
    end

    ENVVAR -->|"--set-env-vars"| CR
    SM["Secret Manager<br/>pagerduty-webhook-signing-secret<br/>pagerduty-rest-api-token<br/>pagerduty-from-email"] -->|"--set-secrets"| CR
    FLAGS -.applied to.-> CR

    CR --> SAROLE["ir-pd-receiver-sa needs<br/>roles/aiplatform.user on engine project<br/>(to call reasoningEngines streamQuery)"]
```

---

## Runtime request flow

PagerDuty POSTs the event; the receiver verifies the HMAC signature, ACKs **202
immediately**, then does the long investigation off the request path in FastAPI
`BackgroundTasks`. This split is mandatory — PagerDuty requires a fast 2xx, but the
investigation takes ~200–600s.

```mermaid
sequenceDiagram
    autonumber
    participant PD as PagerDuty
    participant CR as Cloud Run receiver<br/>(main.py)
    participant BG as BackgroundTasks worker<br/>(_process)
    participant AE as Vertex Agent Engine<br/>(reasoningEngines)

    PD->>CR: POST /pagerduty/webhook<br/>(incident.triggered + X-PagerDuty-Signature)
    Note over CR: verify_signature() HMAC-SHA256<br/>over RAW body
    alt bad / missing signature
        CR-->>PD: 403 (reject before any work)
    else valid signature
        Note over CR: parse event, filter to incident.triggered,<br/>self-exclusion check
        CR-->>PD: 202 ACK (immediate)
        CR->>BG: background.add_task(_process, incident_id, data)

        Note over BG: V3 payload omits the alert body
        BG->>PD: GET /incidents/{id}/alerts
        PD-->>BG: alert details (custom_details / body)
        Note over BG: build_alert_message()

        BG->>AE: POST :query async_create_session
        AE-->>BG: session_id (retries transient failures)
        BG->>AE: POST :streamQuery?alt=sse (~200-600s)
        AE-->>BG: SSE ADK Events
        Note over BG: extract longest report_agent text

        BG->>PD: POST /incidents/{id}/notes<br/>(From: PD_FROM_EMAIL)
        PD-->>BG: 201 note added to SAME incident
    end
```

---

## Why these flags

| Flag | Why it is required | What breaks without it |
|------|--------------------|------------------------|
| `--allow-unauthenticated` | PagerDuty webhook POSTs carry no GCP identity. The real security boundary is the `X-PagerDuty-Signature` HMAC-SHA256 check over the raw body (rejects 403 before any work). | An IAM-protected endpoint would 403 **every** delivery. |
| `--no-cpu-throttling` | The ~200–600s investigation runs in `BackgroundTasks` **after** the 202 response is sent. Always-allocated CPU keeps that work running. | Cloud Run throttles CPU to ~0 after a response, so every triage would be **silently dropped**. This is the single most important flag. |
| `--min-instances 1` | Keeps a warm instance on the webhook path. | Cold start could exceed PagerDuty's initial-ACK delivery timeout. |
| `--service-account ir-pd-receiver-sa` | Runtime identity used (via ADC) to call the Agent Engine `streamQuery`; needs `roles/aiplatform.user` on the engine project, plus read access to the mounted secrets. | Agent calls / secret reads would fail. |

---

## Safety-now nuance

The service is **public**, but `PD_WEBHOOK_SECRET` is currently a **placeholder** — the
real value only exists once the PagerDuty webhook subscription is created. Until then
every incoming webhook fails `verify_signature()` and returns **403**, so **nothing can
invoke the agent** yet. The public endpoint is inert without the real signing secret.
