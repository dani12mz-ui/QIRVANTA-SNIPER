param(
    [string]$Workspace = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path,
    [string]$Output = (Join-Path $PSScriptRoot 'offline-ema-stability.json')
)

$ErrorActionPreference = 'Stop'
$culture = [Globalization.CultureInfo]::InvariantCulture
$anchor = [datetimeoffset]'2026-11-10T22:00:00Z'
$anchorMs = $anchor.ToUnixTimeMilliseconds()
$protocolPath = Join-Path $PSScriptRoot 'PROTOCOL.json'
$protocol = Get-Content -Raw -LiteralPath $protocolPath | ConvertFrom-Json
if ($protocol.ema_absolute_tolerance -cne '0.001' -or -not $protocol.direction_requires_same) {
    throw 'Expected registered EMA50 threshold 0.001 and exact direction.'
}
$tolerance = [double]::Parse($protocol.ema_absolute_tolerance, $culture)
$alpha = 2.0 / 51.0

function Convert-CloseTime([long]$Millis) {
    [datetimeoffset]::FromUnixTimeMilliseconds($Millis).ToString("yyyy-MM-ddTHH:mm:ss'Z'", $culture)
}

function Read-Tape([string]$RelativePath, [string]$ExpectedHash, [int]$ExpectedRows) {
    $path = Join-Path $Workspace $RelativePath
    $sha = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($sha -cne $ExpectedHash) { throw "SHA-256 mismatch for $RelativePath" }
    $previousClose = [long]::MinValue
    $source = @(Import-Csv -LiteralPath $path | ForEach-Object {
        $barOpen = [long]$_.time_ms
        $barClose = [long]$_.close_ms
        $openPrice, $highPrice, $lowPrice, $closePrice = @($_.open, $_.high, $_.low, $_.close |
            ForEach-Object { [double]::Parse($_, $culture) })
        if ($barOpen -lt $previousClose -or $barClose -le $barOpen -or $barClose -gt $anchorMs -or
            $lowPrice -gt [Math]::Min($openPrice, $closePrice) -or
            $highPrice -lt [Math]::Max($openPrice, $closePrice)) {
            throw "Unexpected overlap/order, invalid OHLC, or out-of-scope candle in $RelativePath"
        }
        $previousClose = $barClose
        [pscustomobject]@{ OpenMs = $barOpen; CloseMs = $barClose; Close = $closePrice }
    })
    if ($source.Count -ne $ExpectedRows -or $source[-1].CloseMs -ne $anchorMs) {
        throw "Unexpected length or endpoint for $RelativePath"
    }
    $source
}

function Measure-EmaWindow($Source, [int]$End, [int]$Depth) {
    $first = $End - $Depth + 1
    if ($first -lt 0 -or $Depth -lt 3) { throw 'Insufficient source rows for the selected EMA window.' }
    $value = $Source[$first].Close
    $lag = $value
    for ($i = $first + 1; $i -le $End; $i++) {
        $value += $alpha * ($Source[$i].Close - $value)
        if ($i -eq $End - 2) { $lag = $value }
    }
    $close = $Source[$End].Close
    $direction = if ($close -gt $value -and $value -gt $lag) { 'UP' }
                 elseif ($close -lt $value -and $value -lt $lag) { 'DOWN' }
                 else { 'MIXED' }
    [pscustomobject][ordered]@{
        source_first_close_utc = Convert-CloseTime $Source[$first].CloseMs
        source_last_close_utc = Convert-CloseTime $Source[$End].CloseMs
        lag_source_close_utc = Convert-CloseTime $Source[$End - 2].CloseMs
        bars = $Depth
        seed_first_close = $Source[$first].Close
        close = $close
        ema50 = $value
        ema50_lag_two_bars = $lag
        direction = $direction
    }
}

