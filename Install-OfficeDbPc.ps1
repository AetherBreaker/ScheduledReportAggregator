<#
.SYNOPSIS
  Sets up office-db-pc's WireGuard tunnel through wireguard-spoke-agent. Tunnel only: it never
  touches MSSQL, the firewall or power settings. Idempotent; safe to re-run.

.DESCRIPTION
  Run from an administrator Windows PowerShell on the office database PC:

    irm https://raw.githubusercontent.com/AetherBreaker/ScheduledReportAggregator/main/Install-OfficeDbPc.ps1 | iex

  or, with parameters:

    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/AetherBreaker/ScheduledReportAggregator/main/Install-OfficeDbPc.ps1))) -PrivateKeyFile C:\path\office-db-pc.key

  Creates C:\ProgramData\wireguard-spoke-agent (SYSTEM and Administrators only), installs
  WireGuard for Windows if missing, uv, and the agent; copies the private key in (deleting the
  source) and writes settings.env; then runs `wireguard-spoke-agent install`, which brings the
  tunnel up and registers the agent's scheduled task. -PingKey, -HeartbeatSlug, -PushoverToken
  and -PushoverUserKey turn the agent's alerts on; existing settings.env values are kept unless a
  parameter overrides them. Design and manual: docs/office-database-access.md, section 10.
#>
param(
  [string]$PrivateKeyFile,
  [string]$PingKey,
  [string]$HeartbeatSlug,
  [string]$PushoverToken,
  [string]$PushoverUserKey
)

