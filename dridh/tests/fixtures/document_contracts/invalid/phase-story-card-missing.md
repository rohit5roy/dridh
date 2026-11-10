# Phase 1 — Order Service

> **Standards Version:** 2.1.0  
> **Document Status:** DRAFT  
> **Last Updated:** 2026-11-06  
> **Component:** CMP-01

## ST-1.1: Domain model

| Field | Value |
|:------|:------|
| **Use Case ID** | UC-01 |
| **Flow IDs** | SF-01.1, SF-01.2 |
| **Title** | Domain model |
| **Description** | Model the order aggregate and its states. |
| **Status** | NOT_STARTED |
| **Dependencies** | None |
| **Unlocks** | ST-1.2 |
| **What Will Be Done** | 1. Define the order entity.<br>2. Define order states. |
| **Acceptance Criteria** | An order can move from submitted to authorised. |
| **Definition of Done** | Unit tests pass and evidence is recorded. |

## ST-1.2: Persist orders

Story card omitted.
| **Title** | Persist orders |
| **Description** | Store orders durably. |
| **Status** | NOT_STARTED |
| **Dependencies** | ST-1.1 |
| **Unlocks** | — |
| **What Will Be Done** | 1. Create the orders table.<br>2. Persist the aggregate. |
| **Acceptance Criteria** | A stored order can be read back unchanged. |
| **Definition of Done** | Integration tests pass against a real database. |
