# Deployment Boundary

Stage 1 is local only. The Web service binds to `127.0.0.1:8000`; PostgreSQL has no published host port.

Cloudflare Tunnel is the approved first-choice public ingress for a later stage, but no tunnel, DNS record, public port, firewall rule, Caddy, or Nginx configuration is part of Stage 1.
