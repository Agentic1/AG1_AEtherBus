# AG1 Core Bus – Encryption Workflow

The bus can optionally encrypt envelope content while using the experimental attestation layer. This keeps payloads confidential while retaining signature verification.

## Workflow

1. **Sign then encrypt** – The sender signs the envelope using the same HMAC or asymmetric key used by the attestation helper.
2. **Encrypt payload** – After signing, the serialized envelope is encrypted (e.g., with AES-GCM) and the ciphertext is published to the bus.
3. **Decrypt then verify** – Receivers decrypt the message first and then verify the signature against the ledger entry.

```
 Producer            Validator Nodes                   Consumer
   |                     |                                |
   | sign & encrypt      |                                |
   |-------------------->|                                |
   |                     |---decrypt & verify-----------> |
   |                     |<-----------ledger------------->|
```

## Implementation Notes

- Encryption is handled outside the core bus. Use a helper to wrap `publish_envelope` and `subscribe` similar to the HMAC attestation utilities in `feature/experimental_attestation`.
- The attestation ledger records the signature before encryption so validators can check authenticity after decryption.
- Any symmetric or asymmetric algorithm can be used; the example assumes AES-GCM for payload encryption and HMAC for signing.

This approach keeps the message format consistent while allowing optional confidentiality in addition to integrity and authenticity.
