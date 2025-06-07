# Experimental Attestation Layer

This proof-of-concept adds optional sender/receiver attestation for the AG1 bus.
Messages are signed with an HMAC and stored in a small SQLite ledger. Consumers
can verify the signature before processing the envelope.

## Components

- **crypto_utils.py** – Generates and verifies HMAC signatures for `Envelope`
  objects. The `auth_signature` field is populated automatically.
- **ledger.py** – Append-only SQLite store recording `(envelope_id, sender_id,
  signature, timestamp)`.
- **attestation_node.py** – Example helper that wraps `publish_envelope` and
  `subscribe` with signing and verification logic.

## Usage

1. Set `ATTEST_SECRET` to the shared secret used for HMAC signing.
2. Use `publish_with_attestation` and `subscribe_with_attestation` from
   `attestation_node.py` instead of the core bus helpers.
3. The ledger `attestations.db` is created in the current working directory.

This directory is self-contained and does not modify the existing bus. It can be
removed or ignored if attestation is not desired.