# A script block, so nothing but the parameters lands in the caller's session under `iex`;
# `return`/`throw`, never `exit`, which would close it.
& {
  $ErrorActionPreference = 'Stop'
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

  $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this from an administrator PowerShell.'  # #Requires is ignored under iex
  }

  $Peer = 'office-db-pc'
  $AgentHome = Join-Path $env:ProgramData 'wireguard-spoke-agent'
  $AgentIndex = 'https://pypi.sweetfiretobacco.com/jacob.ogden/internal/+simple'
  $WireGuardExe = Join-Path $env:ProgramFiles 'WireGuard\wireguard.exe'

  function Assert-Exit([string]$what) {
    # Native commands don't throw in Windows PowerShell; check each one that matters.
    if ($LASTEXITCODE -ne 0) { throw "$what failed with exit code $LASTEXITCODE" }
  }

  # ---- 1. The locked folder ----
  New-Item -ItemType Directory -Force (Join-Path $AgentHome 'logs') | Out-Null
  # SYSTEM and Administrators only, inherited inside, nothing inherited from above.
  icacls.exe $AgentHome /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
  Assert-Exit 'icacls'

  # ---- 2. WireGuard for Windows ----
  if (-not (Test-Path $WireGuardExe)) {
    $wgInstaller = Join-Path $env:TEMP 'wireguard-installer.exe'
    Invoke-WebRequest -UseBasicParsing 'https://download.wireguard.com/windows-client/wireguard-installer.exe' -OutFile $wgInstaller
    $sig = Get-AuthenticodeSignature $wgInstaller
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'WireGuard LLC') {
      throw "The downloaded WireGuard installer's signature is not a valid WireGuard LLC one ($($sig.Status))."
    }
    Start-Process -FilePath $wgInstaller -Wait
    if (-not (Test-Path $WireGuardExe)) { throw 'WireGuard for Windows did not install.' }
    Write-Host 'Installed WireGuard for Windows'
  }

  # ---- 3. uv and the agent ----
  # Every uv path inside the locked folder, matching the agent's run.cmd.
  $env:UV_INSTALL_DIR = Join-Path $AgentHome 'uv'
  $env:UV_NO_MODIFY_PATH = '1'
  $env:UV_PYTHON_INSTALL_DIR = Join-Path $AgentHome 'python'
  $env:UV_TOOL_DIR = Join-Path $AgentHome 'tools'
  $env:UV_TOOL_BIN_DIR = Join-Path $AgentHome 'bin'
  $env:UV_CACHE_DIR = Join-Path $AgentHome 'cache'
  $env:UV_MANAGED_PYTHON = '1'
  $uv = Join-Path $env:UV_INSTALL_DIR 'uv.exe'
  if (-not (Test-Path $uv)) {
    # In a child process, as astral documents: its installer refuses a Restricted execution policy
    # (Windows' default), and its `exit` would close this window if it ran here under iex. The
    # child inherits the UV_* variables; Bypass lasts only for that process.
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
    Assert-Exit 'the uv installer'
  }
  & $uv tool install --upgrade --index $AgentIndex wireguard-spoke-agent
  Assert-Exit 'uv tool install wireguard-spoke-agent'

  # ---- 4. The private key ----
  $keyTarget = Join-Path $AgentHome "$Peer.key"
  if (-not $PrivateKeyFile -and -not (Test-Path $keyTarget)) {
    Write-Host "Select $Peer.key in the file picker"
    try {
      Add-Type -AssemblyName System.Windows.Forms
      $dialog = [Windows.Forms.OpenFileDialog]@{ Title = "Select $Peer.key"; Filter = 'WireGuard key (*.key)|*.key|All files (*.*)|*.*' }
      # A topmost owner keeps the picker from opening behind the console.
      $owner = [Windows.Forms.Form]@{ TopMost = $true }
      if ($dialog.ShowDialog($owner) -eq 'OK') { $PrivateKeyFile = $dialog.FileName }
      $owner.Dispose()
    } catch {
      # No desktop session, or an MTA host (PowerShell 7) where the dialog can't open.
      Write-Warning "The file picker could not open: $($_.Exception.Message)"
    }
    if (-not $PrivateKeyFile) { $PrivateKeyFile = Read-Host "Path to $Peer.key" }
    if (-not $PrivateKeyFile) { throw "No $Peer.key given." }
  }
  if ($PrivateKeyFile) {
    # Copy then delete, not move: a moved file keeps its old ACL instead of the locked folder's.
    # Naming the installed key itself stops here, before the delete: Copy-Item refuses a file onto itself.
    Copy-Item $PrivateKeyFile $keyTarget -Force
    Remove-Item $PrivateKeyFile -Force
    # The agent refills its conf only on a new hub tag; forgetting the applied one makes its next
    # run refill the conf with this key and reinstall the tunnel.
    Remove-Item (Join-Path $AgentHome 'state.json') -Force -ErrorAction SilentlyContinue
    Write-Host "Installed $keyTarget and deleted $PrivateKeyFile"
  }

  # ---- 5. settings.env ----
  # Keep existing values, override with any given, always set the fixed ones.
  $settingsFile = Join-Path $AgentHome 'settings.env'
  $settings = [ordered]@{}
  if (Test-Path $settingsFile) {
    foreach ($line in Get-Content $settingsFile) {
      if ($line.Trim() -and -not $line.TrimStart().StartsWith('#')) {
        $k, $v = $line -split '=', 2
        $settings[$k.Trim()] = $v.Trim()
      }
    }
  }
  $settings['WG_PEER_NAME'] = $Peer
  $settings['PERSISTED_DIR_LOC'] = $AgentHome
  # Until aeth-ext treats a missing password as "email off": its settings require one, and
  # ALERTS_RECIPIENTS=[] keeps it from ever logging in to the real mailbox with the placeholder.
  $settings['ALERTS_EMAIL_PWD'] = 'unused'
  $settings['ALERTS_RECIPIENTS'] = '[]'
  if ($PingKey) { $settings['PINGKEY'] = $PingKey }
  if ($HeartbeatSlug) { $settings['HEARTBEAT_SLUG'] = $HeartbeatSlug }
  if ($PushoverToken) { $settings['ALERTS_PUSHOVER_TOKEN'] = $PushoverToken }
  if ($PushoverUserKey) { $settings['ALERTS_PUSHOVER_USER_KEY'] = $PushoverUserKey }
  $text = ($settings.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "`r`n"
  [IO.File]::WriteAllText($settingsFile, "$text`r`n", [Text.UTF8Encoding]::new($false))
  Write-Host "Wrote $settingsFile"

  # ---- 6. The agent's first run ----
  # Registers the agent's scheduled task and brings the tunnel up; its log lines print here.
  & (Join-Path $env:UV_TOOL_BIN_DIR 'wireguard-spoke-agent.exe') install
  Assert-Exit 'wireguard-spoke-agent install'

  # ---- 7. Closing lines ----
  Write-Host ''
  Write-Host "Done. The agent's log is $(Join-Path $AgentHome 'logs\agent.log')."
  Write-Host "Confirm from the hub's side, on the VPS: this PC's public key should show a handshake under 180 s old."
  Write-Host '  docker exec $(docker ps -qf name=wireguard-hub) wg show wg0 latest-handshakes'
}
