---
id: billing-platform-overview
title: Billing platform overview
type: concept
status: draft
claims:
- billing-service-depends-on-postgres-billing-for-all-durable-
- ledger-api-owns-the-billing-service-and-is-maintained-by-pla
entities:
- billing-service
- postgres-billing
- ledger-api
sources: []
tags: []
metadata: {}
created_at: '2026-06-30T02:31:50.788557Z'
updated_at: '2026-06-30T02:31:50.788559Z'
---
Canonical overview of the billing platform. billing-service charges customers and persists to postgres-billing. Owned by ledger-api / platform-team.