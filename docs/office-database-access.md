# Reaching the office MSSQL database

Status: 2026-10-14. ScheduledReportAggregator's own document for reaching the office database over
the WireGuard tunnel: the path, the constraints, the office PC, the decisions taken and the ones
still open, and the checks. The tunnel's overall design (the hub, the bundle, container spokes and
Windows spokes) is `docs/superpowers/specs/2026-09-14-hub-fetched-peer-config-design.md` in
`wireguard-hub`, called "the hub design" below; this document does not repeat it.

It replaces `docs/superpowers/specs/2026-09-15-office-db-pc-peer-design.md` and the parts of the hub
design specific to this project and the office PC, both moved here on 2026-10-14 (owner): the hub
design holds the overall design, and each project holds what is specific to it.

A decision recorded here changes only by the owner's explicit decision, written here first. A point
this document does not decide is asked, not inferred.

## 1. The path

```text
 ScheduledReportAggregator        wireguard-hub                 office-db-pc
 Coolify container, VPS           Coolify container, VPS        Windows 11, office LAN
+------------------------+      +-----------------------+      +---------------------------+
| wg0 10.8.0.20          | UDP  | wg0 10.8.0.1          | UDP  | WireGuard for Windows     |
| devkit-container,      |----->| FORWARD DROP, plus    |<-----|  tunnel 10.8.0.10         |
|  fetched mode          |51820 |  one ACCEPT per flow  |51820 | Windows Firewall: the DB  |
| app: TCP to 10.8.0.10  |      |  (rules.v4)           |      |  port from approved /32s  |
+------------------------+      +-----------------------+      | MSSQL, a Windows service  |
                                                               +---------------------------+
```

- Both spokes dial the hub's public name, `tunnels.sweetfiretobacco.com:51820`. The office needs
  outbound UDP only: no router port forward, no static address. ScheduledReportAggregator reaches
  the public name from inside the VPS through the Docker hairpin (hub design 16).
- No DNS inside the tunnel: the database is addressed as `10.8.0.10`.
- The tunnel address lands on the Windows host itself, so MSSQL and its firewall see each caller's
  own tunnel address. That is why Docker Desktop on the PC was ruled out: behind its VM every
  caller would look like the VM.

## 2. Constraints

All the owner's.

- In-house end to end: no third-party overlay or tunnel service in the data path (hub design 12).
- The database must not be reachable by any other container on the `coolify` network.
- Every real user of the database has its own key pair and tunnel address, so activity is
  attributable; no shared forwarder between apps.
- A read-only database account for the app.
- Private keys only in Coolify secrets or gitignored local files.
- No Docker Desktop on the office PC (2026-09-15).
- Nothing from the earlier attempt is an input: not its folders, not its keys, and not anything
  inferred from it, such as port 1433 (2026-09-14).

## 3. Security layers

In order from the caller:

1. **Cryptokey routing at the hub.** Only enrolled public keys are accepted, each bound to its
   `/32`; a spoke cannot send from an address it was not given.
2. **The hub's `FORWARD` chain.** Policy `DROP`; replies accepted; one `ACCEPT` line per approved
   flow from a spoke's `/32` to `10.8.0.10` on the database port (7.5). Every accepted line is
   `wg0` to `wg0`, so traffic entering the hub any other way, including from a container on the
   `coolify` network, matches nothing and is dropped; a tunnel peer without a flow line is dropped
   too.
3. **The office PC's Windows Firewall.** The database port only from approved tunnel addresses,
   on all profiles, because Windows classes the tunnel interface Public (7.6).
4. **The database login.** A read-only SQL login, its password a Coolify secret (7.2).

## 4. The office PC

### 4.1 Facts

- Windows 11. MSSQL runs natively as a Windows service. Its edition and instance shape are not
  yet known.
- Headless and unattended, reached over RustDesk. RustDesk runs as a service, so a session works
  at the login screen and survives a reboot.
- Tailscale is installed, and MSSQL is reachable today over the LAN and over Tailscale. Tailscale
  (`100.64.0.0/10`) and WireGuard for Windows coexist: different drivers, different subnets. The
  tunnel's `AllowedIPs` cover only `10.8.0.0/24`, so the LAN, Tailscale and the RustDesk session
  are untouched when it comes up.
