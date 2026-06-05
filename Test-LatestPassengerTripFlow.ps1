param(
    [string]$BaseUrl,
    [string]$Email,
    [string]$Otp,
    [switch]$SendOtp,
    [switch]$IncludeInvoice
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-RequiredText {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    while ($true) {
        $value = Read-Host $Label
        $value = $value.Trim()

        if ($value.Length -gt 0) {
            return $value
        }

        Write-Host "Required. Please enter a value." -ForegroundColor Yellow
    }
}

function Normalize-BaseUrl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    return $Value.Trim().TrimEnd("/")
}

function ConvertTo-PrettyJson {
    param(
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [object]$Value
    )

    if ($null -eq $Value) {
        return "null"
    }

    try {
        if ($Value -is [string]) {
            $parsed = $Value | ConvertFrom-Json -Depth 100
            return ($parsed | ConvertTo-Json -Depth 100)
        }

        return ($Value | ConvertTo-Json -Depth 100)
    }
    catch {
        return [string]$Value
    }
}

function Invoke-Api {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,

        [Parameter(Mandatory = $true)]
        [ValidateSet("GET", "POST")]
        [string]$Method,

        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $false)]
        [object]$Body = $null,

        [Parameter(Mandatory = $false)]
        [switch]$Auth
    )

    $url = "$script:BaseUrl$Path"

    Write-Host ""
    Write-Host "================================================================================" -ForegroundColor DarkGray
    Write-Host $Label -ForegroundColor Cyan
    Write-Host "$Method $url" -ForegroundColor DarkGray
    Write-Host "================================================================================" -ForegroundColor DarkGray

    $headers = @{
        "Accept" = "application/json"
    }

    if ($Auth) {
        if ([string]::IsNullOrWhiteSpace($script:AccessToken)) {
            throw "Auth requested, but access token is empty."
        }

        $headers["Authorization"] = "Bearer $script:AccessToken"
    }

    $requestParams = @{
        Method             = $Method
        Uri                = $url
        Headers            = $headers
        SkipHttpErrorCheck = $true
    }

    if ($null -ne $Body) {
        $requestParams["ContentType"] = "application/json"
        $requestParams["Body"] = ($Body | ConvertTo-Json -Depth 100)
    }

    try {
        $response = Invoke-WebRequest @requestParams

        Write-Host "HTTP $($response.StatusCode)" -ForegroundColor Magenta

        if ([string]::IsNullOrWhiteSpace($response.Content)) {
            Write-Host "<empty response>"
            return $null
        }

        Write-Host (ConvertTo-PrettyJson $response.Content)

        try {
            return ($response.Content | ConvertFrom-Json -Depth 100)
        }
        catch {
            return $response.Content
        }
    }
    catch {
        Write-Host "REQUEST FAILED" -ForegroundColor Red
        Write-Host $_.Exception.Message -ForegroundColor Red
        return $null
    }
}

function Get-ResponseObject {
    param(
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [object]$Response,

        [Parameter(Mandatory = $true)]
        [string[]]$PreferredKeys
    )

    if ($null -eq $Response) {
        return $null
    }

    foreach ($key in $PreferredKeys) {
        if ($null -ne $Response.PSObject.Properties[$key]) {
            $value = $Response.$key
            if ($null -ne $value) {
                return $value
            }
        }
    }

    return $Response
}

function Get-ArrayItems {
    param(
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [object]$Response
    )

    if ($null -eq $Response) {
        return @()
    }

    if ($Response -is [System.Array]) {
        return @($Response)
    }

    if ($null -ne $Response.PSObject.Properties["items"]) {
        return @($Response.items)
    }

    if ($null -ne $Response.PSObject.Properties["booking_sessions"]) {
        return @($Response.booking_sessions)
    }

    return @()
}

