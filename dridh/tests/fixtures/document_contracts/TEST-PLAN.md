# Order Intake — Test Plan

> **Standards Version:** 2.1.0  
> **Document Status:** DRAFT  
> **Last Updated:** 2026-11-06

## Table 15 — Test Phase Plan

| Test Phase ID | Test Scope | Use Case / Flow IDs | Component IDs | Dependency Mode | Environment | Execution Command | Evidence | Status |
|---|---|---|---|---|---|---|---|---|
| S1 | Order placement journey | UC-01 (SF-01.1, SF-01.2) | CMP-01 | TESTCONTAINERS | Postgres 16 container with dynamic ports | uv run pytest tests/integration/test_order_placement.py | Report under verification/order-intake/s1 | NOT_STARTED |
| D1 | Cancellation with compose topology | UC-02 (SF-02.1) | CMP-01, CMP-02 | DOCKER_COMPOSE | Compose project dridh_order_intake_d1 | docker compose -p dridh_order_intake_d1 up --abort-on-container-exit | Compose logs under verification/order-intake/d1 | NOT_STARTED |
| X1 | Payment authorisation contract | EF-01.1 | CMP-01 | WIREMOCK_EXCEPTION | dependency: payment-gateway; contract: openapi/payment.yaml; owner: Payments team; expires: 2027-01-31 | uv run pytest tests/contract/test_payment.py | Contract report under verification/order-intake/x1 | NOT_STARTED |