- Enrolled as `office-db-pc` at `10.8.0.10/32` in wireguard-hub 1.5.0 (2026-09-15), public key
  `ikf93PzvBWSGpfQvZrW+sa0B552/ee3LmZHA/B95Jx0=`. The maintainer generated its key pair at
  enrolment. The private key `office-db-pc.key` and the completed `office-db-pc.conf` are
  gitignored files, with identical copies in this repository's root and beside the
  `wireguard-hub` checkout.
- Not set up yet: nothing is installed on the PC and it has never handshaken with the hub
  (owner, 2026-10-05).

### 4.2 Considerations for anything installed on it

Written 2026-09-15, corrected 2026-10-14.

- **Windows classifies the tunnel interface as a Public network.** A firewall rule written for
  the Domain or Private profile does not apply to it. Rules must cover all profiles or name the
  interface.
- **MSSQL's edition and instance shape are unknown.** Express and named instances ship with TCP
  off and a dynamic port found through SQL Browser. The tunnel wants one static port (7.1).
- **A query-based check needs credentials.** From the hub or a spoke that is a SQL login stored as
  a secret. An agent on the PC can use Windows integrated authentication as the account it runs
  under, with no password on disk.
- **"Offline" has three causes and one look.** PC off, WireGuard service stopped, and health agent
  dead all present as silence on healthchecks.io. Two independent signals, one from the PC and one
  from the hub, separate them.
- **Planned reboots page whoever holds the alerts.** Windows Update restarts the PC. A Windows
  service receives a pre-shutdown notice and could send a pause ping; a scheduled task gets none.
  A grace period of ten minutes or so covers a reboot either way.
- **Anything installed on the PC is new surface.** An unsigned exe trips SmartScreen and antivirus,
  and a signing certificate costs yearly. A Python tool through uv avoids that but puts uv on the
  PC. SFTPyPI's index serves `wireguard-spoke-agent` without credentials (checked 2026-10-14), so
  installing it leaves no index credential on the PC.
- **The office side of the path is untested until the first handshake (8):** outbound UDP from
  the LAN, the router's NAT timeout against the 25 s keepalive, and the PC's sleep settings.
