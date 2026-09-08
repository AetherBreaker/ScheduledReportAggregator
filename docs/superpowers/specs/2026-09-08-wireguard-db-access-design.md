# WireGuard access to the office SQL database

Date: 2026-09-08. Status: architecture approved in discussion; the open decisions in section 9
must be answered before implementation plans are written. Plans are written in separate sessions.

Companion spec (the tooling that makes this possible):
`aeth_devkit/docs/superpowers/specs/2026-09-08-devkit-split-and-container-wireguard-design.md`.
Nothing in this document can be implemented until steps 1 and 2 of that spec's section 7 have
shipped: the extracted `devkit-container` with its wireguard mode, and the
`[tool.docker].wireguard` switch in `setup-project`.

## 1. Goal and constraints

ScheduledReportAggregator runs on a cloud VPS as a Coolify-managed Docker container. It must
reach a SQL database running on a Windows PC on the office LAN, over WireGuard.

Constraints, all from the owner:

- **In-house end to end.** No Tailscale-class overlay, hosted or self-hosted. A Cloudflare Tunnel
  was the only outside option considered and lost to WireGuard because it puts a third party in the
  data path.
- **Every real user of the network has its own identity.** Its own key pair, its own tunnel
  address, so activity can be attributed. No shared forwarder between apps.
- **The database must not be reachable by any other container on the `coolify` network.** A
  compromised sibling container must have no path to it.
- **The test project's wireguard stack must be identical to this project's.** Testing done there
  must be accurate for what happens here.
- **Coolify conventions hold.** Auto-deploy on changes under `docker/**`, consistent container
  names, every container on the external `coolify` network so `central-log-server` resolves.
- **Preferred, not required.** healthchecks.io dead-man pings and Pushover alerts for every new
  component.

## 2. Decision: the tunnel lives inside the app container

The app container brings up its own WireGuard interface. The `devkit-container` entrypoint does
the root-only work (render config, bring up `wg0`, wait for a handshake), then spawns the Python
app unprivileged and supervises the tunnel as PID 1. The app connects to the office PC's tunnel
address like any other host. No sidecar, no relay, no extra Docker network.

Why this satisfies every constraint:

- **Nothing listens anywhere.** The container offers no port on the `coolify` network and holds
  no relay. A sibling container has no path to the DB because nothing is offered.
- **Each app is its own peer.** This project and the test project each have their own key and
  address. The hub's logs and the office PC's connection sources attribute traffic per app.
- **Identical by construction.** Both projects set one switch in `pyproject.toml` and pin the
  same `devkit-container` release. The tunnel logic exists in exactly one place; the projects
  differ only in environment values. There is no folder to keep in sync.
- **The app process holds no capabilities.** The privilege drop the entrypoint already performs
  applies unchanged; the smoke tests in the container repo assert empty capability sets.
- **Nothing about Coolify conventions changes.** One service, the same container name, the same
  network, the same heartbeat healthcheck. The compose file gains `cap_add`, `devices`, `sysctls`
  and `WG_*` environment lines, which `setup-project` renders and the compose validator ignores.
- **Monitoring folds into what exists.** The entrypoint writes `/run/devkit/wireguard.json`; the
  app reads it and a stale tunnel becomes an ordinary `aeth_ext` alert over email and Pushover, and
  healthchecks.io keeps seeing one heartbeat per app. The `aeth_ext` reader is follow-on work.

### Rejected alternatives

| Option | Why it lost |
|---|---|
| Sidecar, app shares its network namespace (`network_mode: service:`) | Compose forbids `networks:` on such a service, so Coolify's compose rewriting may reject or break it; recreating the sidecar orphans the app's network. Isolation is equal to the chosen design, cost is higher. |
| Sidecar as a relay on a private `internal: true` network with iptables DNAT | Works and is standards-tolerant, but needs a second network per project, DNAT rules, a separate healthchecks slug per sidecar, and a copied `docker/wireguard/` folder in every consuming repo that must stay byte-identical by hand. |
| Relay on the hub, or any relay reachable on `coolify` | Exposes the DB port to every container on the `coolify` network and gives all apps one shared identity. |
| Cloudflare Tunnel | Third party in the data path; still needs `cloudflared` in or beside the container, so it removes none of the container-side questions. |
| Tailscale, Headscale, Netbird, ZeroTier | Rejected outright: not in-house. |
| Userspace WireGuard forwarder (`onetun`) inside the container | No privileges needed, but a small third-party project on the DB path. Fallback only if the VPS kernel turns out to lack the wireguard module. |

