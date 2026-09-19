# K5 Security Architecture Baseline

Status: architecture requirement. This document defines security properties that future K5 implementation stages must preserve. It does not activate downstream implementation ahead of existing dependency gates.

## Core invariant

Possession of a K5 recorder, appliance, disk, backup, or encrypted recording must not by itself provide the authority or usable secrets required to decrypt protected recordings.

"Encrypted at rest" is incomplete unless key custody, recovery, rotation, revocation, and physical compromise are explicitly designed and tested.

## Root of trust and key custody

- Never persist plaintext master/root encryption keys beside protected recordings.
- Prefer TPM 2.0 or equivalent hardware-backed device identity and key sealing on K5 appliances.
- Support externally protected KMS/HSM custody for deployments requiring centralized or stronger key control.
- Use envelope encryption: data-encryption keys (DEKs) protect data; independently protected key-encryption keys (KEKs) wrap DEKs.
- Separate keys by site/device/security domain so compromise of one appliance does not expose the fleet.
- Define rotation, revocation, backup, escrow/recovery, and authorized disaster-recovery procedures.
- Recovery must not depend solely on the original motherboard/TPM; legitimate recovery requires separately protected authority.
- Do not emit secrets to logs, telemetry, crash dumps, support bundles, repositories, CI artifacts, or UI diagnostics.
- Prefer short-lived credentials and hardware/service identities over static shared secrets.

## Physical compromise and evidence survival

Encryption protects confidentiality, not availability. K5 must assume a recorder, drive, camera, or entire site can be stolen, destroyed, or disconnected.

- Support policy-controlled redundant recording/storage and remote/cloud archival.
- Allow critical events/evidence to be replicated away from the originating physical location.
- Detect and report device disappearance, tamper signals where available, storage loss, and unexpected topology changes.
- Preserve local operation during WAN loss and reconcile protected state when connectivity returns.

## Zero-trust service boundary

Network location alone never establishes trust.

- Authenticate and authorize users, devices, nodes, and service-to-service calls.
- Encrypt service communications in transit; use mTLS/service identity where appropriate.
- Apply least privilege and explicit capability/role checks.
- Keep inbound exposure at zero forwarded ports by default; one only if a justified integration cannot avoid it.
- A compromised node must not automatically become trusted by the rest of the cluster.

## Evidence integrity

- Preserve original evidentiary media independently of privacy rendering/transcoding.
- Bind evidence to source identity, time/provenance, integrity metadata, and audit history.
- Support cryptographic integrity verification/signing where the source and platform permit it.
- Privacy reveal, unmasked export, evidence unlock, key recovery, and privileged administrative actions require explicit authorization and audit records.

## Threat-model gates

Before a subsystem is considered production-ready, document and test its response to applicable scenarios:

1. stolen recorder or appliance
2. removed/stolen recording disk
3. stolen or replaced camera
4. malicious or compromised administrator
5. compromised Windows application/control server
6. compromised Linux recorder/media node
7. database exfiltration
8. backup/archive theft
9. intercepted or hostile WAN/LAN
10. rogue cluster node
11. camera credential compromise
12. cloud account/KMS compromise
13. ransomware or destructive malware
14. insider evidence manipulation
15. physical destruction of local storage/site
16. supply-chain/update compromise
17. loss/corruption of TPM or hardware root of trust
18. secret leakage through logs, dumps, CI, telemetry, support bundles, or configuration
19. clock manipulation affecting forensic timelines
20. license/entitlement compromise attempting to bypass security boundaries

For each applicable threat, record: protected asset, trust boundary, attack precondition, expected failure mode, prevention/detection control, recovery path, retained evidence/audit signal, and an executable verification test where practical.

## CI/security acceptance

Security-sensitive stages should add automated checks appropriate to their surface, including:

- secret scanning and repository-history protection
- tests proving protected key material is not written in plaintext
- sanitized logs/errors/support bundles
- negative authorization tests
- corrupted/tampered evidence rejection
- credential/key rotation and revocation tests
- backup/restore and disaster-recovery tests
- fail-closed behavior when key services or identity validation are unavailable
- dependency/SBOM/vulnerability checks
- signed/verifiable release/update path where supported

## Tiering

These are K5 platform security/evidence properties across Free, Mid, and Enterprise. Security fundamentals are not premium feature gates. Enterprise may add stronger centralized KMS/HSM, federation, redundancy, compliance, and policy capabilities without weakening the baseline in lower tiers.

## Sequencing

This document is a cross-cutting contract, not a new parallel feature lane. Implementation enters at the appropriate dependency stage. Existing K5 stage gates remain authoritative; downstream features do not bypass unstable precursors.