- **The private key at rest.** Imported through the WireGuard app, the key is stored
  DPAPI-encrypted, readable by administrators. Through `wireguard-spoke-agent` (5.3), the key and
  the filled conf are plain files in `C:\ProgramData\wireguard-spoke-agent\`, whose ACL admits
  SYSTEM and Administrators only, and the tunnel service is installed from that conf. The same
  accounts can read the key either way; the agent needs it readable to refill the conf after a
  hub release. The PC is headless, so the key crosses the RustDesk session once, from the
  maintainer's copy.

## 5. Decision record: how the office PC joins the tunnel

### 5.1 The question (2026-09-15)

The hub design said "not a container: WireGuard for Windows, the conf asset from the hub's
release". The owner wanted, beyond the tunnel, a continuous health signal for the tunnel, for the
PC being online, and for the MSSQL service, and asked how that changes the choice. Two facts fixed
the analysis: the database is native, so the tunnel address must land on the Windows host (1); and
the PC may sit at the login screen after a reboot, which the owner can change, so it is a cost, not
a blocker.

### 5.2 The options and their cost (2026-09-15)

Build is agent session effort including tests, docs and releases. Owner is hands-on time to
install, review and verify. Yearly is routine upkeep, excluding incident response. Plus or minus
half.

| Option | Build (h) | Owner (h) | Yearly (h) | Reports |
| --- | --- | --- | --- | --- |
| A. WireGuard for Windows only | 0.5 | 1 | 1 | Nothing; failures surface through the aggregator's jobs |
| D. The hub probes every peer | 4 to 6 | 0.5 | 0.5 | Tunnel up and PC online, every peer, present and future |
| B. Python scheduled-task agent on the PC | 12 to 20 | 1.5 | 2 to 4 | Tunnel, MSSQL service, MSSQL query by integrated auth, config self-update |
| C. Rust Windows-service agent on the PC | 25 to 40 | 1.5 | 2 to 3 | As B, plus reboot-aware pings and Event Log presence |
| E. A Linux node on the office LAN as a devkit spoke | 12 to 20 | 4 to 6 | 4 to 8 | The tunnel heartbeat as designed, MSSQL port reachable over the LAN, no integrated auth |

D combines with any other option at its own cost. Firewall automation from the hub's bundle, which
only a PC-side agent can do, is 6 to 10 more build hours on B or C. A self-updating exe for C is 4
to 8 more.

- **A** is a peer row, a hub release, and an import on the PC; yearly, client updates and a
  re-import when the hub section or the PC's row changes.
- **D** is a probe loop in the hub app pinging each peer's tunnel address, unprivileged ICMP via
  one more sysctl in the hub's compose file, one healthchecks.io check per peer, and a ruling
  reversing hub design 3.5 ("the hub does not observe peers").
- **B** is a new Windows-targeted repository with no Docker: health pings to healthchecks.io, and
  an updater that polls the version endpoint, downloads the PC's conf asset on a new tag and
  re-imports the tunnel. Testable end to end on the owner's Windows dev machine.
- **C** costs twice B for the same coverage: Windows service plumbing, a Windows CI job to build
  and attach the exe, and, for the devkit-container reuse to be real, extracting its fetch and
  bundle code into a library first. SmartScreen friction on every update.
- **E** spends its hours on routing. A container on a Docker bridge masquerades what it forwards,
  so the PC would see the node's address, not the callers'. Keeping tunnel addresses means host
  networking and a static route on the PC, neither rendered by the compose template, and deploys
  by hand or through Coolify's remote-server support, which needs the tunnel first. It is the only
  option that keeps containers end to end and the only one that can later expose other office
  machines.

### 5.3 The ruling (owner, 2026-09-15), and what followed

The ruling: A now. Then a simple scheduled-task script on the PC for config automation alone,
re-importing the conf asset when the hub's tag changes and doing nothing else. Then D, the hub
probing every peer, as the cheap universal signal. Then B for what only the PC can see: MSSQL
health with integrated auth and, later, hub-driven firewall rules. C and E stay as deliberate
upgrades, not starting points: C if the PC's agent should become one exe with no runtime, E if the
office should get a proper devkit node. Each later step gets its own ruling before it is built.

What followed:

- **A was never run.**
- **The scheduled-task step became `wireguard-spoke-agent`**, built for every Windows spoke, not
  for this PC alone (designed with the owner 2026-10-05; 0.1.1 on SFTPyPI). It goes beyond the
  ruling's "re-import and nothing else": it also keeps the tunnel service installed and running,
  sends healthchecks.io a success or fail ping on handshake freshness when a ping key is set (B's
  tunnel signal, not its MSSQL one), upgrades itself, and keeps its scheduled task current. Its
  behaviour is in the hub design (10.2) and the agent's README. It ships no installer: each spoke
  installs it its own way, and the office PC's installer was left for later (owner, 2026-10-05).
  It has run on `cffs-pc` since 2026-10-06, installed by camera-footage-fs's `Install-CffsPc.ps1`.
- **Windows spokes are settled on the agent** (owner, 2026-10-14). The office PC's installer,
  which puts the agent on it, is the next piece of work and gets its own design.
- **D, B, C and E stand as the upgrade path**, each with its own ruling before it is built. D
  changes the hub, so its ruling is recorded in the hub design; B, C and E concern this PC, so
  theirs are recorded here.

## 6. ScheduledReportAggregator as a spoke

- `[tool.docker] wireguard = true`: devkit-container's fetched mode (hub design 5). Migrated in
  3.1.0 on devkit-container 2.1.1 (2026-09-15), with the compose file's nine environment-mode lines
  removed by hand, since the rule engine never removes a key.
- Coolify environment: `WG_PRIVATE_KEY`, whose derived public key must be the enrolled one;
  `WG_HUB_URL=https://tunnels.sweetfiretobacco.com`; `WG_HUB_REPO=AetherBreaker/wireguard-hub`.
  `WG_HUB_TOKEN` stays unset while the hub repository is public (owner, 2026-09-15). The nine
  environment-mode values of earlier deploys must be absent: the binary refuses them beside
  `WG_HUB_URL`.
