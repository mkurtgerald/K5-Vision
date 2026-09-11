# Security Policy

K5 Vision is currently an early public development project.

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
