#!/usr/bin/env python3
"""
Complex-request passthrough test through the FULL production link:
  client -> Cloudflare DNS -> CloudFront (VPC Origin) -> NLB -> Envoy -> echo tenant

Uses an echo-server tenant (reflects method/headers/query/cookies/body as JSON) to
verify that each layer preserves a real browser-grade request. Checks:
  * query strings (incl. special chars, base64 '=', multiple params)
  * custom request headers
  * cookies
  * all HTTP methods (GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS)
  * request body (POST/PUT), content-type
  * large header value
  * Host header integrity (tenant routing correctness)

Requires an 'echo' tenant registered (ealen/echo-server). Run:
  python test_passthrough.py --host echo.webhost.jaydencrazy.win
"""
import argparse
import json
import sys
import urllib.request
import urllib.error


def do(host, method="GET", path="/", headers=None, data=None):
    url = f"https://{host}{path}"
    req = urllib.request.Request(url, method=method, data=data)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read().decode(errors="replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def echo_json(body):
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return {}


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    return cond


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="echo.webhost.jaydencrazy.win")
    args = p.parse_args()
    H = args.host
    results = []

    print("\n=== PT-1 Host header integrity (routing basis) ===")
    s, b = do(H)
    j = echo_json(b)
    results.append(check("Host preserved to origin", j.get("host", {}).get("hostname") == H,
                         f"got {j.get('host', {}).get('hostname')}"))

    print("\n=== PT-2 query string (special chars, base64, multi-param) ===")
    qs = "/?a=1&b=hello%20world&token=YWJjZD09&arr=x&arr=y&sp=a%2Bb%26c"
    s, b = do(H, path=qs)
    j = echo_json(b)
    q = j.get("request", {}).get("query", {})
    results.append(check("query a=1", q.get("a") == "1", str(q)))
    results.append(check("query b='hello world'", q.get("b") == "hello world", str(q)))
    results.append(check("query base64 token intact", q.get("token") == "YWJjZD09", str(q)))
    results.append(check("query special a+b&c", q.get("sp") == "a+b&c", str(q)))

    print("\n=== PT-3 custom request headers ===")
    s, b = do(H, headers={"X-Custom-Header": "my-value-123",
                          "X-Request-Id": "req-abc-789",
                          "Authorization": "Bearer test-token-xyz"})
    j = echo_json(b)
    h = j.get("request", {}).get("headers", {})
    results.append(check("X-Custom-Header passed", h.get("x-custom-header") == "my-value-123", str(h.get("x-custom-header"))))
    results.append(check("X-Request-Id passed", h.get("x-request-id") == "req-abc-789"))
    results.append(check("Authorization passed", h.get("authorization") == "Bearer test-token-xyz"))

    print("\n=== PT-4 cookies ===")
    s, b = do(H, headers={"Cookie": "session=abc123; theme=dark; uid=42"})
    j = echo_json(b)
    c = j.get("request", {}).get("cookies", {})
    results.append(check("cookie session", c.get("session") == "abc123", str(c)))
    results.append(check("cookie theme", c.get("theme") == "dark", str(c)))

    print("\n=== PT-5 HTTP methods ===")
    for m in ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]:
        s, b = do(H, method=m, data=(b"x=1" if m in ("POST", "PUT", "PATCH") else None),
                  headers={"Content-Type": "application/x-www-form-urlencoded"} if m in ("POST","PUT","PATCH") else None)
        j = echo_json(b)
        got = j.get("http", {}).get("method")
        results.append(check(f"method {m}", s in (200, 204) and (got == m or m == "OPTIONS"), f"status={s} echo_method={got}"))

    print("\n=== PT-6 POST body + content-type ===")
    payload = json.dumps({"name": "test", "value": 42, "nested": {"k": "v"}}).encode()
    s, b = do(H, method="POST", path="/", data=payload,
              headers={"Content-Type": "application/json"})
    j = echo_json(b)
    body_echo = j.get("request", {}).get("body", {})
    ct = j.get("request", {}).get("headers", {}).get("content-type", "")
    results.append(check("POST body preserved", body_echo.get("name") == "test" and body_echo.get("value") == 42, str(body_echo)))
    results.append(check("content-type application/json", "application/json" in ct, ct))

    print("\n=== PT-7 large header value (8KB) ===")
    big = "A" * 8000
    s, b = do(H, headers={"X-Big-Header": big})
    j = echo_json(b)
    got_big = j.get("request", {}).get("headers", {}).get("x-big-header", "")
    results.append(check("8KB header intact", got_big == big, f"len={len(got_big)} expected 8000"))

    print("\n=== PT-8 path with special chars / deep path ===")
    s, b = do(H, path="/api/v1/users/123/posts?filter=active&sort=-created")
    j = echo_json(b)
    ou = j.get("http", {}).get("originalUrl", "")
    results.append(check("deep path preserved", "/api/v1/users/123/posts" in ou, ou))

    total = len(results)
    passed = sum(1 for r in results if r)
    print("\n" + "=" * 44)
    print(f"  PASSTHROUGH: {passed}/{total} checks passed")
    print("=" * 44)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
