# Order Intake — Central Plan Index

> **Standards Version:** 2.1.0  
> **Document Status:** DRAFT  
> **Last Updated:** 2026-11-06  
> **Source Revision:** rev-tdd-v1

## Table 2 — Execution Order

| Order | Story ID | Component | Phase | Use Cases | Title | Dependencies | Unlocks | Parallel With | Status |
|---|---|---|---|---|---|---|---|---|---|
| 1 | ST-1.1 | CMP-01 — Order Service | 1 | UC-01 | Domain model | None | ST-1.2 | ST-2.1 | NOT_STARTED |
| 2 | ST-1.2 | CMP-01 — Order Service | 1 | UC-01 | Persist orders | ST-1.1 | — | — | NOT_STARTED |
| 3 | ST-2.1 | CMP-02 — Notification Service | 2 | UC-02 | Notify customer | None | — | ST-1.1 | NOT_STARTED |

## Table 9 — Risk Register

| # | Risk | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
| R1 | Duplicate payment authorisation | Medium | High | Idempotency keys | Platform team |

## Table 10 — Milestones

| # | Milestone | Criteria | Target | Status |
|---|---|---|---|---|
| M1 | Orders persisted | ST-1.1 and ST-1.2 complete | — | NOT_STARTED |

### M1: Orders persisted

- Exit criteria: ST-1.1 and ST-1.2 are COMPLETED with integration evidence.
