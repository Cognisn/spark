# Reverse Proxy and TLS

Spark serves plain HTTP on port 8000 and expects a TLS reverse proxy in front of it.
Users, and every link Spark or warden sends, use the proxy's address, which Spark
must be told as `server.public_base_url`.

## server.public_base_url

Server mode refuses to start without it:

```
SPARK__SERVER__PUBLIC_BASE_URL=https://spark.example.com
```

- `https://host` or `https://host:port`, exactly as users type it. Plain `http` is
  accepted only for a loopback host (`localhost`, `127.0.0.1`, `::1`), for trying
  Spark on one machine.
- No path, query or credentials, and no trailing path: Spark is served at the root of
  its address, so it cannot sit under a prefix such as `https://example.com/spark`.
- A real host name, not a bind address such as `0.0.0.0`.

Set it in the env file so that web, worker and migrate all read the same value. It is
used for:

- **The Origin check.** Every state-changing request and every event stream must come
  from this exact origin (scheme, host and port), and its `Host` header must name this
  host. Anything else is refused with 403 and "Request refused: it did not come from
  this Spark instance's own pages (Origin check failed)".
- **The sign-in cookies.** warden's tokens live in cookies marked `Secure` and
  `HttpOnly`: the access cookie is `__Host-` prefixed, `SameSite=Strict`, on `/`; the
  refresh cookie is `__Secure-` prefixed and limited to `/auth/session/refresh`.
  Browsers keep `Secure` cookies only over https.
- **Links.** Invitations, password resets and the single sign-on redirect URI
  (`https://spark.example.com/identity/auth/sso/callback`) are built from it.

Changing the public address later means changing this setting, restarting web and
worker, and registering the new redirect URI with the single sign-on provider. Users'
sessions on the old address end.

## What the proxy must do

1. **Terminate TLS** with a certificate for the public host name, and redirect plain
   HTTP to https. Sending `Strict-Transport-Security` is recommended.
2. **Pass the `Host` header through unchanged.** The Host Spark sees must be the
   public host (with its port, if the address has a non-default one). A proxy that
   sends its upstream's name (`web:8000`, `127.0.0.1:8000`) instead makes every sign-in
   and every message fail the Origin check.
3. **Leave `Origin`, `Referer`, `Cookie` and `X-CSRF-Token` alone.**
4. **Serve Spark at `/`**, with no path prefix or rewriting.
5. **Stream responses.** Chat answers (`/stream/chat`), debates and panels arrive as
   server-sent events; buffering holds an answer back until it is complete. Turn response
   buffering off, and allow long reads: a model turn with tools can run for minutes.
6. **Accept large bodies**: at least the instance's largest upload
   (`files.max_upload_mb`, default 50 MB), and up to 1 GB on `/admin/bundles` if
   administrators import user bundles.
7. **Be the only way in.** Publish port 8000 on loopback only (the Compose default
   `SPARK_BIND=127.0.0.1`) or not at all (the proxy on the Compose network).

### Client addresses in the audit trail

warden records the client address of sign-ins and other audited actions. Behind a
proxy every request comes from the proxy, unless warden is told to believe the
proxy's `X-Forwarded-For` header. Name the proxy's address or network:

```
SPARK__WARDEN__AUDIT__TRUSTED_PROXIES=192.0.2.10/32
```

Only list addresses that are your proxy; a client that can reach Spark directly
could otherwise choose its own recorded address.

## Caddy

Caddy obtains and renews a certificate itself, passes `Host` through, flushes event
streams as they arrive and sets no body size limit. With Caddy in the Compose project
(see [Replicas](compose.md#replicas)), the `Caddyfile` is:

```
spark.example.com {
	header Strict-Transport-Security "max-age=31536000"
	reverse_proxy web:8000
}
```

With Caddy on the host instead, use `reverse_proxy 127.0.0.1:8000`.

## nginx

```nginx
server {
    listen 80;
    server_name spark.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name spark.example.com;

    ssl_certificate     /etc/nginx/tls/spark.example.com.crt;
    ssl_certificate_key /etc/nginx/tls/spark.example.com.key;
    add_header Strict-Transport-Security "max-age=31536000" always;

    client_max_body_size 1g;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_read_timeout 600s;
    }
}
```

`$host` carries no port. If the public address has a non-default port, use
`proxy_set_header Host $http_host;` instead.

## Checking it

From a machine outside the server:

```bash
curl -s -D - -o /dev/null https://spark.example.com/auth/sign-in
```

expects `200` (the request must be a GET: the page answers `HEAD` with 405) and a `Set-Cookie: __Secure-spark-sign-in=...; HttpOnly; Path=/auth/sign-in; SameSite=strict; Secure`
header. Then sign in from a browser. If the sign-in form answers with the Origin
check message, the proxy is changing `Host` or the address in the browser differs
from `server.public_base_url`.

> Checked for this guide: Caddy 2.8.4 in the Compose project in front of two web
> replicas, with `server.public_base_url` on a loopback https address and Caddy's
> internal certificate (`tls internal`). The sign-in page and its cookie, a password
> sign-in through to the second-factor step, a refused `http://` origin and a refused
> request that bypassed the proxy were all as described above. The nginx example
> was not run.