- Enrolled as `scheduled-report-aggregator` at `10.8.0.20/32` in wireguard-hub 1.2.0, with the
  public key `X3KOCWL9jdgVQthNUxlGqCk6ujaMDW37xQ34w2TOeCw=` derived from the private key in this
  checkout's `.env`. It was enrolled before its first fetched-mode start, so that start is not
  refused.
- Shutdown consent is not adopted until `aeth_ext`'s helper exists (hub design 6.4; owner,
  2026-09-14).
- Deployed in fetched mode at v3.1.2 since 2026-09-17: Coolify deploys `main`'s HEAD, whose compose
  file pins that tag. A running deploy is a connected one: the compose file requires the hub
  variables, fetched mode refuses to start the app without a first handshake unless
  `WG_TOLERATE_DISCONNECTED=1` is set (hub design 5.2), and the healthcheck that Coolify reports
  covers the tunnel's heartbeat file.

## 7. Open: the database itself

Deferred by the owner on 2026-09-14 until the tunnel was implemented, as not blocking it. The
engine is MSSQL (owner, 2026-10-14). Everything else here is undecided, and each item is the
owner's.

1. **Port and instance.** The flow lines, the firewall rule and the connection string want one
   static TCP port, so TCP/IP must be enabled on the instance with a fixed port. The instance and
   the port are unknown.
2. **Login.** A read-only SQL login (2), its password a Coolify secret. One login per app or one
   shared: undecided.
3. **Driver.** The Python driver and, if it needs one, the ODBC driver in the image. This project
   has no database dependency yet.
4. **Who may connect.** The hub design anticipated two flows: ScheduledReportAggregator
   (`10.8.0.20`) and `tunnel-probe` (`10.8.0.21`).
5. **The hub's flow lines**, one per approved flow, in wireguard-hub's `rules.v4`, in the shape of
   its existing camera-footage-fs line:
   `-A FORWARD -i wg0 -o wg0 -s 10.8.0.20/32 -d 10.8.0.10/32 -p tcp --dport <port> -m conntrack --ctstate NEW -j ACCEPT`.
6. **The PC's firewall rule.** Inbound TCP on the port from the approved tunnel addresses, all
   profiles; the Tailscale allowance removed once the tunnel replaces it. Whether the PC's
   installer writes this rule is for the installer's design.
7. **Order.** `tunnel-probe` first: it is the sandbox for the Python connect-and-query workflow
   over the tunnel, outside production (owner, 2026-09-14). This project's jobs query the database
   only after it has passed.

## 8. Checks

Moved from the hub design's first-deploy checklist on 2026-10-14; the hub design's numbers are in
brackets. Each is a hard stop if it fails.

- [ ] (16.4) `office-db-pc` and the hub handshake with each other before any app is involved. On
  the PC, `ping 10.8.0.1` answers: the hub accepts traffic to its own tunnel address. On the VPS,
  `docker exec $(docker ps -qf name=wireguard-hub) wg show wg0 latest-handshakes` lists the PC's
  public key with a timestamp a few seconds old. Coolify suffixes container names, so
  `docker exec wireguard-hub` by name fails. Other peers are not reachable from the PC: forwarding
  between peers is dropped unless a flow line allows it.
- [ ] The tunnel survives a reboot of the PC: before anyone logs in, the VPS command above shows a
  fresh handshake.
- [ ] (16.5) ScheduledReportAggregator, deployed with the environment of section 6, fetches the
  bundle, handshakes with the hub at the public name (the hairpin), and both of its heartbeat files
  are fresh; `docker inspect` shows `NET_ADMIN` in `cap_add`. The check's original first step, a
  start refused as `not enrolled`, does not apply: the key was enrolled before the first start.

The next phase's, once section 7 is decided:

- [ ] `tunnel-probe` runs its query against the database over the tunnel.
- [ ] A sibling container on the `coolify` network cannot reach the database port.
- [ ] The office PC's firewall rejects the database port from a tunnel address that is not
  approved.

## 9. Cleanup

This project's share of the hub design's cleanup list (section 12). Artefacts of the earlier
attempt are removed and never read as inputs; their key pairs are retired, never reused.

- [x] `SERVER_PUBLIC_KEY`, `SERVER_ENDPOINT` and `TAXES_JOB_PRIVATE_KEY` are gone from `.env`
  (checked 2026-10-14).
- [ ] `docker/wireguard/keys.env`, gitignored, is still present.
