# Redis Pulse

Read-only Chinese dashboard at `/monitor/`, using locally served CSS/JS with no external analytics or fonts. The queue API proxies only this namespace to a separate loopback-bound Gunicorn service on port 16380.

## Deployment

Run alongside the existing compose project with `docker compose -f docker-compose.yml -f compose.monitor.yml up -d --build`. Only the new dashboard service needs `monitor.env`, a mode-0600 file containing `MONITOR_SECRET`, `MONITOR_PASSWORD_HASH` (Werkzeug hash), and `MONITOR_API` (the loopback API URL). Never commit these values. Cookies are HTTPS-only by default; `MONITOR_INSECURE_LOCAL=1` is for isolated local development only.

The monitor runs one process, four HTTP threads, with a 192 MB memory limit and 0.5 CPU ceiling. It reads only access logs and selected host `/proc` files. No Docker socket is mounted. The original queue read/write behavior is unchanged.

## Data semantics

- Initial backfill: at most the last 8 MB of the current access log. New complete lines are then read incrementally, including log rotation. Query strings are discarded. The last 500 request records are retained in memory; the API returns at most 100.
- Per-minute aggregates: up to one hour, with bounded IP and route cardinality. History is reset when the monitor restarts. The actual earliest observed log time is shown; unavailable history is not fabricated.
- Request times are completion times from Gunicorn access logs. IPs come from the existing logged X-Real-IP, falling back to the connected peer. Behind a CDN they may identify an edge proxy. This is not verified end-user identity.
- All requests include operational polling and health checks, except `/monitor` itself. HTTP status >=400 counts as an error; <400 counts as successful.
- CPU, memory, load, uptime, disk and network reflect the host. Disk is the filesystem containing the mounted log directory. Memory used = total minus available. Network excludes loopback, Docker bridges and veth devices.
- Collection and inventory refresh every 5 seconds independently of open browsers. The inventory endpoint performs the previously implemented expired-entry cleanup. CPU needs two samples. Inventory 2900 markers are informational; no producers are paused by this dashboard.
- Authentication: independent password, signed 12-hour secure cookie, global failed-login rate bound, no-store responses, same-origin CSP. Credentials never appear in API responses. Expose only via HTTPS.

## Checks

`python -m unittest monitor.test_monitor` tests parsing, authentication, aggregation and incremental/rotated logs. Existing `test_counts.py` verifies the queue counting logic against isolated Redis keys.
