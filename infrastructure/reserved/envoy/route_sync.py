#!/usr/bin/env python3
"""
Route-sync loop for Reserved Mode Envoy (subsystem B).

Polls the app_routes DynamoDB table and regenerates Envoy filesystem dynamic
config (rds.yaml = per-tenant virtual hosts, cds.yaml = per-tenant clusters).
Envoy watches the directory and hot-reloads on change — no restart, no
connection drain (spec ADR-5, filesystem xDS).

Route semantics (Codex-verified, spec §5.1): in bridge mode each tenant maps to
  subdomain -> host_ip (EC2 private IP) : host_port (dynamic host port)
NOT task IP. host_ip/host_port are written by the control plane / register script.

Only rows with status=routing are served (readiness gate, spec §4.D). Envoy
returns 404 for any Host not present in rds.yaml (spec §4.B, MT-4).

Design note: this test harness does a FULL refresh each poll (simple, correct at
test scale). ADR-5's incremental watermark path is a prod optimization.
"""
import os
import time
import tempfile

import boto3

TABLE = os.environ["APP_ROUTES_TABLE"]
REGION = os.environ.get("AWS_REGION", "us-east-1")
CONFIG_DIR = os.environ.get("ENVOY_CONFIG_DIR", "/etc/envoy-dynamic")
POLL = int(os.environ.get("POLL_INTERVAL_SECONDS", "3"))
DOMAIN = os.environ.get("TENANT_DOMAIN", "webhost.jaydencrazy.win")

ddb = boto3.client("dynamodb", region_name=REGION)


def fetch_routes() -> list[dict]:
    """Scan app_routes; return active (status=routing) tenant routes."""
    routes = []
    paginator = ddb.get_paginator("scan")
    for page in paginator.paginate(TableName=TABLE):
        for item in page.get("Items", []):
            status = item.get("status", {}).get("S", "")
            if status != "routing":
                continue
            routes.append({
                "subdomain": item["subdomain"]["S"],
                "host_ip": item["host_ip"]["S"],
                "host_port": int(item["host_port"]["N"]),
            })
    return routes


def render_cds(routes: list[dict]) -> str:
    """One cluster per tenant, each with a single endpoint host_ip:host_port
    and active HTTP health checking (spec §4.B: unhealthy auto-eject, MT-6)."""
    clusters = []
    for r in routes:
        name = f"tenant_{r['subdomain']}"
        clusters.append(f"""  - "@type": type.googleapis.com/envoy.config.cluster.v3.Cluster
    name: {name}
    connect_timeout: 2s
    type: STATIC
    lb_policy: ROUND_ROBIN
    health_checks:
      - timeout: 2s
        interval: 5s
        unhealthy_threshold: 2
        healthy_threshold: 1
        http_health_check: {{ path: "/" }}
    load_assignment:
      cluster_name: {name}
      endpoints:
        - lb_endpoints:
            - endpoint:
                address:
                  socket_address: {{ address: {r['host_ip']}, port_value: {r['host_port']} }}""")
    body = "\n".join(clusters)
    return f'resources:\n{body}\n' if clusters else "resources: []\n"


def render_rds(routes: list[dict]) -> str:
    """One virtual host per tenant matching Host: <subdomain>.<domain>.
    No default catch-all → unknown Host yields 404 (MT-4)."""
    vhosts = []
    for r in routes:
        name = f"tenant_{r['subdomain']}"
        vhosts.append(f"""      - name: {name}
        domains: ["{r['subdomain']}.{DOMAIN}", "{r['subdomain']}.*"]
        routes:
          - match: {{ prefix: "/" }}
            route:
              cluster: {name}
              timeout: 0s
              upgrade_configs:
                - upgrade_type: websocket""")
    body = "\n".join(vhosts) if vhosts else ""
    return f"""resources:
  - "@type": type.googleapis.com/envoy.config.route.v3.RouteConfiguration
    name: tenant_routes
    virtual_hosts:
{body if body else '      []'}
"""


def atomic_write(path: str, content: str) -> None:
    """Write via temp+rename so Envoy never reads a half-written file."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.replace(tmp, path)


def main() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    last_signature = None
    print(f"[route-sync] polling {TABLE} every {POLL}s -> {CONFIG_DIR}", flush=True)
    while True:
        try:
            routes = fetch_routes()
            signature = tuple(sorted(
                (r["subdomain"], r["host_ip"], r["host_port"]) for r in routes))
            if signature != last_signature:
                atomic_write(os.path.join(CONFIG_DIR, "cds.yaml"), render_cds(routes))
                atomic_write(os.path.join(CONFIG_DIR, "rds.yaml"), render_rds(routes))
                last_signature = signature
                print(f"[route-sync] updated: {len(routes)} tenant routes", flush=True)
        except Exception as e:  # noqa: BLE001 — keep polling; log and retry next tick
            print(f"[route-sync] error: {e}", flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
