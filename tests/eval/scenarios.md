# IR agent end-to-end scenarios

Fixed inputs for reproducible evaluation. **These are synthetic.** The original set was built from real production incidents and was removed rather than edited.

Each scenario pairs an alert with a ground-truth file the judge scores against. The three cover three different Tier 2 paths: none, the SQL specialist, and the codebase specialist.

---

## Scenario 1: cloud-infrastructure-startup-probe

**Category:** `cloud-infrastructure`  
**Severity:** `sev3`  
**Environment:** `test`  
**Expected Tier 2:** none (Tier 1 sufficient)  
**Ground truth:** `ground-truth/scenario-1-cloud-infrastructure.json`

### Alert text

> Deploy step stalled - recurring-jobs Cloud Run startup probe timeout in us-central1, acme-prodtest-100001. Container image imported but the startup probe failed after 4m20s. 7/8 services healthy; recurring-jobs stuck. HealthCheckContainerError with zero container logs. The previous deploy of the same service was healthy in 20.37s with identical config.

---

## Scenario 2: sql-database-batch-update-blocking

**Category:** `sql-database`  
**Severity:** `sev2`  
**Environment:** `production`  
**Expected Tier 2:** `sql_dba_agent`  
**Ground truth:** `ground-truth/scenario-2-sql-database.json`

### Alert text

> [Datadog] sqlprod-c1 blocking chain depth above threshold, production. Lock waits climbing on the primary for 18 minutes. Multiple deadlock victims reported by the recalculation job. Read replicas unaffected.

---

## Scenario 3: application-errors-release-regression

**Category:** `application-errors`  
**Severity:** `sev2`  
**Environment:** `production`  
**Expected Tier 2:** `codebase_agent`  
**Ground truth:** `ground-truth/scenario-3-application-errors.json`

### Alert text

> [Sentry] webcore-dotnet - error rate on POST /api/admin/login rose from 0.2% to 9.4% over 12 minutes, production. 418 distinct users affected. No spike on /api/login.

---

## Last Reviewed: 1970-01-01