## 3. Topology

Hub and spoke. One hub on the VPS; every other node is a spoke that only talks to the hub.

- **Hub.** A dedicated Coolify application with its own container, publishing one UDP port on the
  VPS's public address. It forwards between spokes and holds the only `ListenPort`.
- **Spokes.** The office DB PC, this project's container, the test project's container. Each has
  its own key pair and a fixed tunnel address, and each reaches the hub at its public endpoint.
- **Containers on the VPS also use the public endpoint,** not the hub's `coolify` address. Every
  peer then has the same `Endpoint`, no project depends on the hub's container name, and the hub
  can move hosts without touching consumers. This is hairpin NAT through Docker; it normally works
  and is checked on first deploy. If it ever misbehaves, the fallback is the hub's address on the
  `coolify` network.
- **No DNS inside the tunnel.** The DB host is reached by its fixed tunnel address, passed to the
  app as an environment variable.

Proposed address plan, pending the subnet decision in section 9:

| Node | Address |
|---|---|
| hub | `.1` |
| office DB PC | `.10` |
| ScheduledReportAggregator | `.20` |
| test project | `.21` |

## 4. Security controls, layered

- **Cryptokey routing.** The hub accepts a packet from a peer only if its source is inside that
  peer's `AllowedIPs`, and forwards only to a peer whose `AllowedIPs` contains the destination.
  Unauthenticated packets to the hub's port are dropped silently, so a rogue container that can
  reach the port gains nothing.
- **Hub forwarding rules.** iptables `FORWARD` rules on the hub restrict spoke-to-spoke traffic to
  specific source, destination and port. Even a compromised app peer can reach only the DB port on
  the DB host.
- **Windows Firewall.** The DB port on the office PC is allowed only from the tunnel addresses of
  approved peers.
- **Database account.** The app's DB user has read-only rights to the tables it needs.
- **Key handling.** Private keys are Coolify environment secrets, never in git. The entrypoint
  reads `WG_PRIVATE_KEY` and removes it from the app's environment before the app starts. Local
  development keeps keys in the gitignored `.env`.

## 5. Changes to this project

Once the devkit prerequisites have shipped:

1. `pyproject.toml`: set `[tool.docker].wireguard = true`.
2. Run `poe setup-project`. It renders the Dockerfile block that installs `wireguard-tools` and
   `iproute2`, and adds to the app service in `docker/compose.yaml`: `cap_add: [NET_ADMIN]`,
   `devices: [/dev/net/tun:/dev/net/tun]`, `sysctls: net.ipv4.conf.all.src_valid_mark=1`, and the
   `WG_*` environment lines as `${NAME:?}` interpolations. Review the diff; nothing else in the
   compose file changes.
