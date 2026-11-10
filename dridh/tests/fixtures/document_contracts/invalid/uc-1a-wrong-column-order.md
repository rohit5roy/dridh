# Order Intake — Use Cases

> **Standards Version:** 2.1.0  
> **Document Status:** DRAFT  
> **Last Updated:** 2026-11-06  
> **Feature:** Order Intake

## Problem Statement and Objectives

Customers need orders captured and paid for reliably.

## Actors Registry

| Actor ID | Type | Actor Name | Status | Description |
|---|---|---|---|---|
| ACT-01 | Customer | USER | EXISTING | Places and cancels orders |
| ACT-02 | Payment Service | SERVICE | NEW | Authorises payments |

## Use Case Summary Matrix

| ID | Use Case | Primary Actor | Trigger | Category |
|---|---|---|---|---|
| UC-01 | Place an order | ACT-01 | Customer submits a basket | Core |
| UC-02 | Cancel an order | ACT-01 | Customer requests cancellation | Core |

## Detailed Use Cases

### UC-01: Place an order

- **Objective:** A customer places a paid order.
- **Trigger:** Customer submits a basket.

| Flow ID | Type | Origin Step | Subject | Actor ID | Action | Result | Priority |
|---|---|---|---|---|---|---|---|
| SF-01.1 | SUCCESS | — | Customer submits basket | ACT-01 | Submit basket | Order request accepted | HIGH |
| SF-01.2 | SUCCESS | — | Payment is authorised | ACT-02 | Authorise payment | Payment recorded | HIGH |
| AF-01.1 | ALTERNATIVE | SF-01.1 | Customer saves basket for later | ACT-01 | Save basket | Basket stored | MEDIUM |
| EF-01.1 | EXCEPTION | SF-01.2 | Payment is declined | ACT-02 | Return decline outcome | Order is not placed | HIGH |
| BE-01.1 | BOUNDARY | SF-01.1 | Basket is empty | ACT-01 | Block submission | No order is created | HIGH |
| BR-01.1 | BUSINESS_RULE | SF-01.2 | Order is over the review limit | ACT-02 | Hold for review | Order is held | HIGH |

### UC-02: Cancel an order

| Flow ID | Type | Origin Step | Subject | Actor ID | Action | Result | Priority |
|---|---|---|---|---|---|---|---|
| SF-02.1 | SUCCESS | — | Customer requests cancellation | ACT-01 | Submit cancellation | Cancellation is recorded | HIGH |
| EF-02.1 | EXCEPTION | SF-02.1 | Order has already shipped | ACT-01 | Reject cancellation | Order is unchanged | HIGH |

## Assumptions and Constraints

- Payments are authorised by an external service.

## Open Questions / Q&A Log

| # | Question | Asked In | Answer | Source | Impact |
|---|---|---|---|---|---|
| 1 | Is there a review limit? | Ideator | Yes, see BR-01.1 | Product owner | Affects UC-01 |
