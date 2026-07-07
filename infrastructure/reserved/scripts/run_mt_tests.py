#!/usr/bin/env python3
"""
Multi-tenant test runner — executes the automatable cases from
tests/multi-tenant-test-plan.md against the public ALB.

Covers:
  MT-1 routing correctness  (each tenant URL -> its own container)
  MT-2 no cross-tenant leakage (concurrent, sampled)
  MT-4 unregistered subdomain -> 404

Assertion: response JSON tenant_id == requested Host subdomain.

Usage:
  python run_mt_tests.py --alb <alb-dns> --domain webhost.jaydencrazy.win \
      --tenants 10 --concurrency 100
"""
import argparse
import concurrent.futures
import json
import subprocess
import sys


def fetch(alb: str, host: str, path: str = "/") -> tuple[int, str]:
    """Fetch via curl (not urllib): this pyenv's hashlib lacks blake2, which
    breaks urllib's TLS on HTTPS. curl is unaffected. `host` is the tenant
    subdomain FQDN; we hit it directly over HTTPS through the real domain."""
    url = f"https://{host}{path}"
    try:
        out = subprocess.run(
            ["curl", "-s", "-w", "\n%{http_code}", "--max-time", "20", url],
            capture_output=True, text=True, timeout=25)
        parts = out.stdout.rsplit("\n", 1)
        if len(parts) == 2:
            body, code = parts
            return int(code) if code.isdigit() else 0, body
        return 0, out.stdout
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def tenant_id_of(body: str) -> str | None:
    try:
        return json.loads(body).get("tenant_id")
    except Exception:  # noqa: BLE001
        return None


def mt1_routing(alb: str, domain: str, n: int) -> bool:
    print("\n[MT-1] routing correctness")
    ok = True
    for i in range(1, n + 1):
        host = f"tenant-{i}.{domain}"
        status, body = fetch(alb, host)
        got = tenant_id_of(body)
        good = status == 200 and got == f"tenant-{i}"
        ok = ok and good
        print(f"  {host} -> status={status} tenant_id={got} {'✓' if good else '✗ MISMATCH'}")
    print(f"  MT-1 {'PASS' if ok else 'FAIL'}")
    return ok


def mt2_isolation(alb: str, domain: str, n: int, concurrency: int) -> bool:
    print(f"\n[MT-2] no cross-tenant leakage ({concurrency} concurrent x {n} tenants)")
    tasks = []
    for i in range(1, n + 1):
        for _ in range(concurrency):
            tasks.append(f"tenant-{i}")
    mismatches = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
        futs = {ex.submit(fetch, alb, f"{t}.{domain}"): t for t in tasks}
        for fut in concurrent.futures.as_completed(futs):
            expected = futs[fut]
            status, body = fut.result()
            if tenant_id_of(body) != expected:
                mismatches += 1
    total = len(tasks)
    ok = mismatches == 0
    print(f"  {total} requests, {mismatches} mismatches. MT-2 {'PASS' if ok else 'FAIL'}")
    return ok


def mt4_unregistered(alb: str, domain: str) -> bool:
    print("\n[MT-4] unregistered subdomain -> 404")
    status, _ = fetch(alb, f"tenant-999.{domain}")
    ok = status == 404
    print(f"  tenant-999 -> status={status} (expect 404). MT-4 {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--alb", required=True, help="ALB DNS name")
    p.add_argument("--domain", default="webhost.jaydencrazy.win")
    p.add_argument("--tenants", type=int, default=10)
    p.add_argument("--concurrency", type=int, default=100)
    args = p.parse_args()

    results = {
        "MT-1": mt1_routing(args.alb, args.domain, args.tenants),
        "MT-2": mt2_isolation(args.alb, args.domain, args.tenants, args.concurrency),
        "MT-4": mt4_unregistered(args.alb, args.domain),
    }
    print("\n" + "=" * 40)
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    print("=" * 40)
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
