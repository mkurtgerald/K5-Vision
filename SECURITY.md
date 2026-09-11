# Security Policy

K5 Vision is currently an early public development project. Security hardening is performed continuously as part of each delivery gate rather than deferred to a final stabilization phase.

## Stage security hardening

Every active gate must apply the relevant security requirements in `docs/HARDENING_STANDARD.md` before it can close.

At minimum:
- validate trust boundaries and externally supplied data
- fail explicitly on unsupported or malformed external behavior
- keep credentials and sensitive values out of source, exceptions, diagnostics, and ordinary logs
- use bounded retry/timeout behavior at external boundaries
- minimize sensitive-data retention
- review new dependencies and artifacts before acceptance
- add regression coverage for security-relevant defects
- resolve known exploitable behavior at the earliest responsible layer before downstream progression

## Never commit

- external-system usernames/passwords
- API tokens
- cloud credentials
- private keys/certificates
- customer or site information
- production addresses, topology, or VPN details
- proprietary third-party source code
- sensitive reference data or protected artifacts

Use environment variables or an approved secret store for local/deployment secrets.

## Reporting a vulnerability

Do not post exploitable security details, credentials, protected data, or customer information in a public issue. Contact the repository owner privately through an appropriate GitHub-supported private channel until a dedicated security-reporting mechanism is configured.