3. Coolify: add the `WG_*` values (private key, address, hub public key, hub endpoint, allowed
   IPs) and the DB connection settings (host is the DB PC's tunnel address) to the application's
   environment. The compose change under `docker/` triggers the redeploy.
4. Delete `docker/entrypoint.sh` and `docker/scripts/`. They are leftovers of the shell entrypoint;
   devkit already reports them as safe to delete.
5. Later, in the app: the DB client for the reporting jobs (out of scope for this spec) and, once
   `aeth_ext` gains it, the status-file reader that turns a stale tunnel into an alert.

The compose file after step 2 is identical to the test project's except for the container name,
the build args and the persisted-data bind source. That equality is the test project's whole
reason to exist, so it is checked by diff whenever either changes.

## 6. Sister projects to create

Each is its own git repository. The previous attempt's repositories are not inputs (section 10).

### 6.1 The hub

A Coolify application. Recommended shape, to be confirmed: a pinned `linuxserver/wireguard` image
with a server-role `wg0.conf` template rendered from environment at start, the peer table
(public keys and addresses) committed since public keys are not secrets, `PostUp` rules for
forwarding and the per-peer `FORWARD` restrictions, the published UDP port, `NET_ADMIN`,
`/dev/net/tun`, and `net.ipv4.ip_forward`. Files live under `docker/` so the redeploy watcher
applies. Healthcheck: interface up and at least one recent handshake. healthchecks.io: a small
loop that pings the hub's slug while the interface is healthy; Pushover comes from
healthchecks.io's own integration, so the container carries no Pushover code. The hub joins the
`coolify` network like every other container.

### 6.2 The office PC

Recommended, to be confirmed: **native WireGuard for Windows**, not Docker Desktop under WSL2.

- The native client runs as a Windows service at boot using the WireGuardNT kernel driver, before
  anyone logs in. Docker Desktop needs a logged-in session to start reliably, so a reboot after a
  Windows update can leave the tunnel down until someone logs in.
- The native tunnel terminates on the Windows host, so the DB port is directly reachable at the
  PC's tunnel address. Under WSL2 the tunnel ends inside the Linux VM and needs a second relay to
  reach a Windows service, across networking that has changed shape between WSL versions.
- The repository holds: a `.conf` template with the private key gitignored, a PowerShell install
  script that renders the config and installs the tunnel service, the Windows Firewall rule scoped
  to peer tunnel addresses, and a healthcheck script registered as a Scheduled Task that pings the
  PC's healthchecks.io slug while the handshake is fresh. That is as syncable as a compose file.
- The PC needs only outbound UDP to the hub. No port forward on the office router.

### 6.3 The test project

A devkit-managed Python project deployed through Coolify exactly like this one, with
`[tool.docker].wireguard = true` and the same `devkit-container` pin. Its app is a loop that
resolves the DB host over the tunnel, runs a trivial query, and logs the result, plus a
hello-world so the standard scaffolding is exercised. It has its own key, address and
healthchecks slug. It exists so connectivity and queries can be tested without touching this
project.

## 7. Monitoring per component

| Component | Docker healthcheck | healthchecks.io | Pushover |
|---|---|---|---|
| this project and the test project | existing heartbeat file, unchanged | existing per-app heartbeat | `aeth_ext` alert on a stale tunnel, once the status-file reader exists |
| hub | interface up, recent handshake | hub slug, pinged while healthy | via healthchecks.io integration |
| office PC | n/a | PC slug, pinged by the Scheduled Task | via healthchecks.io integration |

## 8. First-deploy verification

In this order, each a hard stop if it fails:

1. The VPS kernel has the wireguard module (`modprobe wireguard` on the host).
2. Coolify passes `cap_add`, `devices` and `sysctls` through unchanged (inspect the running
   container).
3. The hub and the office PC handshake with each other before any app is involved.
4. This project's container handshakes with the hub at the public endpoint (the hairpin check)
   and `/run/devkit/wireguard.json` reports `up`.
5. The test project runs its query against the DB over the tunnel.
6. A sibling container on the `coolify` network cannot reach the DB port (negative test).
7. The office PC's firewall rejects the DB port from a tunnel address that is not approved.

## 9. Open decisions

Each must be answered before a plan is written. Recommendations are noted where one exists.

- **Database engine and port.** Assumed SQL Server on 1433 so far; MySQL or Postgres changes the
  forwarding rule, the firewall rule and the Python driver the test project needs.
- **Is the engine listening on the PC's LAN address, or only on localhost?** A default SQL Server
  Express install has TCP disabled; enabling it would be a Windows-side setup step.
- **Is the PC always on and wired?** Does anything else on the office LAN need to be reachable
  later, or is it strictly this one machine?
- **Tunnel subnet.** Default `10.8.0.0/24` unless the office LAN or the VPS already uses `10.x`.
- **Office PC client.** Native WireGuard for Windows is recommended (section 6.2); confirm, or
  state a constraint on that PC that changes it.
- **Hub image and repository shape.** Recommended in section 6.1; confirm.
- **Test project name.**
- **healthchecks.io slugs** for the hub, the PC, and the test project.
- **Final `WG_*` variable names** follow the container repo's schema doc once that plan fixes them.

## 10. The previous attempt

This design is a do-over. The earlier attempt's artefacts were deliberately not read while
designing this and are not inputs to any plan. They exist only as a cleanup list, each to be
verified before removal:

- this repository's feature branch from that attempt;
- the workspace folders `wireguard-test-sender`, `wireguard-vps-relay` and
  `wireguard-warehouse-client`, and the workspace-root `compose.test.yaml`;
- the gitignored `docker/wireguard/keys.env` in this repository;
- the `SERVER_PUBLIC_KEY` and `SERVER_ENDPOINT` entries in this repository's `.env`, if they
  belong to that attempt;
- any key pairs generated for that attempt, which should be treated as retired rather than reused.