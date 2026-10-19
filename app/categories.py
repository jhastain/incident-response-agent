"""Alert categories and Tier 2 dispatch matrix for the IR agent."""

# The 11 alert categories from the 200-thread analysis
CATEGORIES: dict[str, dict] = {
    "application-errors": {
        "description": "Application-level exceptions, unhandled errors, HTTP 5xx responses",
        "keywords": ["exception", "error", "500", "unhandled", "crash", "stack trace", "NullReference", "5xx"],
        "frequency": "25%",
    },
    "business-logic": {
        "description": "Business rule violations, unexpected application behavior, data flow issues",
        "keywords": [
            "validation",
            "business rule",
            "unexpected behavior",
            "workflow",
            "calculation",
            "incorrect",
            "not activated",
            "race condition",
            "filter",
            "pricing",
        ],
        "frequency": "17%",
    },
    "deploy-release": {
        "description": "Pipeline failures, deployment issues, hotfix pushes, rollback events",
        "keywords": [
            "deploy",
            "pipeline",
            "rollback",
            "hotfix",
            "Octopus",
            "build failure",
            "deployment block",
            "package",
        ],
        "frequency": "14%",
    },
    "scheduled-jobs": {
        "description": "Hangfire recurring job errors, scheduled task failures, cron issues",
        "keywords": [
            "Hangfire",
            "cron",
            "recurring job",
            "scheduled",
            "job not running",
            "financial sync",
            "background job",
        ],
        "frequency": "11%",
    },
    "data-integrity": {
        "description": "Financial rounding discrepancies, data fix scripts, record corruption",
        "keywords": [
            "rounding",
            "data fix",
            "order item",
            "amendment",
            "bid",
            "penny",
            "discrepancy",
            "data corruption",
        ],
        "frequency": "8%",
    },
    "cloud-infrastructure": {
        "description": "GCP container startup issues, OOM kills, resource exhaustion, health check failures",
        "keywords": [
            "OOM",
            "memory",
            "CPU",
            "disk",
            "Valkey",
            "Redis",
            "container",
            "GCP",
            "health check",
            "resource",
        ],
        "frequency": "6%",
    },
    "sql-database": {
        "description": "SQL blocking chains, high CPU queries, deadlocks, tempdb growth",
        "keywords": [
            "SQL",
            "blocking",
            "deadlock",
            "CPU spike",
            "tempdb",
            "query",
            "60GB",
            "database",
            "SQL Agent",
        ],
        "frequency": "3%",
    },
    "auth-security": {
        "description": "DDoS attacks, Auth0 login failures, certificate expiry, token issues",
        "keywords": [
            "Auth0",
            "DDoS",
            "certificate",
            "token",
            "login",
            "WAF",
            "Cloud Armor",
            "security",
            "attack",
        ],
        "frequency": "3%",
    },
    "performance": {
        "description": "Site slowness, page load degradation, latency spikes, timeout errors",
        "keywords": ["latency", "slow", "timeout", "p99", "response time", "taking forever", "performance"],
        "frequency": "3%",
    },
    "monitoring-meta": {
        "description": "Synthetic test failures, uptime check flaps, false alarms",
        "keywords": ["uptime", "synthetic", "health check", "probe", "false alarm", "flapping", "monitoring"],
        "frequency": "3%",
    },
    "pubsub-messaging": {
        "description": "PubSub backlogs, dead letter queues, consumer failures, message delivery issues",
        "keywords": [
            "PubSub",
            "backlog",
            "dead letter",
            "subscription",
            "consumer",
            "undelivered",
            "unacked",
            "message",
        ],
        "frequency": "8%",
    },
    "content-archive": {
        "description": "Content Archive pipeline chain failures / no-runs (ContentArchive-Configure/Request; Datadog monitor 20000002). A chained ADO pipeline where the alerting pipeline has trigger:none and an upstream pipeline queues it.",
        "keywords": [
            "archive site",
            "contentarchive",
            "content-archive",
            "ContentArchive-Configure",
            "ContentArchive-Request",
            "configure pipeline did not run",
            "16605223",
            "archive provisioning",
        ],
        "frequency": "rare",
    },
    "unknown": {
        "description": "Unclassified alerts that do not match any known pattern",
        "keywords": [],
        "frequency": "8%",
    },
}

# Maps category to which Tier 2 agents should be dispatched
TIER2_DISPATCH_MATRIX: dict[str, list[str]] = {
    "application-errors": ["codebase_agent"],
    "business-logic": ["codebase_agent"],
    "deploy-release": ["codebase_agent"],
    "scheduled-jobs": ["sql_dba_agent"],
    "data-integrity": ["sql_dba_agent", "codebase_agent"],
    "cloud-infrastructure": [],  # Tier 1 sufficient
    "sql-database": ["sql_dba_agent"],
    "auth-security": ["auth_security_agent"],
    "performance": ["sql_dba_agent", "cache_agent"],
    # Misroute safety net, not a claim that monitoring-meta is a database problem.
    # On 2026-07-30 a real acme.example outage the DBAs were already calling
    # blocking classified as monitoring-meta, which mapped to nothing — so no agent
    # could produce lock evidence, and the pipeline invented a trigger from unrelated
    # Teams chatter. The real fix is classifier scope (an alert with user impact should
    # not land here at all), but routing must degrade safely when it does.
    "monitoring-meta": ["sql_dba_agent"],
    "pubsub-messaging": ["pubsub_agent"],
    "content-archive": ["codebase_agent"],  # deploy_correlation (tier1) walks the chain; codebase for dangling caller/callee YAML
    "unknown": [],  # Tier 1 sufficient
}

# Formatted category descriptions for the classifier prompt
CATEGORY_DESCRIPTIONS = "\n".join(
    f"- **{cat}**: {info['description']} (keywords: {', '.join(info['keywords'][:5])})"
    for cat, info in CATEGORIES.items()
)