function Sort-ByMostRecent {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Items
    )

    return @(
        $Items |
            Sort-Object `
                @{ Expression = {
                    if ($null -ne $_.PSObject.Properties["created_at"] -and $_.created_at) {
                        try { return [datetime]$_.created_at } catch { return [datetime]::MinValue }
                    }

                    return [datetime]::MinValue
                }; Descending = $true },
                @{ Expression = {
                    if ($null -ne $_.PSObject.Properties["updated_at"] -and $_.updated_at) {
                        try { return [datetime]$_.updated_at } catch { return [datetime]::MinValue }
                    }

                    return [datetime]::MinValue
                }; Descending = $true }
    )
}

function Get-TripStopForSessionStop {
    param(
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [object]$ScheduledTrip,

        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [string]$StopId,

        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [object]$SequenceNo
    )

    if ($null -eq $ScheduledTrip) {
        return $null
    }

    if ($null -eq $ScheduledTrip.PSObject.Properties["stops"] -or $null -eq $ScheduledTrip.stops) {
        return $null
    }

    $stops = @($ScheduledTrip.stops)

    if (-not [string]::IsNullOrWhiteSpace($StopId)) {
        $matchedByStop = @(
            $stops | Where-Object {
                $null -ne $_.PSObject.Properties["stop"] -and
                $null -ne $_.stop -and
                $null -ne $_.stop.PSObject.Properties["id"] -and
                "$($_.stop.id)" -eq "$StopId"
            }
        )

        if ($matchedByStop.Count -gt 0) {
            return $matchedByStop[0]
        }
    }

    if ($null -ne $SequenceNo) {
        $matchedBySequence = @(
            $stops | Where-Object {
                $null -ne $_.PSObject.Properties["sequence_no"] -and
                "$($_.sequence_no)" -eq "$SequenceNo"
            }
        )

        if ($matchedBySequence.Count -gt 0) {
            return $matchedBySequence[0]
        }
    }

    return $null
}

if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
    $BaseUrl = Read-RequiredText "Base URL, example http://127.0.0.1:8000"
}

if ([string]::IsNullOrWhiteSpace($Email)) {
    $Email = Read-RequiredText "Passenger email"
}

$script:BaseUrl = Normalize-BaseUrl $BaseUrl
$script:AccessToken = ""

Write-Host ""
Write-Host "Passenger latest booking session / trip API flow test" -ForegroundColor Green
Write-Host "Base URL: $script:BaseUrl" -ForegroundColor DarkGray
Write-Host "Email: $Email" -ForegroundColor DarkGray

if ($SendOtp) {
    Invoke-Api `
        -Label "A1. Send passenger login OTP" `
        -Method "POST" `
        -Path "/auth/login/send-otp" `
        -Body @{
            email = $Email
            role  = "passenger"
        } `
        | Out-Null
}

if ([string]::IsNullOrWhiteSpace($Otp)) {
    $Otp = Read-RequiredText "Passenger OTP"
}

Invoke-Api `
    -Label "A2. Verify passenger login OTP" `
    -Method "POST" `
    -Path "/auth/login/verify-otp" `
    -Body @{
        email = $Email
        otp   = $Otp
        role  = "passenger"
    } `
    | Out-Null

$loginResponse = Invoke-Api `
    -Label "A3. Login passenger and get token" `
    -Method "POST" `
    -Path "/auth/login" `
    -Body @{
        email  = $Email
        otp    = $Otp
        role   = "passenger"
        device = @{
            device_name   = "PowerShell Passenger Flow Test"
            device_family = "CLI"
            platform      = "Windows PowerShell"
            browser       = $null
        }
    }

if ($null -eq $loginResponse -or $null -eq $loginResponse.PSObject.Properties["access_token"]) {
    throw "Login did not return access_token. Cannot continue."
}

$script:AccessToken = [string]$loginResponse.access_token

Invoke-Api `
    -Label "A4. Auth /me" `
    -Method "GET" `
    -Path "/auth/me" `
    -Auth `
    | Out-Null

$sessionListResponse = Invoke-Api `
    -Label "B1. List booking sessions" `
    -Method "GET" `
    -Path "/passenger/booking-sessions" `
    -Auth

$sessions = Get-ArrayItems $sessionListResponse
$sessions = @(Sort-ByMostRecent -Items $sessions)

if ($sessions.Count -eq 0) {
    throw "No booking sessions found for this passenger."
}

$latestSessionFromList = $sessions[0]
$latestSessionId = [string]$latestSessionFromList.id

Write-Host ""
Write-Host "Most recent booking session selected: $latestSessionId" -ForegroundColor Green

$sessionDetailResponse = Invoke-Api `
    -Label "B2. Latest booking session detail" `
    -Method "GET" `
    -Path "/passenger/booking-sessions/$latestSessionId" `
    -Auth

$session = Get-ResponseObject `
    -Response $sessionDetailResponse `
    -PreferredKeys @("booking_session")

if ($null -eq $session -or $null -eq $session.PSObject.Properties["id"]) {
    throw "Could not read latest booking session detail."
}

$tripId = [string]$session.scheduled_trip_id
$routeId = [string]$session.route_id
$pickupStopId = [string]$session.pickup_stop_id
$dropoffStopId = [string]$session.dropoff_stop_id

if ([string]::IsNullOrWhiteSpace($tripId)) {
    throw "Latest booking session has no scheduled_trip_id."
}

$scheduledTripResponse = Invoke-Api `
    -Label "C1. Scheduled trip detail" `
    -Method "GET" `
    -Path "/passenger/scheduled-trips/$tripId" `
    -Auth

$scheduledTrip = Get-ResponseObject `
    -Response $scheduledTripResponse `
    -PreferredKeys @("scheduled_trip")

