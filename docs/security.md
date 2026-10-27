# Stage 1 Security Baseline

- Secrets are injected through environment variables and excluded from Git.
- Django DEBUG, allowed hosts, trusted CSRF origins, and secure cookies are environment-controlled.
- Logs are written to stdout/stderr with bounded Docker rotation and a redaction filter.
- Application containers run as an unprivileged user with all Linux capabilities dropped, no privilege escalation, and a read-only root filesystem.
- PostgreSQL is available only on the private Docker network and has no host port mapping.
- The Web service binds only to host loopback during Stage 1.
- No public ingress, Cloudflare, AI, email, collection, Redis, Node, or pgvector is configured.
