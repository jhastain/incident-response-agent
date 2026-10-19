<#
.SYNOPSIS
    Apply the IR agent's Datadog configuration (dashboard, monitors, span-based
    metrics) from the JSON files beside this script.

.DESCRIPTION
    Replaces the three Terragrunt units that used to live under
    infrastructure/test/global/incident_response_agent/datadog/.
    They are gone. There is no Terraform for IR agent Datadog config any more.

    WHY. The org-wide datadog provider pin in InfrastructureAsCode/datadog_root.hcl
    is ~>3.23.0, which has neither a datadog_spans_metric resource nor any
    llm_observability data source. Running two provider versions in one repo was
    rejected, so all three units became JSON applied by this script.

    IDEMPOTENCE. Every object is looked up before it is written. An object that
    already exists is UPDATED in place; only a genuinely missing one is created.
    Each JSON entry carries a `_datadog_id` recording the live object it owns;
    when that is absent the script falls back to a name lookup and REFUSES to act
    on an ambiguous match rather than guessing. When it creates an object it
    prints the new id and tells you to write it back into the JSON.

.PARAMETER Only
    Apply just one unit: dashboard, monitors, or spansmetrics. Default: all three.

.PARAMETER WhatIf
    Standard PowerShell dry run. No write request leaves the machine; the script
    prints the create-vs-update decision and a field-level diff for each object.
    Read requests (GET) still run -- they are what the diff is computed from.

.PARAMETER Site
    Datadog API host. Default api.us5.datadoghq.com, which is the site the agent
    reports to (DD_SITE=us5.datadoghq.com on the live Agent Engine deployment).

.INPUTS
    Credentials come from environment variables and are NEVER hardcoded:
      DD_API_KEY  -- Datadog API key,         sent as the DD-API-KEY header
      DD_APP_KEY  -- Datadog application key, sent as the DD-APPLICATION-KEY header
    Header names verified against the Datadog OpenAPI securitySchemes
    (https://docs.datadoghq.com/resources/json/full_spec_v2.json).

    The keys that work here are ir-agent-datadog-api-key / mcpgw-datadog-app-key in
    the target GCP project (the app key resolves to aiops-mcp-registry-app-key,
    FULL scope). aiops-readonly has only *_read scopes and every write fails.

      $env:DD_API_KEY = gcloud secrets versions access latest `
          --secret ir-agent-datadog-api-key --project aiops-test-project
      $env:DD_APP_KEY = gcloud secrets versions access latest `
          --secret mcpgw-datadog-app-key --project aiops-test-project

.EXAMPLE
    .\Apply-DatadogConfig.ps1 -WhatIf
    Show what would change across all three units. Writes nothing.

.EXAMPLE
    .\Apply-DatadogConfig.ps1 -Only dashboard
    Apply the dashboard only.

.NOTES
    API endpoints, verified 2026-08-26:
      Dashboards     https://docs.datadoghq.com/api/latest/dashboards/
                     POST /api/v1/dashboard | PUT /api/v1/dashboard/{id}
                     GET  /api/v1/dashboard/{id} | GET /api/v1/dashboard
      Monitors       https://docs.datadoghq.com/api/latest/monitors/
                     POST /api/v1/monitor | PUT /api/v1/monitor/{id}
                     GET  /api/v1/monitor/{id} | GET /api/v1/monitor?name=
      Spans metrics  https://docs.datadoghq.com/api/latest/spans-metrics/
                     POST  /api/v2/apm/config/metrics
                     PATCH /api/v2/apm/config/metrics/{metric_id}
                     GET   /api/v2/apm/config/metrics/{metric_id}

    ASCII punctuation only in this file. Non-ASCII punctuation parses as ANSI
    under Windows PowerShell 5.1 and breaks string terminators.
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('all', 'dashboard', 'monitors', 'spansmetrics')]
    [string]$Only = 'all',

    [string]$Site = 'api.us5.datadoghq.com'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BaseUrl = "https://$Site"

# ---------------------------------------------------------------------------
# Credentials -- environment only.
# ---------------------------------------------------------------------------
function Get-DdHeaders {
    $apiKey = $env:DD_API_KEY
    $appKey = $env:DD_APP_KEY

    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        throw "DD_API_KEY is not set. See the .NOTES block in this script for how to populate it from Secret Manager."
    }
    if ([string]::IsNullOrWhiteSpace($appKey)) {
        throw "DD_APP_KEY is not set. See the .NOTES block in this script for how to populate it from Secret Manager."
    }

    return @{
        'DD-API-KEY'         = $apiKey
        'DD-APPLICATION-KEY' = $appKey
        'Content-Type'       = 'application/json'
    }
}

