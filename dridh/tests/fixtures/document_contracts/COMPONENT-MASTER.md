# Order Service — Component Master

> **Standards Version:** 2.1.0  
> **Document Status:** DRAFT  
> **Last Updated:** 2026-11-06  
> **Component:** CMP-01

## Component Overview

- **Component ID:** CMP-01
- **Canonical TDD Definition:** `feature_tdd.md#cmp-01-order-service`
- **Component Name:** Order Service

## Table 4 — Story Index

| Story ID | Phase | Story Name | Use Cases | Status |
|---|---|---|---|---|
| ST-1.1 | 1 | Domain model | UC-01 | NOT_STARTED |
| ST-1.2 | 1 | Persist orders | UC-01 | NOT_STARTED |

## Table 5 — Phase Status Dashboard

| Phase | Total Stories | NOT_STARTED | IN_PROGRESS | IN_REVIEW | COMPLETED | BLOCKED |
|---|---|---|---|---|---|---|
| 1 | 2 | 2 | 0 | 0 | 0 | 0 |
| **Total** | 2 | 2 | 0 | 0 | 0 | 0 |

#### CDD-1: Store orders in one table

- **Context / Problem:** Storage layout for orders.
- **Decision:** Single table keyed by order ID.