function Compare-EmaWindows($Source, [int]$Shorter, [int]$Longer) {
    $last = $Source.Count - 1
    $first = [Math]::Max($Longer - 1, $last - 49)
    $rows = for ($i = $first; $i -le $last; $i++) {
        $left = Measure-EmaWindow $Source $i $Shorter
        $right = Measure-EmaWindow $Source $i $Longer
        $emaDelta = [Math]::Abs($left.ema50 - $right.ema50)
        $lagDelta = [Math]::Abs($left.ema50_lag_two_bars - $right.ema50_lag_two_bars)
        $directionMatch = $left.direction -ceq $right.direction
        [pscustomobject]@{
            known_at_utc = $left.source_last_close_utc
            ema50_abs_delta = $emaDelta
            ema50_lag_two_abs_delta = $lagDelta
            direction_same = $directionMatch
            within_registered_threshold = $directionMatch -and $emaDelta -le $tolerance -and $lagDelta -le $tolerance
        }
    }
    [ordered]@{
        shorter_depth = $Shorter
        longer_depth = $Longer
        scored_at_anchor = $rows[-1]
        matched_source_endpoints = $rows.Count
        first_source_endpoint_utc = $rows[0].known_at_utc
        max_ema50_abs_delta = ($rows.ema50_abs_delta | Measure-Object -Maximum).Maximum
        max_ema50_lag_two_abs_delta = ($rows.ema50_lag_two_abs_delta | Measure-Object -Maximum).Maximum
        differing_directions = @($rows | Where-Object { -not $_.direction_same }).Count
        all_within_registered_threshold = @($rows | Where-Object { -not $_.within_registered_threshold }).Count -eq 0
    }
}

$configurations = @(
    @{
        name = 'H4'
        path = 'outputs/professional-selector-htf/repro/inputs/H4-history.csv'
        sibling = 'outputs/donchian_20_10/reconstructed/H4.csv'
        hash = '550344b3c094128f1be56cc0572b474047b775c14df038924fca4aefd51c1fa3'
        count = 1330
        depths = @(100, 200, 300, 450, 600, 1200, 1330)
        short = @(300, 450, 600)
        long = @(450, 600, 1200)
    },
    @{
        name = 'D1'
        path = 'outputs/donchian_20_10/reconstructed/D1.csv'
        hash = 'ad97bfe06c69ee4bdb42faa71c04354576c87e10124d8e5e571c8db667db5be3'
        count = 222
        depths = @(50, 100, 150, 200, 222)
        short = @(100, 150, 200)
        long = @(150, 200, 222)
    }
)
$result = [ordered]@{
    status = 'OFFLINE_SENSITIVITY_ONLY'
    symbol = 'OANDA:XAUUSD'
    anchor_utc = Convert-CloseTime $anchorMs
    source_registration = 'https://github.com/dani12mz-ui/QIRVANTA-SNIPER/blob/9381a6bcf54e30be897904cf3af7069ede4677d5/research/donchian_20_10/evidence/data_manifest.json'
    input_definition = 'Existing TradingView OANDA M30 OHLC archives aggregated to New York wall-clock H4/D1 buckets by the registered Donchian study. Keep recorded gaps and shortened groups. No forward fill, interpolation, or native request representation.'
    method = 'EMA50 = previous value + (2/51)*(close-previous value), first observed close seeds each nested depth, compare common exact close endpoints and the EMA two observed source bars earlier. Score up to the final 50 common source endpoints.'
    threshold = [ordered]@{ ema50_absolute = $tolerance; lag_two_absolute = $tolerance; direction_equal = $true }
    limitations = @(
        'No row beyond the shared endpoint, no sealed data, no H1 tape.',
        'Cannot determine original request.security() values, requested source availability, or native seed. Aggregated source timestamps are defined by the prior registration.',
        'Stability at these source endpoints does not prove live or replay Pine stability. No READY, trade, or rule change tested.'
    )
    frames = [ordered]@{}
}
foreach ($item in $configurations) {
    $source = @(Read-Tape $item.path $item.hash $item.count)
    if ($item.sibling) { $null = Read-Tape $item.sibling $item.hash $item.count }
    $last = $source.Count - 1
    $result.frames[$item.name] = [ordered]@{
        path = $item.path
        identical_local_copy = $item.sibling
        sha256 = $item.hash
        rows = $source.Count
        first_open_utc = Convert-CloseTime $source[0].OpenMs
        first_close_utc = Convert-CloseTime $source[0].CloseMs
        endpoint = @($item.depths | ForEach-Object { Measure-EmaWindow $source $last $_ })
        pair_checks = @(for ($i = 0; $i -lt $item.short.Count; $i++) {
            Compare-EmaWindows $source $item.short[$i] $item.long[$i]
        })
    }
}
$json = $result | ConvertTo-Json -Depth 9
[IO.File]::WriteAllText($Output, $json + [char]10, [Text.UTF8Encoding]::new($false))

