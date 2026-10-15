# The office PC's installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Tick each box the moment its step completes, in this file.

**Goal:** `Install-OfficeDbPc.ps1` at ScheduledReportAggregator's root puts `office-db-pc` on the WireGuard tunnel through wireguard-spoke-agent in one administrator run, and the owner's run on the PC passes section 8's checks.

**Architecture:** One self-contained Windows PowerShell 5.1 script, run from `main`'s raw URL with `irm | iex`, built from the WireGuard half of camera-footage-fs's `Install-CffsPc.ps1`: locked folder, WireGuard for Windows, uv and the agent, the private key, `settings.env`, the agent's first run, closing lines. Each step is idempotent, so a failure is fixed by re-running. It never touches MSSQL, the firewall or power settings.

**Tech Stack:** Windows PowerShell 5.1, `icacls`, uv, wireguard-spoke-agent 0.1.2+ from SFTPyPI, WireGuard for Windows.

**Spec:** `docs/office-database-access.md`, section 10 (10.1 decisions, 10.2 running it, 10.3 the steps, 10.4 re-runs and failures, 10.5 verification). Section 8 holds the acceptance checks.

## Global Constraints

- One file, `Install-OfficeDbPc.ps1`, at this repository's root.
- Windows PowerShell 5.1 syntax only: no `??`, `?.`, `? :`, and no `&&` or `||` between commands.
- ASCII only, LF line endings in the repository (`.gitattributes`: `* text=auto eol=lf`).
- Peer `office-db-pc`; home `C:\ProgramData\wireguard-spoke-agent`; index `https://pypi.sweetfiretobacco.com/jacob.ogden/internal/+simple`; WireGuard installer `https://download.wireguard.com/windows-client/wireguard-installer.exe`; uv installer `https://astral.sh/uv/install.ps1`.
- Tunnel only: never touches MSSQL, Windows Firewall or power settings; no subnet check; no version prompt.
- Parameters `-PrivateKeyFile`, `-PingKey`, `-HeartbeatSlug`, `-PushoverToken`, `-PushoverUserKey`, all optional, none ever prompted for (the key file alone is asked for, by picker, when none is installed).
- The script itself is never run anywhere but the office database PC (spec 10.5). A throwaway check may run single steps of it against a scratch folder.
- Commits: Conventional Commits, ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Push every repository touched (ScheduledReportAggregator, wireguard-hub).
- This plan file is deleted once the script has landed and passed acceptance (owner's rule: plans are disposable).

## Review Focus

1. **A re-run on a PC already set up, without `-PrivateKeyFile`**: a person expects no key prompt and the installed key untouched. Pinned in Task 1, Step 4 (case 4b) and Task 2, Step 4.
2. **`-PrivateKeyFile` naming the installed key itself**: a person expects the key to survive. `Copy-Item` refuses to copy a file onto itself, so the run stops before the delete. Pinned in Task 1, Step 4 (case 4c).
3. **An existing `settings.env` with a BOM, a comment, a blank line, and a value containing `=`** (a ping URL with a query string): a person expects every value kept intact and the file written back without a BOM, CRLF. Pinned in Task 1, Step 4 (case 5a).
4. **A monitoring parameter given on a re-run**: a person expects it to override the stored value, and parameters not given to leave stored values alone. Pinned in Task 1, Step 4 (cases 5a and 5b).
5. **The hub unreachable, or the PC not enrolled, during the first run**: a person expects the run to stop naming `wireguard-spoke-agent install`, with the agent's own error printed above it, and a later re-run to finish the job. Pinned in Task 1, Step 5 (the comparison checklist), since the agent's run cannot be exercised off the PC.

---

### Task 1: The script, checked, with the docs that follow it

**Files:**
- Create: `Install-OfficeDbPc.ps1`
- Modify: `docs/office-database-access.md` (section 10's status line)
- Modify: `../wireguard-hub/docs/superpowers/specs/2026-09-14-hub-fetched-peer-config-design.md` (10.2's `office-db-pc` line)
- Throwaway, never committed: `<scratchpad>/check_installer_blocks.ps1`

**Interfaces:**
- Consumes: wireguard-spoke-agent's `wireguard-spoke-agent install` subcommand (registers its task and runs once; exits non-zero on failure), its `settings.env` keys `WG_PEER_NAME`, `PERSISTED_DIR_LOC`, `PINGKEY`, `HEARTBEAT_SLUG`, `ALERTS_PUSHOVER_TOKEN`, `ALERTS_PUSHOVER_USER_KEY`, and the placeholders `ALERTS_EMAIL_PWD=unused`, `ALERTS_RECIPIENTS=[]`.
- Produces: the raw URL `https://raw.githubusercontent.com/AetherBreaker/ScheduledReportAggregator/main/Install-OfficeDbPc.ps1`, used by Task 2. Each step of the script opens with a marker line `# ---- <n>. <name> ----`; the throwaway check in Step 4 finds steps 4 and 5 by those markers.

- [x] **Step 1: Write `Install-OfficeDbPc.ps1`**

Write exactly this, LF line endings, ASCII only:

```powershell
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
  if (-not (Test-Path $uv)) { Invoke-RestMethod 'https://astral.sh/uv/install.ps1' | Invoke-Expression }
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
```

- [x] **Step 2: Parse it with PowerShell's own parser**

Parsing only; nothing is executed. Run in Windows PowerShell from the repository root:

```powershell
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path .\Install-OfficeDbPc.ps1).Path, [ref]$null, [ref]$errors) | Out-Null
if ($errors) { $errors | ForEach-Object { "$($_.Extent.StartLineNumber): $($_.Message)" } } else { 'no parse errors' }
```

Expected: `no parse errors`.

- [x] **Step 3: Check the encoding and line endings**

Run in Git Bash from the repository root:

```bash
grep -cP '[^\x00-\x7F]' Install-OfficeDbPc.ps1; grep -c $'\r' Install-OfficeDbPc.ps1
```

Expected: `0` and `0` (ASCII only, LF only).

- [x] **Step 4: Run steps 4 and 5 of the script against a scratch folder**

Write this throwaway file to the session scratchpad as `check_installer_blocks.ps1`. It cuts steps 4 and 5 out of the real script by their markers and runs them with `$AgentHome` pointing into the scratch folder, so it tests the shipped text and touches nothing outside the scratch folder. It never runs case "no parameter, no installed key", which would open a file picker.

```powershell
# Throwaway: run steps 4 and 5 of Install-OfficeDbPc.ps1, as written, against a scratch folder.
param([string]$Script, [string]$Scratch)
$ErrorActionPreference = 'Stop'
$source = Get-Content -Raw $Script
$step4 = [scriptblock]::Create([regex]::Match($source, '(?s)# ---- 4\. The private key ----(.*?)# ---- 5\.').Groups[1].Value)
$step5 = [scriptblock]::Create([regex]::Match($source, '(?s)# ---- 5\. settings\.env ----(.*?)# ---- 6\.').Groups[1].Value)
$script:bad = 0
function Check([string]$name, [bool]$ok) { if (-not $ok) { $script:bad++ }; '{0} {1}' -f $(if ($ok) { 'ok ' } else { 'BAD' }), $name }

$AgentHome = Join-Path $Scratch 'home'
if (Test-Path $AgentHome) { Remove-Item -Recurse -Force $AgentHome }
New-Item -ItemType Directory $AgentHome | Out-Null
$Peer = 'office-db-pc'
$key = Join-Path $AgentHome 'office-db-pc.key'

# 4a: a given key is copied in, its source deleted.
$PrivateKeyFile = Join-Path $Scratch 'given.key'
Set-Content $PrivateKeyFile 'K1'
. $step4
Check '4a key installed from -PrivateKeyFile' ((Get-Content $key) -eq 'K1')
Check '4a source deleted' (-not (Test-Path (Join-Path $Scratch 'given.key')))

# 4b: no parameter, key installed: nothing happens, no picker.
$PrivateKeyFile = ''
. $step4
Check '4b installed key untouched' ((Get-Content $key) -eq 'K1')

# 4c: -PrivateKeyFile names the installed key itself: the run stops, the key survives.
$PrivateKeyFile = $key
$threw = $false
try { . $step4 } catch { $threw = $true }
Check '4c run stops' $threw
Check '4c installed key survives' ((Test-Path $key) -and ((Get-Content $key) -eq 'K1'))

# 5a: an existing file with a BOM, a comment, a blank line and a value containing '='.
$settingsFile = Join-Path $AgentHome 'settings.env'
$old = "# note`r`nWG_PEER_NAME=old`r`nPINGKEY=abc`r`nALERTS_HEALTHCHECK_PING_URL=https://hc.example/ping?a=b`r`n`r`n"
[IO.File]::WriteAllText($settingsFile, $old, [Text.UTF8Encoding]::new($true))
$PingKey = ''; $HeartbeatSlug = ''; $PushoverToken = ''; $PushoverUserKey = ''
. $step5
$bytes = [IO.File]::ReadAllBytes($settingsFile)
$lines = [IO.File]::ReadAllText($settingsFile) -split "`r`n"
Check '5a no BOM written' (-not ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF))
Check '5a CRLF endings' ([IO.File]::ReadAllText($settingsFile).EndsWith("`r`n") -and -not ([IO.File]::ReadAllText($settingsFile) -match "[^`r]`n"))
Check '5a peer name set' ($lines -contains 'WG_PEER_NAME=office-db-pc')
Check '5a PERSISTED_DIR_LOC set' ($lines -contains "PERSISTED_DIR_LOC=$AgentHome")
Check '5a mail placeholders set' (($lines -contains 'ALERTS_EMAIL_PWD=unused') -and ($lines -contains 'ALERTS_RECIPIENTS=[]'))
Check '5a stored PINGKEY kept' ($lines -contains 'PINGKEY=abc')
Check "5a value with '=' kept whole" ($lines -contains 'ALERTS_HEALTHCHECK_PING_URL=https://hc.example/ping?a=b')

# 5b: a monitoring parameter on a re-run overrides; parameters not given leave stored values.
$PingKey = 'new'
. $step5
$lines = [IO.File]::ReadAllText($settingsFile) -split "`r`n"
Check '5b given PINGKEY overrides' ($lines -contains 'PINGKEY=new')
Check '5b ping URL still kept' ($lines -contains 'ALERTS_HEALTHCHECK_PING_URL=https://hc.example/ping?a=b')

"$script:bad wrong"
```

Run it from the repository root (the script only reads `Install-OfficeDbPc.ps1`; every write goes under the scratch folder):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <scratchpad>\check_installer_blocks.ps1 -Script (Resolve-Path .\Install-OfficeDbPc.ps1).Path -Scratch <scratchpad>\installer-check
```

Expected: every line starts `ok`, the last line is `0 wrong`. The `Write-Host` lines of the steps print between them. A `BAD` line is a defect in the script: fix the script, then re-run Steps 2 to 4.

- [x] **Step 5: Compare with `Install-CffsPc.ps1`**

Run in Git Bash from the repository root:

```bash
git diff --no-index ../camera-footage-fs/Install-CffsPc.ps1 Install-OfficeDbPc.ps1
```

Expected differences, and nothing else:

- The help block and `param(...)`: no `-Version` and no `-AdminRemoteAddress`.
- The constants: `$Peer = 'office-db-pc'`; no `$Repo`, `$Ingress`, `$Group`, `$SftpgoDir`.
- Removed: the version prompt, the whole SFTPGo section with its firewall rules and restart, the `powercfg` call, and the admin-URL listing at the end.
- cffs's one `# ---- 2. WireGuard, through wireguard-spoke-agent ----` section split into the markers `# ---- 1.` to `# ---- 7.`; the comment above `icacls` (cffs: "Same lock-down SFTPGo's installer applies ...") and the one opening `settings.env` (cffs: "settings.env: keep existing values ...") reworded to fit.
- One added comment line in step 4 about naming the installed key itself; one added comment line before the agent's run.
- New closing lines (step 7).

Then confirm by reading the new file, each item a Review Focus line or a Global Constraint:

- The administrator check is the first statement after `$ErrorActionPreference` and the TLS line, before any change (Global Constraints).
- `Assert-Exit` follows `icacls`, `uv tool install` and the agent's `install`; the agent's `install` is the last native command, so a hub that is unreachable, or a PC not enrolled, stops the run there with `wireguard-spoke-agent install failed with exit code 1`, the agent's own error printed above it, and nothing after it needs undoing (Review Focus 5).
- No `powercfg`, `New-NetFirewallRule`, `Set-NetFirewall*`, `sqlcmd`, or `Read-Host` other than the key path's (Global Constraints).

- [x] **Step 6: Update the docs that follow the build**

In `docs/office-database-access.md`, section 10's first paragraph, replace:

```markdown
Designed with the owner on 2026-10-14 and 2026-10-15. Status: designed, not built. The script is
`Install-OfficeDbPc.ps1` at this repository's root. This section is its design and its manual.
```

with:

```markdown
Designed with the owner on 2026-10-14 and 2026-10-15. Status: built, not yet run on the PC. The
script is `Install-OfficeDbPc.ps1` at this repository's root. This section is its design and its
manual.
```

In `../wireguard-hub/docs/superpowers/specs/2026-09-14-hub-fetched-peer-config-design.md`, section 10.2, replace:

```markdown
- `office-db-pc` (`10.8.0.10`): in design; see ScheduledReportAggregator's document (10.4).
```

with:

```markdown
- `office-db-pc` (`10.8.0.10`): ScheduledReportAggregator's `Install-OfficeDbPc.ps1` (10.4); not
  run on the PC yet.
```

- [ ] **Step 7: Commit and push both repositories**

```bash
git add Install-OfficeDbPc.ps1 docs/office-database-access.md docs/superpowers/plans/2026-10-15-office-db-pc-installer.md
git commit -m "feat(wireguard): add the office PC's installer" -m "Install-OfficeDbPc.ps1 puts office-db-pc on the tunnel through wireguard-spoke-agent in one administrator run: the locked folder, WireGuard for Windows, uv and the agent, the private key, settings.env, the agent's first run. Built from Install-CffsPc.ps1's WireGuard half; tunnel only (docs/office-database-access.md, section 10)." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push
cd ../wireguard-hub
git add docs/superpowers/specs/2026-09-14-hub-fetched-peer-config-design.md
git commit -m "docs(specs): office-db-pc's installer is ScheduledReportAggregator's Install-OfficeDbPc.ps1" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push
```

Expected: both pushes succeed; `git status` in each repository shows it up to date with `origin/main`.

- [ ] **Step 8: Confirm the raw URL serves the new file**

GitHub caches raw URLs for about five minutes. Run until it prints the script's first lines, at most every minute:

```bash
curl -s https://raw.githubusercontent.com/AetherBreaker/ScheduledReportAggregator/main/Install-OfficeDbPc.ps1 | head -3
```

Expected: `<#`, `.SYNOPSIS`, `  Sets up office-db-pc's WireGuard tunnel ...`. Only then is Task 2 handed to the owner.

---

### Task 2: Acceptance on the PC, and closing out

The owner runs Steps 1 to 5 on the PC and the VPS and pastes the output here; the executor reads each paste against the expected result before ticking it, then does Steps 6 to 9.

**Files:**
- Modify: `docs/office-database-access.md` (4.1, 8, 10)
- Modify: `../wireguard-hub/docs/superpowers/specs/2026-09-14-hub-fetched-peer-config-design.md` (10.2, 10.4, 14)
- Delete: `docs/superpowers/plans/2026-10-15-office-db-pc-installer.md`

**Interfaces:**
- Consumes: the raw URL from Task 1, Step 8; `office-db-pc.key` from the maintainer's copy (ScheduledReportAggregator's root or beside the `wireguard-hub` checkout, identical).
- Produces: section 8's checks (16.4), the reboot check and (16.5) ticked; the PC recorded as installed.

- [ ] **Step 1 (owner): Put the key on the PC**

Copy `office-db-pc.key` to the PC's desktop with RustDesk's file transfer.

- [ ] **Step 2 (owner): Run the installer, paste the console output**

In an administrator Windows PowerShell on the PC:

```powershell
irm https://raw.githubusercontent.com/AetherBreaker/ScheduledReportAggregator/main/Install-OfficeDbPc.ps1 | iex
```

Pick the key on the desktop in the file picker. Expected in the output, in order: `Installed WireGuard for Windows`; uv's install lines; `Installed C:\ProgramData\wireguard-spoke-agent\office-db-pc.key and deleted ...`; `Wrote C:\ProgramData\wireguard-spoke-agent\settings.env`; the agent's lines including `installed tunnel service WireGuardTunnel$office-db-pc`, `hub v... applied (conf changed, tunnel reinstalled)`, `handshake fresh` and `registered scheduled task wireguard-spoke-agent`; then the closing lines. `handshake stale` instead of `fresh` is not a failure (spec 10.4): carry on to Step 3, which decides.

- [ ] **Step 3 (owner): Check (16.4) from both ends, paste both outputs**

On the PC: `ping 10.8.0.1`. Expected: replies.

On the VPS:

```bash
docker exec $(docker ps -qf name=wireguard-hub) wg show wg0 latest-handshakes
```

Expected: a line for `ikf93PzvBWSGpfQvZrW+sa0B552/ee3LmZHA/B95Jx0=` (the PC) with a Unix timestamp under 180 s old (`date +%s` on the VPS gives now). The same output settles (16.5): the line for `X3KOCWL9jdgVQthNUxlGqCk6ujaMDW37xQ34w2TOeCw=` (ScheduledReportAggregator) under 180 s old means connected.

- [ ] **Step 4 (owner): Re-run without parameters, paste the output**

Run the same one-liner again. Expected (Review Focus 1): no file picker and no key prompt; no `Installed ... key` line; `Wrote ...settings.env`; the agent's run logs `handshake fresh` and no `applied (conf changed ...)` line. Then `Get-Content C:\ProgramData\wireguard-spoke-agent\settings.env` shows exactly `WG_PEER_NAME=office-db-pc`, `PERSISTED_DIR_LOC=C:\ProgramData\wireguard-spoke-agent`, `ALERTS_EMAIL_PWD=unused`, `ALERTS_RECIPIENTS=[]`.

- [ ] **Step 5 (owner): The reboot check, paste the VPS output**

Reboot the PC; without logging in at it, run the VPS command of Step 3 a minute or two after it is back. Expected: the PC's line has a fresh timestamp (the tunnel service starts at boot, before anyone logs in).

- [ ] **Step 6: Record the results in ScheduledReportAggregator's document**

In `docs/office-database-access.md`:

- 4.1: replace the bullet `- Not set up yet: nothing is installed on the PC and it has never handshaken with the hub (owner, 2026-10-05).` (it wraps across two lines) with `- Installed with \`Install-OfficeDbPc.ps1\` on <date of Step 2>; first handshake with the hub the same day.`
- 8: tick `(16.4)` and the reboot check; tick `(16.5)` only if Step 3 showed ScheduledReportAggregator's line under 180 s old, otherwise leave it and add one line under it saying what Step 3 showed.
- 10: replace `Status: built, not yet run on the PC.` with `Status: built, and run on the PC on <date of Step 2>; accepted.`

- [ ] **Step 7: Record the results in the hub design**

In `../wireguard-hub/docs/superpowers/specs/2026-09-14-hub-fetched-peer-config-design.md`:

- 10.2: replace the `office-db-pc` bullet's `; not\n  run on the PC yet.` ending with `; run on the PC on <date of Step 2>.`
- 10.4: replace `It is a Windows spoke (10.2) and has\nnot been installed yet.` with `It is a Windows spoke (10.2), installed on <date of Step 2>.`
- 14: tick release-order step 4, and append to the 2026-10-14 status paragraph: `The office PC was installed on <date of Step 2> with ScheduledReportAggregator's \`Install-OfficeDbPc.ps1\` and handshakes with the hub; step 4 is done.`

- [ ] **Step 8: Delete this plan, commit and push both repositories**

```bash
git rm docs/superpowers/plans/2026-10-15-office-db-pc-installer.md
git add docs/office-database-access.md
git commit -m "docs(wireguard): the office PC is on the tunnel; the installer plan is done" -m "Accepted on the PC: check (16.4) from both ends, the reboot check, and (16.5) as recorded in section 8. The plan is deleted now its work has landed." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push
cd ../wireguard-hub
git add docs/superpowers/specs/2026-09-14-hub-fetched-peer-config-design.md
git commit -m "docs(specs): office-db-pc is installed; release-order step 4 is done" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push
```

Expected: both pushes succeed and both repositories are up to date with `origin/main`.

- [ ] **Step 9: Update the WireGuard memory**

In the memory file `project_wireguard_do_over.md`, replace `office-db-pc enrolled at 10.8.0.10 but never installed;` and `the DB PC's installer is the next project.` with the installation date and `Install-OfficeDbPc.ps1` as the way it was installed.