Invoke-Api `
    -Label "C2. Scheduled trip driver + vehicle info" `
    -Method "GET" `
    -Path "/passenger/scheduled-trips/$tripId/driver-vehicle-info" `
    -Auth `
    | Out-Null

if (
    -not [string]::IsNullOrWhiteSpace($routeId) -and
    -not [string]::IsNullOrWhiteSpace($pickupStopId) -and
    -not [string]::IsNullOrWhiteSpace($dropoffStopId)
) {
    Invoke-Api `
        -Label "C3. Fare preview for session pickup/dropoff" `
        -Method "POST" `
        -Path "/passenger/fare/preview" `
        -Body @{
            route_id         = $routeId
            pickup_stop_id   = $pickupStopId
            dropoff_stop_id  = $dropoffStopId
        } `
        | Out-Null

    Invoke-Api `
        -Label "C4. Available seats for session trip leg" `
        -Method "POST" `
        -Path "/passenger/scheduled-trips/$tripId/available-seats" `
        -Body @{
            route_id         = $routeId
            pickup_stop_id   = $pickupStopId
            dropoff_stop_id  = $dropoffStopId
            seat_number      = $null
        } `
        -Auth `
        | Out-Null
}

$pickupTripStop = Get-TripStopForSessionStop `
    -ScheduledTrip $scheduledTrip `
    -StopId $pickupStopId `
    -SequenceNo $session.pickup_sequence_no_snapshot

$dropoffTripStop = Get-TripStopForSessionStop `
    -ScheduledTrip $scheduledTrip `
    -StopId $dropoffStopId `
    -SequenceNo $session.dropoff_sequence_no_snapshot

Write-Host ""
Write-Host "================================================================================" -ForegroundColor DarkGray
Write-Host "DERIVED SELECTED TRIP LEG" -ForegroundColor Cyan
Write-Host "================================================================================" -ForegroundColor DarkGray

$derivedLeg = @{
    booking_session_id         = $session.id
    scheduled_trip_id          = $tripId
    pickup_stop_id             = $pickupStopId
    dropoff_stop_id            = $dropoffStopId
    pickup_sequence_no_snapshot = $session.pickup_sequence_no_snapshot
    dropoff_sequence_no_snapshot = $session.dropoff_sequence_no_snapshot
    pickup_stop_name           = if ($null -ne $pickupTripStop -and $null -ne $pickupTripStop.stop) { $pickupTripStop.stop.name } else { $null }
    dropoff_stop_name          = if ($null -ne $dropoffTripStop -and $null -ne $dropoffTripStop.stop) { $dropoffTripStop.stop.name } else { $null }
    pickup_planned_time        = if ($null -ne $pickupTripStop) { $pickupTripStop.planned_time_at_stop } else { $null }
    dropoff_planned_time       = if ($null -ne $dropoffTripStop) { $dropoffTripStop.planned_time_at_stop } else { $null }
}

Write-Host ($derivedLeg | ConvertTo-Json -Depth 100)

$bookings = @()

if ($null -ne $session.PSObject.Properties["bookings"] -and $null -ne $session.bookings) {
    $bookings = @($session.bookings)
}

if ($bookings.Count -eq 0) {
    Write-Host ""
    Write-Host "No bookings/seats found in latest booking session." -ForegroundColor Yellow
}
else {
    $seatIndex = 1

    foreach ($booking in $bookings) {
        $bookingId = [string]$booking.id

        if ([string]::IsNullOrWhiteSpace($bookingId)) {
            continue
        }

        Write-Host ""
        Write-Host "################################################################################" -ForegroundColor Yellow
        Write-Host "SEAT BOOKING $seatIndex / $($bookings.Count)" -ForegroundColor Yellow
        Write-Host "booking_id: $bookingId" -ForegroundColor Yellow
        Write-Host "seat_number: $($booking.seat_number)" -ForegroundColor Yellow
        Write-Host "status: $($booking.booking_status)" -ForegroundColor Yellow
        Write-Host "traveller: $($booking.traveller_name_snapshot)" -ForegroundColor Yellow
        Write-Host "################################################################################" -ForegroundColor Yellow

        Invoke-Api `
            -Label "D$seatIndex.1. Individual booking detail" `
            -Method "GET" `
            -Path "/passenger/bookings/$bookingId" `
            -Auth `
            | Out-Null

        Invoke-Api `
            -Label "D$seatIndex.2. Booking QR for this exact TripBooking" `
            -Method "GET" `
            -Path "/passenger/bookings/$bookingId/qr" `
            -Auth `
            | Out-Null

        Invoke-Api `
            -Label "D$seatIndex.3. Current trip status for this booking" `
            -Method "GET" `
            -Path "/passenger/bookings/$bookingId/current-status" `
            -Auth `
            | Out-Null

        Invoke-Api `
            -Label "D$seatIndex.4. Live location for this booking" `
            -Method "GET" `
            -Path "/passenger/bookings/$bookingId/live-location" `
            -Auth `
            | Out-Null

        Invoke-Api `
            -Label "D$seatIndex.5. Rating for this booking" `
            -Method "GET" `
            -Path "/passenger/bookings/$bookingId/rating" `
            -Auth `
            | Out-Null

        if ($IncludeInvoice) {
            Invoke-Api `
                -Label "D$seatIndex.6. Invoice for this booking" `
                -Method "GET" `
                -Path "/passenger/bookings/$bookingId/invoice" `
                -Auth `
                | Out-Null
        }

        $seatIndex++
    }
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green