# ---------------------------------------------------------------------------
# HTTP. Read calls always run, including under -WhatIf -- the diff needs them.
# Write calls are gated by ShouldProcess.
# ---------------------------------------------------------------------------
function Invoke-DdRead {
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$AllowNotFound
    )
    $uri = "$BaseUrl$Path"
    try {
        return Invoke-RestMethod -Method Get -Uri $uri -Headers (Get-DdHeaders)
    }
    catch {
        $status = $null
        if ($_.Exception.PSObject.Properties.Name -contains 'Response' -and $_.Exception.Response) {
            $status = [int]$_.Exception.Response.StatusCode
        }
        if ($AllowNotFound -and ($status -eq 404)) { return $null }
        throw "GET $uri failed ($status): $($_.Exception.Message)"
    }
}

function Invoke-DdWrite {
    param(
        [Parameter(Mandatory)][ValidateSet('Post', 'Put', 'Patch')][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Body,
        [Parameter(Mandatory)][string]$Target
    )
    $uri = "$BaseUrl$Path"
    $json = $Body | ConvertTo-Json -Depth 64 -Compress

    if (-not $PSCmdlet.ShouldProcess($Target, "$($Method.ToUpper()) $Path")) {
        return $null
    }

    # -Body as a UTF-8 byte array: passing a string lets the default encoding
    # mangle any non-ASCII that ever reaches a message field.
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    try {
        return Invoke-RestMethod -Method $Method -Uri $uri -Headers (Get-DdHeaders) -Body $bytes
    }
    catch {
        $detail = ''
        try {
            $stream = $_.Exception.Response.GetResponseStream()
            $reader = New-Object System.IO.StreamReader($stream)
            $detail = $reader.ReadToEnd()
        }
        catch { }
        throw "$($Method.ToUpper()) $uri failed: $($_.Exception.Message) $detail"
    }
}

# ---------------------------------------------------------------------------
# JSON helpers.
# ---------------------------------------------------------------------------

# Strip the `_`-prefixed documentation keys. They exist because JSON has no
# comment syntax and these files carry the reasoning the .hcl files used to.
# Datadog rejects unknown fields on some endpoints, so they never go on the wire.
function Remove-UnderscoreKeys {
    param($Node)

    if ($null -eq $Node) { return $null }

    # Scalars first: [string] is IEnumerable, so it has to be excluded before
    # the collection test, and StrictMode makes a wrong guess here fatal.
    if ($Node -is [string] -or $Node -is [ValueType]) { return $Node }

    if ($Node -is [System.Collections.IDictionary]) {
        $clean = [ordered]@{}
        foreach ($key in $Node.Keys) {
            if ([string]$key -like '_*') { continue }
            $clean[[string]$key] = Remove-UnderscoreKeys $Node[$key]
        }
        return [pscustomobject]$clean
    }

    if ($Node -is [System.Collections.IEnumerable]) {
        $out = @()
        foreach ($item in $Node) { $out += , (Remove-UnderscoreKeys $item) }
        return , $out
    }

    if ($Node -is [System.Management.Automation.PSCustomObject]) {
        $clean = [ordered]@{}
        foreach ($prop in $Node.PSObject.Properties) {
            if ($prop.Name -like '_*') { continue }
            $clean[$prop.Name] = Remove-UnderscoreKeys $prop.Value
        }
        return [pscustomobject]$clean
    }

    return $Node
}

function Read-ConfigJson {
    param([Parameter(Mandatory)][string]$Name)
    $path = Join-Path $ScriptDir $Name
    if (-not (Test-Path $path)) { throw "Config file not found: $path" }
    return Get-Content -Path $path -Raw -Encoding UTF8 | ConvertFrom-Json
}

# Canonical JSON for comparison: sorted keys, no whitespace. Compares only the
# fields WE declare, so Datadog's own server-set fields (id, created_at,
# author_handle, overall_state, ...) never register as a spurious change.
function ConvertTo-Canonical {
    param($Node)

    if ($null -eq $Node) { return 'null' }

    if ($Node -is [string]) { return '"' + $Node + '"' }
    if ($Node -is [bool]) { return $Node.ToString().ToLower() }
    if ($Node -is [ValueType]) { return [string]$Node }

    if ($Node -is [System.Collections.IDictionary]) {
        $parts = @()
        foreach ($key in ($Node.Keys | Sort-Object)) {
            $parts += '"' + [string]$key + '":' + (ConvertTo-Canonical $Node[$key])
        }
        return '{' + ($parts -join ',') + '}'
    }

    if ($Node -is [System.Collections.IEnumerable]) {
        $parts = @()
        foreach ($item in $Node) { $parts += (ConvertTo-Canonical $item) }
        return '[' + ($parts -join ',') + ']'
    }

    if ($Node -is [System.Management.Automation.PSCustomObject]) {
        $parts = @()
        foreach ($prop in ($Node.PSObject.Properties | Sort-Object Name)) {
            $parts += '"' + $prop.Name + '":' + (ConvertTo-Canonical $prop.Value)
        }
        return '{' + ($parts -join ',') + '}'
    }

    return [string]$Node
}

# True when every field we declare is already present on the live object with
# the same value. SUBSET semantics for objects: Datadog fills in its own
# defaults (a monitor's options gains include_tags, new_host_delay,
# notify_audit, require_full_window; a dashboard gains ids and timestamps), and
# an exact comparison would report those as drift on every single run, so the
# script would never be able to say NO CHANGE. Arrays and scalars compare
# exactly -- a tag list or a widget list is fully owned by the JSON.
function Test-SubsetMatch {
    param($Want, $Have)

    if ($null -eq $Want) { return ($null -eq $Have) }
    if ($null -eq $Have) { return $false }

    $wantIsObj = ($Want -is [System.Management.Automation.PSCustomObject]) -or ($Want -is [System.Collections.IDictionary])
    $haveIsObj = ($Have -is [System.Management.Automation.PSCustomObject]) -or ($Have -is [System.Collections.IDictionary])

    if ($wantIsObj -and $haveIsObj) {
        $wantProps = if ($Want -is [System.Collections.IDictionary]) {
            $Want.Keys | ForEach-Object { [pscustomobject]@{ Name = [string]$_; Value = $Want[$_] } }
        }
        else { $Want.PSObject.Properties }

        foreach ($p in $wantProps) {
            $haveVal = $null
            if ($Have -is [System.Collections.IDictionary]) {
                if (-not $Have.Contains($p.Name)) { return $false }
                $haveVal = $Have[$p.Name]
            }
            else {
                if ($Have.PSObject.Properties.Name -notcontains $p.Name) { return $false }
                $haveVal = $Have.PSObject.Properties[$p.Name].Value
            }
            if (-not (Test-SubsetMatch -Want $p.Value -Have $haveVal)) { return $false }
        }
        return $true
    }

    return ((ConvertTo-Canonical $Want) -eq (ConvertTo-Canonical $Have))
}

# Per-field diff of desired vs live, restricted to the fields we declare.
function Get-FieldDiff {
    param(
        [Parameter(Mandatory)]$Desired,
        $Live
    )
    $diffs = @()
    foreach ($prop in $Desired.PSObject.Properties) {
        $haveVal = $null
        $present = ($null -ne $Live) -and ($Live.PSObject.Properties.Name -contains $prop.Name)
        if ($present) { $haveVal = $Live.PSObject.Properties[$prop.Name].Value }

        if ($present -and (Test-SubsetMatch -Want $prop.Value -Have $haveVal)) { continue }

        $diffs += [pscustomobject]@{
            Field = $prop.Name
            Have  = if ($present) { ConvertTo-Canonical $haveVal } else { 'ABSENT' }
            Want  = ConvertTo-Canonical $prop.Value
        }
    }
    return $diffs
}

function Write-Plan {
    param(
        [Parameter(Mandatory)][string]$Action,
        [Parameter(Mandatory)][string]$Target,
        $Diffs
    )
    $color = if ($Action -eq 'CREATE') { 'Green' } elseif ($Action -eq 'UPDATE') { 'Yellow' } else { 'DarkGray' }
    Write-Host ("  [{0,-9}] {1}" -f $Action, $Target) -ForegroundColor $color
    if ($Diffs) {
        foreach ($d in $Diffs) {
            $have = $d.Have; if ($have.Length -gt 110) { $have = $have.Substring(0, 110) + '...' }
            $want = $d.Want; if ($want.Length -gt 110) { $want = $want.Substring(0, 110) + '...' }
            Write-Host ("      {0}" -f $d.Field) -ForegroundColor DarkCyan
            Write-Host ("        have: {0}" -f $have) -ForegroundColor DarkGray
            Write-Host ("        want: {0}" -f $want) -ForegroundColor DarkGray
        }
    }
}

# ---------------------------------------------------------------------------
# Dashboard.
#   create POST /api/v1/dashboard
#   update PUT  /api/v1/dashboard/{dashboard_id}   (full body, not a merge)
# ---------------------------------------------------------------------------
function Invoke-DashboardApply {
    Write-Host "`nDASHBOARD (dashboard_ir_agent.json)" -ForegroundColor Cyan

    $cfg = Read-ConfigJson 'dashboard_ir_agent.json'
    $body = Remove-UnderscoreKeys $cfg

    $id = $null
    if ($cfg.PSObject.Properties.Name -contains '_datadog_id') { $id = $cfg._datadog_id }

    $live = $null
    if ($id) {
        $live = Invoke-DdRead -Path "/api/v1/dashboard/$id" -AllowNotFound
        if (-not $live) {
            Write-Host "  _datadog_id '$id' is in the JSON but no such dashboard exists." -ForegroundColor Yellow
            Write-Host "  Refusing to create a second board under that title. Clear _datadog_id to create a new one." -ForegroundColor Yellow
            return
        }
    }
    else {
        # No pinned id. Match on exact title, and refuse an ambiguous result --
        # 'IR Agent - Observability' currently names two boards (dev + test).
        $all = Invoke-DdRead -Path '/api/v1/dashboard'
        $matches = @($all.dashboards | Where-Object { $_.title -eq $cfg.title })
        if ($matches.Count -gt 1) {
            $ids = ($matches | ForEach-Object { $_.id }) -join ', '
            throw "Ambiguous: $($matches.Count) dashboards are titled '$($cfg.title)' ($ids). Set _datadog_id in dashboard_ir_agent.json to the one this file owns."
        }
        if ($matches.Count -eq 1) {
            $id = $matches[0].id
            $live = Invoke-DdRead -Path "/api/v1/dashboard/$id" -AllowNotFound
            Write-Host "  Matched existing dashboard by title: $id (pin it as _datadog_id)." -ForegroundColor DarkGray
        }
    }

    if ($id) {
        $diffs = Get-FieldDiff -Desired $body -Live $live
        if (-not $diffs) {
            Write-Plan -Action 'NO CHANGE' -Target "dashboard $id ($($cfg.title))"
            return
        }
        Write-Plan -Action 'UPDATE' -Target "dashboard $id ($($cfg.title))" -Diffs $diffs
        $res = Invoke-DdWrite -Method Put -Path "/api/v1/dashboard/$id" -Body $body -Target "dashboard $id"
        if ($res) { Write-Host "  Updated: $BaseUrl/dashboard/$($res.id)" -ForegroundColor Green }
    }
    else {
        Write-Plan -Action 'CREATE' -Target "dashboard '$($cfg.title)'"
        $res = Invoke-DdWrite -Method Post -Path '/api/v1/dashboard' -Body $body -Target "dashboard '$($cfg.title)'"
        if ($res) {
            Write-Host "  Created id $($res.id). WRITE THIS BACK into dashboard_ir_agent.json as _datadog_id." -ForegroundColor Green
        }
    }
}

# ---------------------------------------------------------------------------
# Monitors.
#   create POST /api/v1/monitor
#   update PUT  /api/v1/monitor/{monitor_id}
# Names collide across environments, so _datadog_id is the real handle.
# ---------------------------------------------------------------------------
function Invoke-MonitorsApply {
    Write-Host "`nMONITORS (monitors_ir_agent.json)" -ForegroundColor Cyan

    $cfg = Read-ConfigJson 'monitors_ir_agent.json'

    if ($cfg.PSObject.Properties.Name -contains 'disabled_monitors') {
        foreach ($d in $cfg.disabled_monitors) {
            Write-Plan -Action 'SKIPPED' -Target "$($d.name) -- disabled in JSON, see _disabled_reason"
        }
    }

    foreach ($m in $cfg.monitors) {
        $body = Remove-UnderscoreKeys $m
        $id = $null
        if ($m.PSObject.Properties.Name -contains '_datadog_id') { $id = $m._datadog_id }

        $live = $null
        if ($id) {
            $live = Invoke-DdRead -Path "/api/v1/monitor/$id" -AllowNotFound
            if (-not $live) {
                Write-Host "  _datadog_id $id for '$($m.name)' does not exist. Clear it to allow a create." -ForegroundColor Yellow
                continue
            }
        }
        else {
            # name= is a substring filter, so re-check for an exact match, then
            # narrow by query. Refuse anything still ambiguous.
            $encoded = [uri]::EscapeDataString($m.name)
            $found = @(Invoke-DdRead -Path "/api/v1/monitor?name=$encoded")
            $exact = @($found | Where-Object { $_.name -eq $m.name })
            $narrow = @($exact | Where-Object { $_.query -eq $m.query })
            $candidates = if ($narrow.Count -gt 0) { $narrow } else { $exact }

            if ($candidates.Count -gt 1) {
                $ids = ($candidates | ForEach-Object { $_.id }) -join ', '
                throw "Ambiguous: $($candidates.Count) monitors match '$($m.name)' ($ids). Set _datadog_id in monitors_ir_agent.json."
            }
            if ($candidates.Count -eq 1) {
                $id = $candidates[0].id
                $live = $candidates[0]
                Write-Host "  Matched '$($m.name)' by name+query: $id (pin it as _datadog_id)." -ForegroundColor DarkGray
            }
        }

        if ($id) {
            # Datadog reclassifies some 'metric alert' submissions as
            # 'query alert' on the way in and reports the stored type back --
            # verified live 2026-08-26 on monitors 21633848 and 21633850, which
            # Terraform declared as 'metric alert' and the API returns as
            # 'query alert'. The two are the same monitor; treating them as a
            # difference would report drift on every run forever.
            $liveForDiff = $live | ConvertTo-Json -Depth 32 | ConvertFrom-Json
            if ($liveForDiff.type -eq 'query alert' -and $body.type -eq 'metric alert') {
                $liveForDiff.type = 'metric alert'
            }

            $diffs = Get-FieldDiff -Desired $body -Live $liveForDiff
            if (-not $diffs) {
                Write-Plan -Action 'NO CHANGE' -Target "monitor $id ($($m.name))"
                continue
            }
            Write-Plan -Action 'UPDATE' -Target "monitor $id ($($m.name))" -Diffs $diffs
            $res = Invoke-DdWrite -Method Put -Path "/api/v1/monitor/$id" -Body $body -Target "monitor $id ($($m.name))"
            if ($res) { Write-Host "  Updated monitor $($res.id)." -ForegroundColor Green }
        }
        else {
            Write-Plan -Action 'CREATE' -Target "monitor '$($m.name)'"
            $res = Invoke-DdWrite -Method Post -Path '/api/v1/monitor' -Body $body -Target "monitor '$($m.name)'"
            if ($res) {
                Write-Host "  Created monitor $($res.id). WRITE THIS BACK into monitors_ir_agent.json as _datadog_id." -ForegroundColor Green
            }
        }
    }
}

# ---------------------------------------------------------------------------
# Span-based metrics.
#   create POST  /api/v2/apm/config/metrics       body: data{id,type,attributes}
#   update PATCH /api/v2/apm/config/metrics/{id}  body: data{type,attributes}
#
# The metric NAME is the id, so lookup is a plain GET by name -- no search.
#
# PATCH accepts ONLY compute.include_percentiles, filter and group_by
# (SpansMetricUpdateAttributes in the v2 OpenAPI spec). aggregation_type and
# compute.path are immutable. Changing either requires DELETE + POST, which this
# script deliberately does NOT do -- deleting a span metric discards its history.
# It reports the mismatch and stops instead.
# ---------------------------------------------------------------------------
function Invoke-SpansMetricsApply {
    Write-Host "`nSPAN-BASED METRICS (spans_metrics_ir_agent.json)" -ForegroundColor Cyan

    $cfg = Read-ConfigJson 'spans_metrics_ir_agent.json'

    foreach ($metric in $cfg.metrics) {
        $name = $metric.id
        $attrs = Remove-UnderscoreKeys $metric.attributes

        $live = Invoke-DdRead -Path "/api/v2/apm/config/metrics/$name" -AllowNotFound

        if (-not $live) {
            Write-Plan -Action 'CREATE' -Target "spans metric $name" -Diffs (Get-FieldDiff -Desired $attrs -Live $null)
            $body = @{
                data = @{
                    id         = $name
                    type       = 'spans_metrics'
                    attributes = $attrs
                }
            }
            $res = Invoke-DdWrite -Method Post -Path '/api/v2/apm/config/metrics' -Body $body -Target "spans metric $name"
            if ($res) { Write-Host "  Created spans metric $($res.data.id)." -ForegroundColor Green }
            continue
        }

        $liveAttrs = $live.data.attributes

        # Immutable fields first. A PATCH carrying a changed aggregation_type or
        # path is silently ignored by the API, which would leave the JSON and the
        # live object permanently out of sync with no error.
        $immutable = @()
        if ($attrs.compute.aggregation_type -ne $liveAttrs.compute.aggregation_type) {
            $immutable += "compute.aggregation_type: live '$($liveAttrs.compute.aggregation_type)' vs desired '$($attrs.compute.aggregation_type)'"
        }
        $livePath = $null
        if ($liveAttrs.compute.PSObject.Properties.Name -contains 'path') { $livePath = $liveAttrs.compute.path }
        $wantPath = $null
        if ($attrs.compute.PSObject.Properties.Name -contains 'path') { $wantPath = $attrs.compute.path }
        if ($livePath -ne $wantPath) {
            $immutable += "compute.path: live '$livePath' vs desired '$wantPath'"
        }
        if ($immutable.Count -gt 0) {
            Write-Host "  [BLOCKED  ] spans metric $name" -ForegroundColor Red
            foreach ($i in $immutable) { Write-Host "      $i" -ForegroundColor Red }
            Write-Host "      PATCH cannot change these (SpansMetricUpdateAttributes accepts only" -ForegroundColor Red
            Write-Host "      compute.include_percentiles, filter, group_by). Delete and recreate the" -ForegroundColor Red
            Write-Host "      metric by hand -- that discards its history, so decide deliberately." -ForegroundColor Red
            continue
        }

        # Only the PATCH-able subset goes on the wire.
        $patchAttrs = [ordered]@{
            filter   = $attrs.filter
            group_by = $attrs.group_by
        }
        if ($attrs.compute.PSObject.Properties.Name -contains 'include_percentiles') {
            $patchAttrs['compute'] = @{ include_percentiles = $attrs.compute.include_percentiles }
        }
        $patchObj = [pscustomobject]$patchAttrs

        $diffs = Get-FieldDiff -Desired $patchObj -Live $liveAttrs
        if (-not $diffs) {
            Write-Plan -Action 'NO CHANGE' -Target "spans metric $name"
            continue
        }

        Write-Plan -Action 'UPDATE' -Target "spans metric $name" -Diffs $diffs
        $body = @{
            data = @{
                type       = 'spans_metrics'
                attributes = $patchObj
            }
        }
        $res = Invoke-DdWrite -Method Patch -Path "/api/v2/apm/config/metrics/$name" -Body $body -Target "spans metric $name"
        if ($res) { Write-Host "  Updated spans metric $($res.data.id)." -ForegroundColor Green }
    }
}

# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------
Write-Host "IR agent Datadog config apply" -ForegroundColor White
Write-Host "  site:   $BaseUrl"
Write-Host "  source: $ScriptDir"
Write-Host "  scope:  $Only"
if ($WhatIfPreference) {
    Write-Host "  MODE:   -WhatIf -- read-only. No create, update or delete will be sent." -ForegroundColor Yellow
}

# Fail fast on missing credentials rather than part-way through a unit.
$null = Get-DdHeaders

if ($Only -eq 'all' -or $Only -eq 'spansmetrics') { Invoke-SpansMetricsApply }
if ($Only -eq 'all' -or $Only -eq 'monitors') { Invoke-MonitorsApply }
if ($Only -eq 'all' -or $Only -eq 'dashboard') { Invoke-DashboardApply }

Write-Host "`nDone." -ForegroundColor White
if ($WhatIfPreference) {
    Write-Host "Nothing was written. Re-run without -WhatIf to apply." -ForegroundColor Yellow
}
