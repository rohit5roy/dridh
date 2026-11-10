# Order Intake — Technical Design Document

> **Standards Version:** 2.1.0  
> **Document Status:** DRAFT  
> **Last Updated:** 2026-11-06  
> **Feature:** Order Intake  
> **Owner:** Platform team  
> **Source Use Case Document:** `.opencode/dridh/documents/001-USE-CASES.md`  
> **Source Revision:** rev-usecases-v0

## Document Control

This document records the design baseline for Order Intake.

## Blocking Reviewers

N/A — no blocking reviewers are registered for this fixture.

## Problem Statement & Resources

Orders must be captured and paid for without duplicate charges.

## Definitions

Order: a confirmed customer purchase request.

## Scope

Order placement and cancellation for UC-01 and UC-02.

## Out of Scope

Refund workflows are handled by a later feature.

## Target Dates and Milestones

N/A — dates are tracked in the central plan.

## Design Summary

A checkout service owns orders and delegates payment authorisation.

## Design Details

### System Context and Architecture

The order service is a new component that calls the existing payment gateway.

### Component Responsibilities

### CMP-01: Order Service

- Status: new.
- Responsibility: accepts, stores and cancels orders.
- Flow IDs: SF-01.1, SF-01.2, EF-01.1, SF-02.1, EF-02.1.

### CMP-02: Notification Service

- Status: existing.
- Responsibility: sends order confirmations.

### Internal and External Data Flow

The order service records the order, then requests authorisation from the payment gateway.

### Use Case-to-Design Traceability Matrix

| UC ID | Flow IDs | Catering Component(s) | API / Event | Design Decision (DD-N) |
|---|---|---|---|---|
| UC-01 | SF-01.1, SF-01.2, EF-01.1 | CMP-01 — Order Service | POST /orders | DD-1 |
| UC-02 | SF-02.1, EF-02.1 | CMP-01 — Order Service, CMP-02 — Notification Service | Event: order.cancelled | N/A |

### Data Ownership and Persistence

The order service owns the orders table.

### Scalability and Future Evolution

Expected load is 200 orders per minute at peak; assumption recorded in Open Questions.

## Design Decisions

#### DD-1: Use an outbox for order events

- **Context / Problem:** Order events must reach consumers reliably.
- **Decision:** Write events to an outbox table in the same transaction.
- **Alternatives Considered:** Direct publish after commit.
- **Rationale:** Avoids lost events on crash.
- **Consequences / Trade-offs:** Requires a relay process.
- **Affected Components:** CMP-01 — Order Service.
- **Related Use Cases / Flows:** UC-01, SF-01.2.
- **Reversibility:** Reversible.
- **Evidence / References:** N/A.

## Dependencies

The payment gateway must expose an authorisation endpoint.

## API Design

### API Inventory

POST /orders.

### OpenAPI Specification

N/A — the OpenAPI specification is tracked in the component master.

## Events

### Event Inventory

order.cancelled.

### AsyncAPI Specification

N/A — the event is internal to the feature.

## Data Model

Orders table with one row per order.

## Quality Approach

### Quality Risks and Assumptions

Duplicate authorisation is the main quality risk; see R1.

### Test Scope

UC-01 and UC-02 success and exception flows.

### Test Strategy

Real database containers for integration tests.

### Environments and Test Data

Local Testcontainers environment.

### Route to Release

Release after the test plan passes.

## Well-Architected Considerations

### Cost Considerations

Single database instance.

### Security & Privacy Considerations

No card data is stored by this feature.

## Expected Level of Effort

Two stories, about three days.

## Open Questions And Assumptions

Peak load of 200 orders per minute is an assumption.

## OPTIONAL — Technical Overview Supporting Sections

N/A — the optional technical overview sections below are retained for reviewers.

## Technical Justifications / Alternative Designs

N/A — no alternative designs beyond DD-1.

## Integration/Migration Plan

N/A — greenfield feature.

## Rollback Plan

N/A — feature flag removal is the rollback.

## Feature Flags

N/A — no feature flag is required.

## Feature/Service Logic

Orders are validated before authorisation is requested.

## Scaling Estimate

Single instance handles the assumed peak load.

## Monitoring & Alerting

N/A — monitoring is defined by the platform team.

## Failure Scenarios And Recovery

Payment timeouts leave the order pending until retried.

## Risks

### R1: Duplicate payment authorisation

Retries may authorise the same order twice; mitigated by idempotency keys.

## Acceptance Criteria

Orders are accepted only after authorisation is recorded.

## AI Systems

N/A — this feature does not introduce an AI system.

## TDD Completion Checklist

- [x] Use cases traced in Table 13.

## Document References

- `001-USE-CASES.md`
