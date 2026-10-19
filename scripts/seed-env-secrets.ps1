# Seed the IR Agent's secret PAYLOADS into a target environment.
#
# Terraform creates the secret CONTAINERS and their IAM
# (infrastructure/<env>/global/secret_manager/terragrunt.hcl); it deliberately
# does not hold values. This script adds the first version to each container after that
# apply lands.
#
# Safe by default: every write is a NEW secret version (additive). Nothing is disabled,
# destroyed, or overwritten, and a container that already has a matching value is skipped.
# Run with -WhatIf first.
#
# Usage:
#   .\seed-env-secrets.ps1 -TargetProject aiops-test-project -WhatIf
#   .\seed-env-secrets.ps1 -TargetProject aiops-test-project
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetProject,

    # Source of the two reusable read-only credentials. These are the SAME credential in
    # every environment (a read-only ADO PAT and a read-only Octopus key), so they are
    # copied rather than re-issued. Verified present in aiops-dev-project 2026-08-03.
    [string]$SourceProject = "aiops-dev-project",

    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

# Copied from $SourceProject. Same value across environments by design.
# Source and target names differ: dev's containers predate the naming standard
# (IR_agent_*), while test/prod use lowercase-hyphenated per PR 109300 review.
$copyFromSource = @(
    @{ Source = "IR_agent_ado_readonly";         Target = "ir-agent-ado-readonly" },
    @{ Source = "IR_agent_octopus_task_watcher"; Target = "ir-agent-octopus-task-watcher" }
)

# Per-environment values. NEVER copied between environments — test's PagerDuty webhook
# signing secret comes from test's own PD webhook subscription and must not equal prod's.
# The VK is minted per Bifrost instance by mint-vk.ps1.
$perEnvironment = @(
    "incident-response-agent-bfvk",
    "pagerduty-webhook-signing-secret",
    "pagerduty-rest-api-token",
    "pagerduty-from-email"
)

function Test-SecretExists([string]$Name, [string]$Project) {
    gcloud secrets describe $Name --project=$Project 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

function Get-EnabledVersionCount([string]$Name, [string]$Project) {
    $v = gcloud secrets versions list $Name --project=$Project --filter="state=enabled" --format="value(name)" 2>$null
    if (-not $v) { return 0 }
    return @($v -split "`n" | Where-Object { $_ }).Count
}

Write-Host "=== Preflight: containers in $TargetProject ===" -ForegroundColor Cyan
$missing = @()
foreach ($s in (@($copyFromSource | ForEach-Object { $_.Target }) + $perEnvironment)) {
    if (Test-SecretExists $s $TargetProject) {
        $n = Get-EnabledVersionCount $s $TargetProject
        Write-Host ("  {0,-36} exists, {1} enabled version(s)" -f $s, $n)
    } else {
        Write-Host ("  {0,-36} MISSING" -f $s) -ForegroundColor Red
        $missing += $s
    }
}
if ($missing.Count -gt 0) {
    throw "$($missing.Count) container(s) missing in ${TargetProject}: $($missing -join ', '). Run the IaC pipeline (AIOps-IaC-BuildAndDeploy) first — this script only adds values to containers Terraform already created."
}

Write-Host "`n=== Copying reusable credentials from $SourceProject ===" -ForegroundColor Cyan
foreach ($pair in $copyFromSource) {
    $src = $pair.Source
    $dst = $pair.Target

    if (-not (Test-SecretExists $src $SourceProject)) {
        Write-Host "  $src : NOT in $SourceProject — skipping" -ForegroundColor Yellow
        continue
    }

    $val = gcloud secrets versions access latest --secret=$src --project=$SourceProject 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $val) {
        Write-Host "  $src : could not read from $SourceProject (permission?) — skipping" -ForegroundColor Yellow
        continue
    }

    # Skip if the target already holds this exact value — avoids a pointless new version.
    if ((Get-EnabledVersionCount $dst $TargetProject) -gt 0) {
        $cur = gcloud secrets versions access latest --secret=$dst --project=$TargetProject 2>$null
        if ($cur -eq $val) {
            Write-Host "  $src -> $dst : already matches source — skip" -ForegroundColor DarkGray
            continue
        }
        Write-Host "  $src -> $dst : target has a DIFFERENT value; adding a new version (old stays enabled)" -ForegroundColor Yellow
    }

    if ($WhatIf) {
        Write-Host "  [WhatIf] would add version to $dst in $TargetProject ($($val.Length) chars)" -ForegroundColor Magenta
    } else {
        $tmp = New-TemporaryFile
        try {
            # -NoNewline: a trailing newline becomes part of the secret and breaks bearer tokens.
            [System.IO.File]::WriteAllText($tmp.FullName, $val)
            gcloud secrets versions add $dst --project=$TargetProject --data-file=$tmp.FullName | Out-Null
            Write-Host "  $src -> $dst : version added" -ForegroundColor Green
        } finally {
            Remove-Item $tmp.FullName -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "`n=== Per-environment values (NOT copied) ===" -ForegroundColor Cyan
foreach ($s in $perEnvironment) {
    $n = Get-EnabledVersionCount $s $TargetProject
    if ($n -gt 0) {
        Write-Host "  $s : $n enabled version(s) — OK" -ForegroundColor Green
    } else {
        Write-Host "  $s : EMPTY — needs a value" -ForegroundColor Yellow
    }
}

Write-Host @"

Remaining manual steps for the empty per-environment secrets:

  incident-response-agent-bfvk
      .\mint-vk.ps1 -TargetProject $TargetProject

  pagerduty-rest-api-token / pagerduty-from-email
      From the environment's own PagerDuty service:
      gcloud secrets versions add pagerduty-rest-api-token --project=$TargetProject --data-file=-

  pagerduty-webhook-signing-secret
      Returned when the PD webhook subscription is created. Add it, then redeploy the
      receiver so it picks up the new version.
"@ -ForegroundColor Cyan
