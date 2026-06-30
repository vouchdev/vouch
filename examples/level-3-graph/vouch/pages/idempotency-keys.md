---
id: idempotency-keys
title: Idempotency keys
type: decision
status: draft
claims:
- incident-247-was-caused-by-a-missing-idempotency-key-on-the-
- every-charge-endpoint-write-must-carry-an-idempotency-key-st
entities:
- idempotency-keys
- incident-247
sources: []
tags: []
metadata: {}
created_at: '2026-06-30T02:31:51.178503Z'
updated_at: '2026-06-30T02:31:51.178504Z'
---
After incident-247, every charge-endpoint write carries an idempotency key stored in postgres-billing for 24h.