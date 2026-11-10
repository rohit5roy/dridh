# TABLE-STANDARDS.md — Dridh Harness Canonical Document and Table Contract

> **Standards Version:** 2.1.0  
> **Updated:** 2026-11-05  
> **Applies to:** Dridh Ideator, Technical Designer, Planner, Executor, Coordinator, Code Reacher, review workflows, and all generated planning documents.  
> **Purpose:** Defines the canonical Markdown document structures, exact table headers, IDs, statuses, traceability rules, review records, and machine-validation requirements used by the Dridh Harness.

---

## 1. Authority, scope, and enforcement

This file is the canonical formatting and schema contract for documents generated or maintained by Dridh. Python validators parse these structures. Agents MUST follow the exact table headers, column order, ID formats, heading formats, and controlled status values defined here.

### 1.1 Source-of-truth hierarchy

1. **Approved `001-USE-CASES.md`:** authoritative record of agreed user/business behaviour and scope.
2. **Approved `<feature>_tdd.md`:** authoritative technical design that traces approved use cases to components and technical choices.
3. **Approved `CENTRAL-PLAN-INDEX.md` and component plans:** authoritative implementation decomposition derived from the approved use cases and TDD.
4. **`LEDGER.md`:** the only permitted mechanism for recording an authorised deviation from the approved use cases. It records the original requirement and the disposition; it does not silently rewrite the use-case source of truth.
5. **SQLite Coordinator state:** authoritative machine-readable workflow, relationship, validation, review, and synchronization state. Markdown remains the human-readable representation and must be reconciled with the database.

A generated document does not become approved merely because an agent created it, a validator passed, Jev returned a positive decision, or a readiness score reached its threshold. Human approval is required at the defined handoff gates.

### 1.2 General rules

- Keep documents self-contained where the TDD template asks for this; avoid replacing required content with links to separate documents.
- Use the exact filenames defined by the Dridh workflow. Do not invent alternate names for canonical artefacts.
- Use declarative language and explicit ordering where sequence matters.
- Every table must use the exact header and column order in this standard.
- Do not add, remove, rename, or reorder columns. A schema change requires a standards-version change, validator update, and migration plan.
- Use `—` for a not-applicable or intentionally empty table cell unless a table-specific rule says `None` or `N/A`.
- Use `N/A` when an entire section or capability does not apply. Keep required headings even when the content is `N/A`.
- In Markdown table cells, escape literal pipes as `\|`. Use `<br>` for intentional line breaks inside a cell. Do not create nested Markdown tables inside a table cell.
- Do not put unescaped newline characters inside a table row.
- Dates use ISO format `YYYY-MM-DD`. Timestamps in machine-readable JSON use ISO 8601 with timezone.
- IDs are case-sensitive and must match the exact uppercase format.
- ID references must resolve to exactly one definition, subject to the explicit row-based exceptions in Section 5.
- Do not use an LLM-generated table as the sole source of truth for workflow state. Coordinator parses, validates, stores, and renders the canonical state.

### 1.3 Required document metadata

Every canonical generated document MUST include a short metadata block near the top:

```markdown
> **Standards Version:** 2.1.0  
> **Document Status:** DRAFT  
> **Last Updated:** YYYY-MM-DD
```

Where applicable, include `Feature`, `Component`, `Owner`, and `Source Revision`. Do not add metadata columns to the canonical tables unless defined below.

---

## 2. Canonical ID formats

IDs are stable identifiers, not display labels. An ID must never be reassigned to a different entity. Sequence numbers are allocated by the Coordinator or its controlled ID allocator, not independently by parallel agents.

| Entity | Canonical format | Regex | Examples | Definition location |
|---|---|---|---|---|
| Actor | `ACT-NN` | `ACT-\d{2}` | `ACT-01`, `ACT-12` | Actors Registry row in `001-USE-CASES.md` |
| Component | `CMP-NN` | `CMP-\d{2}` | `CMP-01`, `CMP-12` | `### CMP-NN: <Component Name>` in the Stage 2 TDD; component plans reference it |
| Use Case | `UC-NN` | `UC-\d{2}` | `UC-01`, `UC-15` | `### UC-NN: <Title>` in `001-USE-CASES.md` |
| Success Flow Step | `SF-NN.N` | `SF-\d{2}\.\d+` | `SF-01.1`, `SF-01.3` | Table 1B row under its parent UC |
| Alternative Flow | `AF-NN.N` | `AF-\d{2}\.\d+` | `AF-01.1`, `AF-07.3` | Flow row under its parent UC |
| Exception Flow | `EF-NN.N` | `EF-\d{2}\.\d+` | `EF-01.1`, `EF-07.2` | Flow row under its parent UC |
| Boundary Edge Case | `BE-NN.N` | `BE-\d{2}\.\d+` | `BE-01.1`, `BE-07.4` | Flow row under its parent UC |
| Business Rule | `BR-NN.N` | `BR-\d{2}\.\d+` | `BR-01.1`, `BR-07.2` | Flow row under its parent UC |
| Starling Story | `ST-P.S` | `ST-\d+\.\d+` | `ST-1.1`, `ST-3.6` | `## ST-P.S: <Title>` in a phase file |
| Extension Story | `<ABBREV>-N` | `[A-Z]{2,3}-\d+` | `DK-1`, `RX-3`, `LP-2` | `## <ID>: <Title>` in the applicable phase file |
| Prerequisite Story | `PRE-N` | `PRE-\d+` | `PRE-1`, `PRE-2` | `## PRE-N: <Title>` in a phase file |
| Cross-service Story | `XN` | `X\d+` | `X1`, `X2` | `## XN: <Title>` in the applicable plan |
| Milestone | `MN` | `M\d+` | `M1`, `M6` | `### MN: <Title>` in `CENTRAL-PLAN-INDEX.md` or `COORDINATOR.md` |
| Design Decision | `DD-N` | `DD-\d+` | `DD-1`, `DD-5` | `#### DD-N: <Title>` in the feature TDD |
| Component Design Decision | `CDD-N` | `CDD-\d+` | `CDD-1`, `CDD-5` | `#### CDD-N: <Title>` in `COMPONENT-MASTER.md` |
| Risk | `RN` | `R\d+` | `R1`, `R7` | First identified in Stage 2: `### Rn: <Title>` in the TDD; if first identified during Stage 3 planning: in `CENTRAL-PLAN-INDEX.md` |
| Ledger Entry | `LDG-NN` | `LDG-\d{2}` | `LDG-01`, `LDG-12` | `#### LDG-NN: <Title>` in `LEDGER.md` |
| Test Phase | `S/D/X + N` | `[SDX]\d+` | `S1`, `D6`, `X1` | `## S/D/XN: <Title>` in the test plan |

### 2.1 ID allocation rules

- `NN` means a zero-padded two-digit sequence where shown. `N` and `P.S` follow the exact patterns above.
- `ACT-NN`, `CMP-NN`, `UC-NN`, and `LDG-NN` are unique within their documented feature/workspace scope. The Coordinator database must additionally retain a stable internal primary key.
- Flow sequence numbers are unique within their parent use case and flow type. Example: `SF-01.1`, `AF-01.1`, and `EF-01.1` may coexist because their prefixes differ.
- Each sequential primary/success journey step MUST receive its own `SF-NN.N` ID. The UC parent ID is not reused as a Table 1B flow ID.
- `SF-NN.N`, `AF-NN.N`, `EF-NN.N`, `BE-NN.N`, and `BR-NN.N` encode the parent use-case number in the first numeric segment. For example, `SF-01.2` belongs to `UC-01`.
- Story IDs must be unique within the component/phase scope defined by the project. The Coordinator must reject collisions across indexed documents.
- A `CMP-NN` is allocated once per actionable component and reused consistently in the TDD, central plan, component master, stories, and traceability records.
- A story with no direct use-case origin must use `N/A` in the Use Case field and must be identified as infrastructure or operations work in the appropriate planning context.
- Never renumber an existing ID solely to make a document appear sequential. Retired IDs remain reserved.

---

## 3. Canonical status values

Status fields are controlled values. Explanatory text belongs in a separate description, notes field, or ledger entry; do not append free-form text to a status cell.

| Context | Allowed values | Meaning |
|---|---|---|
| Pipeline Progress (`COORDINATOR.md`) | `NOT_STARTED`, `IN_PROGRESS`, `COMPLETED`, `REVIEWED`, `REVIEWED (STALE)`, `N/A` | Progress of a pipeline stage or artefact |
| Story Status (phase files, `COMPONENT-MASTER.md`, Central Plan) | `NOT_STARTED`, `IN_PROGRESS`, `IN_REVIEW`, `COMPLETED`, `BLOCKED` | Implementation state of a story |
| Milestone Status | `NOT_STARTED`, `IN_PROGRESS`, `COMPLETED` | Milestone state |
| Document Review Status | `DRAFT`, `READY_FOR_REVIEW`, `IN_REVIEW`, `CHANGES_REQUESTED`, `APPROVED`, `REJECTED`, `STALE` | Human review lifecycle |
| Ledger Deviation Status | `DONE`, `DEVIATED`, `NOT_DONE`, `WONT_DO` | Disposition of an approved use-case requirement or flow |
| Validation Result | `PASS`, `FAIL`, `WARNING`, `NOT_RUN`, `BLOCKED` | Deterministic validation outcome |
| GHA / Build Status | `NOT_STARTED`, `IN_PROGRESS`, `PASSED`, `FAILED`, `CANCELLED`, `UNKNOWN` | CI workflow state as observed by Dridh |
| PR Status | `NOT_CREATED`, `DRAFT`, `OPEN`, `IN_REVIEW`, `APPROVED`, `MERGED`, `CLOSED` | Remote pull-request lifecycle, separate from story readiness |
| Test Status | `NOT_STARTED`, `IN_PROGRESS`, `PASSED`, `FAILED`, `BLOCKED`, `NOT_APPLICABLE` | Test phase or test execution result |
| Story Dependency Gate Status | `NOT_READY`, `TESTS_PASSED`, `PR_CREATED`, `GHA_PASSED`, `LOCALLY_INTEGRATED`, `INTEGRATION_PUSHED`, `INDEXED`, `DEPENDENTS_UNLOCKED`, `REMOTE_MERGED`, `BLOCKED` | Computed evidence/gate lifecycle, stored separately from Story Status and PR Status |

### 3.1 Story completion and dependency readiness are distinct

A story's remote PR lifecycle is not the same thing as whether that story can unlock its dependents.

For the Dridh dependency-unlock rule, a predecessor is eligible to unlock dependent stories only when all of the following are true:

1. Its GitHub PR has been created.
2. The story's required verification and integration tests have passed.
3. All required GitHub Actions workflows/checks have passed successfully.
4. The branch has been integrated into the local feature integration branch using the approved local rebase/merge strategy.
5. The shared `dridh/integration/<feature-slug>` branch has been pushed/fast-forwarded to `origin` and its exact commit SHA recorded.
6. Code Reacher has successfully re-indexed the integrated feature baseline.
7. Coordinator has reconciled the resulting evidence and confirmed there are no blocking dependency or validation failures.

**Remote PR approval and remote PR merge are not prerequisites for unlocking dependent stories.** The PR status remains visible and tracked independently. A story must not be marked `COMPLETED` merely because a PR was opened; its required verification and integration evidence must exist.

The Coordinator database should store a separate computed dependency-readiness state/evidence record rather than overloading the Markdown Story Status value. Allowed Story Dependency Gate Status values are `NOT_READY`, `TESTS_PASSED`, `PR_CREATED`, `GHA_PASSED`, `LOCALLY_INTEGRATED`, `INTEGRATION_PUSHED`, `INDEXED`, `DEPENDENTS_UNLOCKED`, `REMOTE_MERGED`, and `BLOCKED`. The Markdown tables display the canonical story status and the PR/Build columns separately. Regression tests must prove that changing Story Status, PR Status, Build Status, or Dependency Gate Status never implicitly mutates another state machine.

---

## 4. `001-USE-CASES.md` — Use Case Specification

The Use Case document is the single source of truth for agreed functional behaviour and scope. It must be sufficiently detailed for the Technical Designer to produce a traceable design without re-interviewing the user for facts already captured.

### 4.1 Required document section order

The generated document MUST contain these sections in this order:

1. Document metadata.
2. Problem Statement and Objectives.
3. Goals / What the solution must achieve.
4. Out of Scope.
5. Actors Registry (Table 1A).
6. Use Case Summary Matrix (Table 1).
7. Detailed Use Cases, in user-journey/action sequence (one `### UC-NN` section per row in Table 1).
8. Phase 2 / Future Scope Boundary, when applicable.
9. Assumptions and Constraints.
10. Open Questions / Q&A Log (Table 11).

If a section is not applicable, retain its heading and mark it `N/A` with a short reason.

### 4.2 Table 1A — Actors Registry

**Location:** `001-USE-CASES.md`, before the Use Case Summary Matrix.

**Exact header:**

```markdown
| Actor ID | Actor Name | Type | Status | Description |
|---|---|---|---|---|
```

**Rules:**

- `Actor ID` MUST use `ACT-NN`.
- `Type` MUST be one of `USER`, `SYSTEM`, or `SERVICE`.
- `Status` MUST be one of `EXISTING` or `NEW`.
- `Actor Name` must be a stable, unambiguous name.
- `Description` must explain the actor's responsibility or relationship to the feature.
- Every actor referenced by a use case or flow MUST be present in this registry.
- Do not represent an actor only by a free-text name in a flow row; use its `ACT-NN` identifier.
- If the feature has no separate external or internal actor to register, retain the table and include a single `—` row.

### 4.3 Table 1 — Use Case Summary Matrix

**Exact header:**

```markdown
| ID | Use Case | Primary Actor | Trigger | Category |
|---|---|---|---|---|
```

**Rules:**

- `ID` contains `UC-NN` only.
- `Use Case` is a concise outcome-oriented title.
- `Primary Actor` contains a registered `ACT-NN`.
- `Trigger` explains the event or user action that starts the use case.
- `Category` is a concise project classification. The source requirements do not define a closed category vocabulary; do not invent a mandatory enum. Keep category wording consistent within a feature.
- Every row MUST have exactly one corresponding `### UC-NN: <Title>` section.
- Every `### UC-NN` section MUST have exactly one row in Table 1.
- Order rows by the intended user journey or sequence of business actions, not by the order in which the agent happened to discover them.
- The Phase 2 boundary table uses the exact header `| Future UC ID | Use Case (Phase 2) | Notes |`.

### 4.4 Detailed use-case section

Every summary row MUST have a matching section with this heading format:

```markdown
### UC-NN: <Use Case Title>
```

Each use case section MUST include:

- **Objective:** the user/business outcome.
- **Preconditions:** conditions that must already hold.
- **Trigger:** the event or action that begins the use case.
- **Primary / Success Flow:** ordered steps describing the normal journey.
- **Postconditions:** observable state/result after successful completion.
- **Alternative, Exception, Boundary, and Business Rule flows:** include all applicable branches.
- **Priority:** business/product priority for the use case and each sub-flow.
- **Open questions / assumptions:** only where still unresolved.

### 4.5 Table 1B — Per-Use-Case Flow Table

**Location:** inside each `### UC-NN` section, after preconditions/context and before any narrative notes that explain the flows.

**Exact header:**

```markdown
| Flow ID | Type | Origin Step | Subject | Actor ID | Action | Result | Priority |
|---|---|---|---|---|---|---|---|
```

**Primary success journey representation:**

- The primary/success journey MUST be represented as one row per sequential step, not compressed into one row.
- Each primary step receives a unique `SF-NN.N` ID whose parent number matches the enclosing UC. Example: `SF-01.1`, `SF-01.2`, `SF-01.3` belong to `UC-01`.
- Table row order defines the primary journey execution order. The sequence suffix MUST increase in journey order; gaps are allowed only when an earlier step has been retired and its ID remains reserved.
- Every success-step row contains exactly one `ACT-NN` Actor ID. If more than one actor participates in a logical step, split it into separately ordered steps where the sequence is materially relevant.
- A use case with one primary step still uses `SF-NN.1`; do not use the parent `UC-NN` as a flow ID.

**Flow IDs and types:**

- `Type` MUST be one of `SUCCESS`, `ALTERNATIVE`, `EXCEPTION`, `BOUNDARY`, or `BUSINESS_RULE`.
- Prefix and Type MUST agree: `SF` → `SUCCESS`, `AF` → `ALTERNATIVE`, `EF` → `EXCEPTION`, `BE` → `BOUNDARY`, `BR` → `BUSINESS_RULE`.
- Every `SF-NN.N`, `AF-NN.N`, `EF-NN.N`, `BE-NN.N`, and `BR-NN.N` ID is unique within the feature and must map to exactly one parent UC.
- `Origin Step` for `SUCCESS` rows is `—`.
- `Origin Step` for an alternate/exception/boundary/business-rule branch MUST reference the exact primary step ID (`SF-NN.N`) from which it branches. Use `—` only when the branch has no single originating primary step; explain why in the row's Subject or nearby notes.
- Do not use an integer-only origin reference such as `2`; use the canonical ID `SF-01.2` so that it is unambiguous and referentially valid.

**Column rules:**

- `Subject` is the scenario/condition being handled.
- `Actor ID` MUST reference an `ACT-NN` in Table 1A.
- `Action` states what the actor does or what the system does in response.
- `Result` states the observable outcome, including error/compensation behaviour where applicable.
- `Priority` must be populated consistently using the project's agreed priority vocabulary. Do not silently impose a new priority enum where the product has not defined one.
- Each flow row must have enough detail to be independently traced to design and implementation work.

**Illustrative multi-step example (format only; replace all values in a generated use-case document):**

```markdown
| Flow ID | Type | Origin Step | Subject | Actor ID | Action | Result | Priority |
|---|---|---|---|---|---|---|---|
| SF-01.1 | SUCCESS | — | Customer submits order | ACT-01 | Submit order details | Order request is accepted for processing | HIGH |
| SF-01.2 | SUCCESS | — | Payment is authorised | ACT-02 | Authorise payment | Payment authorisation is recorded | HIGH |
| SF-01.3 | SUCCESS | — | Inventory is reserved | ACT-03 | Reserve requested inventory | Inventory reservation is recorded | HIGH |
| EF-01.1 | EXCEPTION | SF-01.2 | Payment is declined | ACT-02 | Return payment decline outcome | Order does not proceed to inventory reservation | HIGH |
```

### 4.6 Phase 2 / future-scope boundary table

When a requirement is explicitly deferred, use:

```markdown
| Future UC ID | Use Case (Phase 2) | Notes |
|---|---|---|
```

Use `—` for a future ID not yet allocated. Do not allocate a current-phase `UC-NN` to an unapproved future-scope item.

---

## 5. `CENTRAL-PLAN-INDEX.md` — Central Implementation Plan

The Central Plan Index is the feature-wide execution map. It maps use cases and components to stories, dependencies, unlocked stories, parallel work, milestones, and risks.

### 5.1 Table 2 — Execution Order (expanded canonical schema)

**Exact header — all ten columns are mandatory:**

```markdown
| Order | Story ID | Component | Phase | Use Cases | Title | Dependencies | Unlocks | Parallel With | Status |
|---|---|---|---|---|---|---|---|---|---|
```

This expanded schema replaces the previous seven-column Table 2. It is the sole canonical Table 2; do not emit a second alternative Table 2B.

**Rules:**

- `Order` is a sequential integer beginning at 1 and follows a valid topological execution order.
- `Story ID` uses a registered story ID format from Section 2.
- `Component` contains `CMP-NN — <Component Name>`. The component must have a matching component definition.
- `Phase` is a positive integer (1, 2, 3, ...).
- `Use Cases` is a comma-separated list of `UC-NN` values and may include associated flow IDs in the relevant story card. Use `—` for infrastructure-only stories and `Ops` for operational stories.
- `Title` matches the story section heading and story card title.
- `Dependencies` is a comma-separated list of predecessor Story IDs, or `None` when no dependency exists.
- `Unlocks` is a comma-separated list of direct dependent Story IDs, or `—` when the story unlocks none. This column is derived from the reverse dependency relationship in Coordinator state and must agree with `Dependencies` elsewhere.
- `Parallel With` is a comma-separated list of stories that can execute concurrently with this story, or `—` when none. Do not list a story as parallel when it is an ancestor or descendant in the dependency graph.
- `Status` uses the Story Status values in Section 3.
- Every Story ID must occur exactly once in this table.
- Every dependency and unlock reference must resolve to a story definition.
- The dependency graph MUST be acyclic. Coordinator must reject circular dependencies before the plan is approved.
- `Order` must be compatible with dependency direction: every predecessor appears before its dependent story.
- The canonical graph is stored in SQLite. Markdown is validated against and rendered from that state; independent edits must be reconciled before approval.

### 5.2 Table 9 — Risk Register

**Exact header:**

```markdown
| # | Risk | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
```

**Rules:**

- `#` contains a unique Risk ID (`R1`, `R2`, ...).
- Every risk ID must have exactly one definition heading: `### R1: <Risk Title>`.
- A risk first identified during Stage 2 is defined in the TDD under `## Risks`; the Central Plan Table 9 row references that definition and does not duplicate it.
- A risk first identified during Stage 3 planning may be defined in `CENTRAL-PLAN-INDEX.md`. The Coordinator's feature-wide index must enforce exactly one definition across the TDD and Central Plan.
- `Likelihood` and `Impact` use the project's declared scale (for example, Low/Medium/High). Declare the scale before using it; do not mix scales within one document.
- `Mitigation` must describe a concrete prevention, detection, or recovery action.
- `Owner` must name a responsible role or person; use `—` only when genuinely unassigned and record the gap as an open question.

### 5.3 Table 10 — Milestones

**Exact header:**

```markdown
| # | Milestone | Criteria | Target | Status |
|---|---|---|---|---|
```

**Rules:**

- `#` contains `M1`, `M2`, ...
- Every milestone must have one definition heading: `### M1: <Milestone Title>`.
- `Criteria` must be objectively verifiable.
- `Target` uses `YYYY-MM-DD` or `—` when unknown.
- `Status` uses the Milestone Status values in Section 3.

---

## 6. Component planning documents

Each actionable component has its own directory and `COMPONENT-MASTER.md`. Component implementation phases are maintained in separate phase files.

### 6.1 Component definition and Stage 2 identity allocation

The canonical definition of every actionable `CMP-NN` component MUST be created during Stage 2 in the feature TDD, under `### Component Responsibilities`, using this heading:

```markdown
### CMP-NN: <Component Name>
```

The TDD is generated and validated before Planner creates `COMPONENT-MASTER.md`; therefore, a component ID MUST NOT depend on a Stage 3 file to resolve during Stage 2 validation.

The component definition must identify its responsibility, boundaries, owner/owning team where known, existing/new status, associated use cases/flows, dependencies, and major contracts.

During Stage 3, each component's `COMPONENT-MASTER.md` MUST reference the already allocated `CMP-NN` and the canonical TDD definition. It MUST NOT create a second canonical `### CMP-NN:` definition heading. Use this metadata block (plain Markdown fields, not a new canonical table):

```markdown
## Component Overview

- **Component ID:** CMP-01
- **Canonical TDD Definition:** `<feature>_tdd.md#cmp-01-component-name`
- **Component Name:** <Component Name>
```

The component master may add descriptive component content, but it must not redefine the ID or conflict with the TDD's canonical responsibility/boundary statement.

The same ID must be reused consistently across the TDD, central plan, component master, phase stories, and Coordinator database. A newly discovered component during planning must be returned to the Technical Designer for TDD revision and Coordinator re-validation before planning proceeds with it.

### 6.2 Table 4 — Story Index (`COMPONENT-MASTER.md`)

**Exact header:**

```markdown
| Story ID | Phase | Story Name | Use Cases | Status |
|---|---|---|---|---|
```

**Rules:**

- `Story ID` is unique and must have a corresponding phase-file story heading.
- `Phase` is a positive integer.
- `Story Name` matches the phase story title.
- `Use Cases` is a comma-separated list of `UC-NN` IDs, `—` for infrastructure stories, or `Ops` for operational stories.
- `Status` uses Story Status values.
- Every story in the component's phase files must appear exactly once in this index, and vice versa.

### 6.3 Table 5 — Phase Status Dashboard (`COMPONENT-MASTER.md`)

**Exact header:**

```markdown
| Phase | Total Stories | NOT_STARTED | IN_PROGRESS | IN_REVIEW | COMPLETED | BLOCKED |
|---|---|---|---|---|---|---|
```

**Rules:**

- `Phase` is a positive integer.
- All count columns contain non-negative integers.
- `Total Stories` equals the sum of the five status counts for the phase.
- The last row MUST be the `**Total**` row.
- The total row must equal the sum of all phase rows.
- Coordinator must recalculate counts from the story index/database; agents must not guess or manually maintain totals.

### 6.4 Table 3 — Story Card (phase files)

Each story section MUST use this heading format:

```markdown
## <STORY-ID>: <Title>
```

Examples:

```markdown
## ST-2.1: Domain Model
## DK-3: Push Contract
## PRE-1: Provision SQS Queues
```

**Exact table header:**

```markdown
| Field | Value |
|:------|:------|
```

**Mandatory rows — exact field names and order:**

```markdown
| **Use Case ID** | <UC-NN or N/A or comma-separated UC IDs> |
| **Flow IDs** | <SF-NN.N / AF-NN.N / EF-NN.N / BE-NN.N / BR-NN.N IDs, or N/A> |
| **Title** | <title> |
| **Description** | <description> |
| **Status** | <NOT_STARTED / IN_PROGRESS / IN_REVIEW / COMPLETED / BLOCKED> |
| **Dependencies** | <comma-separated story IDs or None> |
| **Unlocks** | <comma-separated story IDs or —> |
| **What Will Be Done** | <numbered steps> |
| **Acceptance Criteria** | <observable, verifiable criteria> |
| **Definition of Done** | <DoD, including applicable test and evidence requirements> |
```

**Rules:**

- The field/value header and all ten mandatory rows are exact and ordered. Do not omit `Flow IDs` or `Unlocks`.
- `Use Case ID` identifies the originating use cases. Use `N/A` for infrastructure or operational stories with no use-case origin.
- `Flow IDs` identifies the precise originating flow-step IDs (`SF`, `AF`, `EF`, `BE`, `BR`). Do not put the parent `UC-NN` in this field; use `N/A` only when the story has no use-case flow origin.
- `Dependencies` and `Unlocks` must agree with Table 2 and the SQLite dependency graph.
- `What Will Be Done` contains ordered, actionable implementation steps. Use `<br>` between steps where needed to preserve the table row.
- `Acceptance Criteria` must be objectively testable and must trace to the associated use case/flow or explicit infrastructure requirement.
- `Definition of Done` must state implementation, validation, integration testing, documentation, and evidence expectations relevant to the story.
- The story's ID must occur in exactly one phase story heading and one component story index row.
- A story must be small enough for one Executor worker to own without conflicting with another concurrently executing story. Shared-file overlap must be called out in dependencies or a planning note.

### 6.5 Component design decisions

Component-level design decisions use `CDD-N`, never `DD-N`.

Heading format:

```markdown
#### CDD-N: <Decision Title>
```

`DD-N` is reserved for feature-wide architectural decisions in the TDD. This avoids collisions between feature-level and component-level decisions.

---

## 7. TDD (`<feature>_tdd.md`) standards

The TDD must follow `Tech-Design-Template.md` and map the approved use cases to the system/components and technical design. It must be self-contained enough for reviewers and engineers with limited project context.

### 7.1 Required template handling

- Preserve the template's main sections and their order.
- Remove all Notion instructional `<aside>...</aside>` blocks from the generated TDD, including their embedded tooltip text.
- Do **not** remove the optional technical overview sections at the bottom of the template.
- Retain unused optional sections and mark them `N/A` with a short explanation.
- Do not remove the optional `AI Systems` section. Mark it `N/A` when the feature does not introduce a production ML/AI system.
- Keep the TDD's required headings even when their content is not applicable.
- Include the agreed use-case traceability table under `## Design Details`.

### 7.2 Table 13 — Use Case-to-Design Traceability Matrix

**Location:** under `## Design Details` in the feature TDD.

**Exact header:**

```markdown
| UC ID | Flow IDs | Catering Component(s) | API / Event | Design Decision (DD-N) |
|---|---|---|---|---|
```

**Rules:**

- Every approved `UC-NN` must appear in at least one row.
- `Flow IDs` lists the flows covered by the row; do not imply that a flow is covered if the design does not address it.
- `Catering Component(s)` lists one or more `CMP-NN` IDs and component names.
- `API / Event` lists the relevant API or event name/specification reference, or `N/A` when none applies.
- `Design Decision (DD-N)` lists relevant feature-wide design decision IDs, or `N/A` when no separate decision applies.
- If one UC is addressed by multiple distinct components or technical mechanisms, use multiple rows rather than compressing unrelated mappings into an ambiguous cell.
- Every referenced `DD-N` must have a heading definition in the TDD.
- Coordinator validates coverage from every approved UC/flow to the relevant technical-design mapping. Unmapped requirements are blocking findings unless explicitly handled through the Ledger.

### 7.3 Design decisions

Feature-level design decisions use:

```markdown
#### DD-N: <Decision Title>
```

Each decision should include, at minimum:

- Context/problem.
- Decision.
- Alternatives considered where material.
- Rationale and consequences/trade-offs.
- Affected components and use cases.

### 7.4 Open questions and assumptions

All planning documents, including the TDD, use the canonical Table 11 Q&A Log in Section 10. The older four-column example shown inside the source TDD template is not the canonical Dridh schema.

---

## 8. `COORDINATOR.md` standards

`COORDINATOR.md` is a rendered human-readable overview of the workspace/feature pipeline. SQLite Coordinator state is authoritative; the Markdown overview must be generated or reconciled by the state-manager renderer.

### 8.1 Table 6 — Pipeline Progress

**Exact header:**

```markdown
| # | Stage | Agent | Status | Output | Date Completed | Reviewed By |
|---|---|---|---|---|---|---|
```

**Rules:**

- `#` is a sequential integer from 1 through 10.
- The `Stage` and `Agent` names must match the configured Dridh lifecycle stages/agent registry.
- `Status` uses Pipeline Progress values.
- `Output` is a backtick-wrapped workspace-relative file path or `—`.
- `Date Completed` is `YYYY-MM-DD` or `—`.
- `Reviewed By` is the human reviewer name or `—`.
- `REVIEWED (STALE)` means the artefact changed after the review and must be reviewed again before a downstream approval gate can rely on it.

### 8.2 Table 7 — Design Artifacts

**Exact header:**

```markdown
| Document | Path | Status | Lines | Key Metrics |
|---|---|---|---|---|
```

**Rules:**

- `Document` is the canonical filename.
- `Path` is a workspace-relative path.
- `Status` uses Document Review Status or Pipeline Progress as appropriate; do not mix meanings within one rendered table. The renderer must document which status vocabulary is being displayed.
- `Lines` is a non-negative integer or `—` if not measured.
- `Key Metrics` contains concise factual counts, such as use-case count, flow count, component count, story count, or blocker count.

### 8.3 Table 8 — Implementation Progress

**Exact header:**

```markdown
| Component | Phase | Branch | PR # | PR Status | Build Status | Stories Done | Stories Total |
|---|---|---|---|---|---|---|---|
```

**Rules:**

- `Component` contains `CMP-NN — <Component Name>`.
- `Phase` is a positive integer or `—` when not applicable.
- `Branch` is the exact local/remote branch name or `—`.
- `PR #` is the GitHub PR number or `—`.
- `PR Status` uses PR Status values from Section 3.
- `Build Status` uses GHA / Build Status values from Section 3.
- `Stories Done` and `Stories Total` are non-negative integers; `Stories Done` must not exceed `Stories Total`.
- PR approval/merge status must not be used as a proxy for story dependency readiness. Apply Section 3.1.

### 8.4 Table 12 — Review Quality (rendered)

**Rendered by:** Coordinator state-manager from the review store/database.

**Exact header:**

```markdown
| Document | Readiness | Checklist | Sections | Blockers | Last Reviewer |
|---|---|---|---|---|---|
```

**Column definitions:**

| Column | Format | Description |
|---|---|---|
| Document | `filename.md` | Canonical document filename |
| Readiness | `N/100` | Deterministic/defined quality-readiness score from 0 to 100 |
| Checklist | `N/M` | Checked checklist items / total required checklist items |
| Sections | `N/M` | Approved sections / total reviewable sections |
| Blockers | Integer | Count of unresolved blocking comments/findings |
| Last Reviewer | Name or `—` | Last human reviewer |

**Rules:**

- A readiness score of at least 90/100 is the quality threshold stated by the original standards.
- Readiness is not human approval and cannot approve a document.
- Jev's score/decision is advisory and cannot substitute for a human decision.
- A document can pass deterministic validation and still require human review.
- Human approval requires the checklist to be complete and an explicit approval action by an authorised human.
- A document changed after approval becomes stale; the approval must not be silently carried forward to the changed content.
- The source requirements do not define a human override policy for a readiness score below 90. Do not invent one; record a policy decision as an open question if a use case requires it.

---

## 9. Human-in-the-loop review and approval

Dridh uses a CLI/Markdown review workflow. It does not require a web Review UI for the initial implementation.

### 9.1 Review artefact

For each review gate, generate a workspace-local `REVIEW-CHECKLIST.md` adjacent to the artefact or in the canonical reviews directory. It must include:

- Document path and document type.
- Standards version.
- Document revision or content hash.
- Generated-at date.
- Deterministic validation summary.
- Readiness score and unresolved blocker count.
- Checklist items derived from the problem statement, use cases, TDD, or implementation plan as applicable.
- An explicit human decision section.

Use Markdown checkboxes for checklist items:

```markdown
## Review Checklist

- [ ] All required use cases and flows are represented.
- [ ] Actor references resolve to the Actors Registry.
- [ ] Scope and out-of-scope statements are clear.
- [ ] Open questions are resolved or explicitly accepted.

## Human Decision
- Reviewer: —
- Decision: PENDING
- Reviewed At: —
- Comments: —
```

The items above are examples; actual checklist items must be generated from the artefact's content and stage-specific criteria.

### 9.2 Approval rules

- Checking every box does not itself approve the document.
- A human must explicitly approve through the Dridh review command/workflow.
- Approval must record reviewer identity, time, document path, document content hash/revision, checklist completion, and decision.
- The Coordinator must reject approval if required checklist items remain unchecked or if blocking validation issues remain.
- Any document modification after approval invalidates that approval and marks its review state stale.
- The next workflow stage cannot start until its required source document has an active human approval record.
- Jev cannot approve, reject, waive, or override a human review decision.

The exact CLI command names may be implemented by the CLI phase, but the semantics must be equivalent to `review show`, `review validate`, and an explicit `review approve` operation. A command must never infer human approval from a file timestamp or an all-checked checklist alone.

---

## 10. Table 11 — Q&A Log (all planning documents)

Use one canonical Q&A schema throughout use-case documents, TDDs, implementation plans, and component planning documents.

**Exact header:**

```markdown
| # | Question | Asked In | Answer | Source | Impact |
|---|---|---|---|---|---|
```

**Rules:**

- `#` is a sequential integer beginning at 1 within the document.
- `Question` records the question as asked, without changing its meaning.
- `Asked In` identifies the stage, section, agent, or session where it arose.
- `Answer` contains the agreed answer, `OPEN`, or `N/A` with a reason.
- `Source` identifies the human, document, system, or evidence that supplied the answer.
- `Impact` explains what the answer changes or `None` if no impact.
- An unanswered material question must remain visible as `OPEN`; do not fabricate an answer.
- If a question changes an approved requirement, the Coordinator must determine whether a new approval or Ledger entry is required.

The older four-column TDD example and the alternative short use-case Q&A table are superseded by this canonical six-column header.

### 10.1 Inquirer handoff artefacts

When an Inquirer completes an interview, it must produce both:

1. A human-readable Markdown staging document using the complete applicable target-document section/table schema, not a free-form meeting summary. Ideator notes use the same Actors Registry, Use Case Summary Matrix, per-UC sections, Table 1B flow table, and Table 11 formats as `001-USE-CASES.md`. Technical Designer notes use the corresponding TDD section structure, component definitions, Table 13, and Table 11 formats. These files are staging drafts and MUST NOT be mistaken for the approved canonical target documents.
2. A machine-readable JSON Q&A log containing the complete question/answer history.

The JSON log must preserve, at minimum:

- `schema_version`
- `workflow_id`
- `stage`
- `agent_id`
- `session_id`
- `created_at`
- `questions[]`, with each question's stable ID, question text, answer, source, impact, timestamp, unresolved flag, and related UC/flow IDs where known.

The JSON and Markdown representations must be cross-checked by the Coordinator. The JSON log must not contain credentials or unrelated secrets. The canonical workspace path is under `.opencode/dridh/state/` or the configured equivalent; the exact subdirectory is controlled by the workspace configuration.

---

## 11. Table 14 — Coordinator Ledger (`LEDGER.md`)

`LEDGER.md` is a dedicated document. It is the only authorised mechanism for recording a deviation from the approved `001-USE-CASES.md` source of truth.

**Exact header:**

```markdown
| Ledger ID | UC / Flow ID | Deviation Status | Linked Story ID | Original Requirement | Actual / Deviated Behavior | Justification | Approved By |
|---|---|---|---|---|---|---|---|
```

**Rules:**

- `Ledger ID` uses `LDG-NN`.
- Every Ledger ID must have one story-style definition heading below the table:

```markdown
#### LDG-NN: <Ledger Entry Title>
```

- `UC / Flow ID` identifies the originating approved use case or flow.
- `Deviation Status` is one of `DONE`, `DEVIATED`, `NOT_DONE`, or `WONT_DO`.
- `Linked Story ID` is a valid story ID or `—` when no implementation story exists.
- `Original Requirement` must preserve the original requirement/flow meaning; do not rewrite it to match the deviation.
- `Actual / Deviated Behavior` states what was implemented, not implemented, or explicitly excluded.
- `Justification` explains why the disposition is necessary.
- `Approved By` must identify the approving human for `DEVIATED`, `NOT_DONE`, and `WONT_DO`. Use `—` for `DONE` only when no separate approval is required by the project's policy.
- A `DEVIATED`, `NOT_DONE`, or `WONT_DO` entry without the required human approval is not an authorised deviation and must not unblock a downstream stage that depends on the affected requirement.
- Ledger records must be append-only in meaning. Corrections should preserve an audit trail rather than silently deleting historical decisions.
- The CLI MUST expose a deterministic human approval operation, for example `dridh ledger approve LDG-01 --reviewer "<human identity>"`. The command must require an interactive TTY confirmation (or a separately configured trusted human-authentication mechanism); `--reviewer` is attribution metadata, not proof of human presence. Agent tools and non-interactive agent subprocesses MUST NOT be able to approve Ledger entries.
- A corresponding rejection operation must record reviewer, timestamp, reason, and audit event. Pending/rejected entries remain unauthorised and cannot satisfy downstream deviation gates.
- Coordinator synchronises Ledger entries to SQLite and validates that every referenced UC/flow/story exists.

### 11.1 Ledger entry detail

Each `#### LDG-NN` section should include:

- Originating UC/flow IDs.
- Original requirement.
- Disposition (`DONE`, `DEVIATED`, `NOT_DONE`, or `WONT_DO`).
- Actual behaviour or reason for non-delivery.
- Impacted components/stories.
- Justification and alternatives considered where relevant.
- Human approver and approval date where approval is required.
- Links to evidence/PRs/tests when available.

---

## 12. Table 15 — Test Phase Plan

The Planner's Testing Planner must produce the feature-level `TEST-PLAN.md` in the workspace documents directory and, where appropriate, component-level test phase plans that reference its Test Phase IDs. The overall plan must map test phases to approved use cases/flows and identify the real dependencies required for execution. The deterministic `validate-test-plan.py` validator is mandatory.

**Exact header:**

```markdown
| Test Phase ID | Test Scope | Use Case / Flow IDs | Component IDs | Dependency Mode | Environment | Execution Command | Evidence | Status |
|---|---|---|---|---|---|---|---|---|
```

**Rules:**

- `Test Phase ID` uses `S1`, `D1`, `X1`, etc. (`[SDX]\d+`).
- `Test Scope` describes the integration or user-journey behaviour being tested.
- `Use Case / Flow IDs` lists the use cases/flows under test, or `N/A` for infrastructure tests.
- `Component IDs` lists registered `CMP-NN` IDs.
- `Dependency Mode` must be one of:
  - `TESTCONTAINERS` — preferred approach for real dependent services.
  - `DOCKER_COMPOSE` — for dependencies requiring an explicit Compose topology.
  - `REAL_QA_ENDPOINT` — real QA service endpoint/host when Docker execution is not practical.
  - `WIREMOCK_EXCEPTION` — last-resort interface simulation, only where neither Testcontainers/Docker nor a real QA endpoint can be used.
- `Environment` identifies the test environment and required configuration, without exposing secrets.
- `Execution Command` provides the reproducible command or script entry point.
- `Evidence` identifies reports, logs, screenshots where appropriate, container output, or CI run references.
- `Status` uses Test Status values.

### 12.1 No-mocks testing policy

- The default is real integration testing, not application/service mocks.
- Prefer Testcontainers to run real dependency services.
- Use Docker Compose when a dedicated multi-service topology is required.
- If a dependency cannot run in Docker, use a real QA URL/host where available.
- WireMock is a last resort only when a real service/QA endpoint and containerised dependency are not viable. The exception must identify the missing real dependency, the interface/OpenAPI contract used, why other options failed, affected tests, owner, and approval/expiry policy. Do not silently label WireMock tests as full end-to-end integration tests.
- A test plan must explicitly identify any `WIREMOCK_EXCEPTION` row.
- User-journey tests must be derived from the UC success, alternative, exception, boundary, and business-rule flows.

### 12.2 Parallel Docker isolation

For concurrent Executor worktrees:

- Prefer Testcontainers with isolated containers and dynamically allocated host ports.
- If Docker Compose is used, every worktree/branch must have a unique Compose project name and a unique Docker network derived from a sanitised branch/story identifier.
- Never rely on fixed host ports where concurrent workers may collide.
- Use deterministic sanitisation for names, for example `feature/ST-1.1` → `dridh_st_1_1`.
- Compose network names must be unique to the branch/worktree, for example `dridh_st_1_1_network`.
- Labels should identify workspace, story, worktree, and execution run to support cleanup.
- Cleanup must run on success, failure, cancellation, and timeout; cleanup failures must be reported.

### 12.3 New mock detection in Executor changes

The Coordinator/Code Reacher test gate must compare newly added/modified test code against the correct base revision. Existing historical mock usage must not be treated as a new violation merely because it appears in the repository.

New prohibited mocking additions (for example Mockito, `jest.mock`, `unittest.mock`, or `@MockBean`) should be reported with file/line and context. The Executor should be asked to correct them through the normal bounded repair loop; this finding should not cause an uninformative permanent workflow failure. If it remains unresolved after the configured three attempts, escalate to the human.

---

## 13. Traceability and linkability rules

Every identifier referenced in a table cell, paragraph, list item, or diagram must resolve to one canonical definition within the feature/workspace, except for the explicit row-based entities below.

### 13.1 Definition locations

| ID Prefix | Scope | Defined In | Canonical definition |
|---|---|---|---|
| `ACT-NN` | Feature-wide | `001-USE-CASES.md` | Table 1A Actors Registry row |
| `CMP-NN` | Feature-wide/component | `<feature>_tdd.md` (Stage 2); referenced by component master in Stage 3 | `### CMP-NN: <Component Name>` in TDD; component master references the ID without redefining it |
| `UC-NN` | Feature-wide | `001-USE-CASES.md` | `### UC-NN: <Title>` |
| `SF-NN.N` | Per-UC | `001-USE-CASES.md` | Table 1B success-step row under parent UC |
| `EF-NN.N` | Per-UC | `001-USE-CASES.md` | Table 1B flow row under parent UC |
| `AF-NN.N` | Per-UC | `001-USE-CASES.md` | Table 1B flow row under parent UC |
| `BE-NN.N` | Per-UC | `001-USE-CASES.md` | Table 1B flow row under parent UC |
| `BR-NN.N` | Per-UC | `001-USE-CASES.md` | Table 1B flow row under parent UC |
| `DD-N` | Feature-wide | `<feature>_tdd.md` | `#### DD-N: <Title>` |
| `CDD-N` | Per-component | `COMPONENT-MASTER.md` | `#### CDD-N: <Title>` |
| `ST-P.N` | Per-component | Component phase file | `## ST-P.N: <Title>` |
| Extension story ID | Per-component | Component phase file | `## <ID>: <Title>` |
| `PRE-N` | Per-component | Component phase file | `## PRE-N: <Title>` |
| `XN` | Feature/cross-service | Relevant plan | `## XN: <Title>` |
| `MN` | Feature-wide | `CENTRAL-PLAN-INDEX.md` or `COORDINATOR.md` | `### MN: <Title>` |
| `RN` | Feature-wide | `<feature>_tdd.md` when first identified in Stage 2; otherwise `CENTRAL-PLAN-INDEX.md` when first identified in Stage 3 | `### Rn: <Title>` in exactly one canonical source; the other document references it |
| `LDG-NN` | Feature-wide | `LEDGER.md` | `#### LDG-NN: <Title>` |
| `S/D/XN` | Test plan | Test plan document | `## S/D/XN: <Title>` |

### 13.2 Row-based exceptions

The following IDs are intentionally defined by table rows rather than headings:

- `ACT-NN` — defined in the Actors Registry (Table 1A).
- `SF-NN.N`, `AF-NN.N`, `EF-NN.N`, `BE-NN.N`, and `BR-NN.N` — defined in Table 1B within the parent UC section.

The validator/review renderer must link these IDs to their specific registry/flow row or, where the UI cannot address a row directly, to the parent section with the row identity preserved. Do not create duplicate headings for these IDs.

### 13.3 Validation requirements

The Coordinator's cross-reference validator must:

1. Extract all ID definitions.
2. Extract all ID references from documents and canonical tables.
3. Reject malformed IDs.
4. Reject duplicate definitions.
5. Reject dangling references.
6. Verify that every row-based flow belongs to the UC number encoded in its ID (for example, `SF-01.2` and `AF-01.1` must be inside `UC-01`).
7. Verify that every non-success branch's `Origin Step` references an existing `SF-NN.N` row within the same parent UC.
8. Validate references stage-aware: during Stage 2 TDD validation, resolve `CMP-NN`, `DD-N`, and Stage 2 risk definitions against the TDD itself and previously approved Use Case artefacts; do not require Stage 3 `COMPONENT-MASTER.md` or `CENTRAL-PLAN-INDEX.md` files to exist. During Stage 3 full-feature validation, enforce global uniqueness and resolution across all generated artefacts.
9. Verify that every use-case actor reference resolves to Table 1A.
10. Verify that component references resolve to the unique TDD component definitions.
11. Verify that every story maps to valid UC/flow IDs or is explicitly infrastructure/operations work.
12. Verify that the TDD traceability matrix covers every approved UC.
13. Verify that the central plan and component story indexes agree.
14. Verify that every dependency and unlock reference resolves and the graph has no cycles.
15. Verify that the Ledger only references existing UC/flow/story IDs and that required approvals are present.

---

## 14. Synchronization rules: Markdown and SQLite

Markdown is the human-readable interface; SQLite is the machine-readable authoritative state for coordination and validation.

- Coordinator imports/parses documents only after syntax/schema validation.
- Coordinator must not silently overwrite human edits that have not been reconciled.
- On synchronization conflict, retain the source revision, identify the conflicting records, and require an explicit reconciliation action.
- Changes to a component phase story must update the canonical story record and be reflected in `COMPONENT-MASTER.md` and `CENTRAL-PLAN-INDEX.md`.
- Changes to dependencies must recompute `Unlocks`, execution order, parallel groups, and cycle validation.
- Changes to an approved Use Case or flow must trigger downstream impact analysis and stale affected TDD/planning/review records.
- Any authorised deviation must be represented in `LEDGER.md` and synchronised to SQLite.
- Generated dashboards and progress tables must be rendered from database state, not maintained independently by agents.
- All synchronisation operations must be idempotent and auditable.

---

## 15. Agent responsibilities when generating standards-compliant documents

| Agent / Role | Document responsibility | Mandatory validation handoff |
|---|---|---|
| Ideator Inquirer | Captures problem, actors, questions, success/alternative/exception/boundary/business-rule flows, notes, and Q&A JSON | Check completeness of interview notes and actor/flow coverage |
| Ideator Document Generator | Generates `001-USE-CASES.md`, Table 1A, Table 1, UC sections, and Table 1B | Run use-case schema, ID, actor, flow, and cross-reference validation |
| Ideator Handoff Provider | Creates `REVIEW-CHECKLIST.md` and presents use cases for human review | Require complete checklist and explicit human approval |
| Technical Designer Inquirer | Captures existing/new systems, component responsibilities, constraints, APIs, events, data, security, quality and open questions | Preserve Q&A history and link facts to approved UC/flow IDs |
| Technical Designer Document Generator | Generates `<feature>_tdd.md` from the supplied template, strips `<aside>` blocks, retains optional sections, adds Table 13 | Run TDD structure, template, traceability, ID, and schema validation |
| Technical Designer Handoff Provider | Creates TDD review checklist and requests human approval | Require complete checklist and explicit human approval |
| Central Plan Indexer | Maps use cases to components/stories and creates Table 2, risk and milestone records | Validate topological order, DAG, unlocks, parallelism and coverage |
| Component Planner | Generates component masters and phase Story Cards | Validate Table 3, Table 4, Table 5, story origin, dependencies and unlocks |
| Testing Planner | Creates Table 15 test plan and user-journey coverage | Validate real dependency strategy, no-mocks policy and Docker isolation |
| Executor | Implements one assigned story in its isolated worktree and updates evidence/status | Run tests, mock-diff scan, Code Reacher refresh, PR and GHA evidence gates |
| Coordinator | Orchestrates lifecycle, validates documents, synchronises state, renders dashboards and maintains Ledger | Enforce all prerequisites and approvals; retain audit history |
| Code Reacher | Indexes and incrementally validates code structure and relationships | Record index revision and source commit; report indexing failures |

---

## 16. Retry and repair rules

- The maximum automatic repair/retry count is **three attempts** for an agent task/checkpoint.
- A deterministic Python validator supplies exact error IDs, file paths, line numbers, expected values, and actual values where possible.
- The offending agent receives the exact validation findings and the relevant document/context, not a vague instruction to "fix the file".
- After each repair attempt, rerun the same deterministic validator and compare results.
- Do not count a successful validation as proof of human approval.
- If the third attempt still fails, mark the workflow/checkpoint as blocked and escalate with the remaining findings to a human.
- Jev may provide structured advisory decisions to agents, but Python validation remains mandatory and Jev unavailability falls back to deterministic validation.
- Jev must never override human approval or the Coordinator's persisted workflow gates.

---

## 17. Non-applicable sections and empty tables

- Required tables must remain present even when no records apply, unless the section is explicitly marked `N/A` and the validator's schema defines that as acceptable.
- Where a table is retained, use the exact header and a single row containing `—` in every data column.
- Do not invent placeholder IDs to populate a table.
- For TDD optional sections, preserve the heading and write `N/A — <brief reason>` when unused.
- Do not delete optional TDD sections simply because the current feature does not use them.

---

## 18. Validator implementation contract

Python validators must be deterministic and must report machine-readable findings. The exact CLI names may be finalised in the implementation plan, but the following validation capabilities are mandatory:

| Validator capability | Minimum checks |
|---|---|
| Use Case Schema | Table 1A, Table 1, every UC heading, Table 1B, actor references, flow/type correspondence, required fields |
| TDD Schema | Required template sections, no HTML comments or `<aside>` blocks in generated output, optional sections retained, Table 13, TDD-local `CMP-NN`/`DD-N` definitions, allowed risk definitions, no unresolved placeholders or template boilerplate |
| Central Plan Schema | Expanded Table 2 exact header, field formats, story uniqueness, dependency references, unlock derivation, topological order, parallelism, risk/milestone tables |
| Component Plan Schema | Component ID references match TDD definitions without duplicate `CMP-NN` definition headings, Table 3 Story Cards, Table 4 index, Table 5 dashboard, `CDD-N` definitions |
| Coordinator Schema | Tables 6, 7, 8 and 12 exact headers, status values, rendered counts and paths |
| Q&A Schema | Table 11 exact header and valid sequential numbering |
| Ledger Schema | Table 14 exact header, LDG definitions, allowed dispositions, approver requirements, valid source IDs |
| Test Plan Schema (`validate-test-plan.py`) | `TEST-PLAN.md` Table 15 exact header, test phase IDs, allowed dependency modes, status values, evidence and no-mocks exception documentation |
| Cross-reference Schema | All ID formats, unique definitions, resolved references, flow-to-parent mapping, actor/component/story traceability |
| Synchronization Schema | Markdown/database parity, stale-review invalidation, status parity and idempotent rendering |

### 18.0 TDD template sanitisation and residual-content checks

The TDD generator MUST run a deterministic post-processor before validation:

1. Remove every HTML comment block (`<!-- ... -->`) from generated TDD output, including the `TEMPLATE AUTHORING CONTRACT` comment.
2. Remove instructional `<aside>...</aside>` blocks while preserving surrounding required headings/content.
3. Reject the generated TDD if any known instructional strings remain, including `Replace the illustrative diagram above`, `Replace the illustrative sequence`, `TEMPLATE AUTHORING CONTRACT`, `END TEMPLATE AUTHORING CONTRACT`, or other phrases registered in the template sanitisation test fixtures.
4. Reject unresolved placeholder markers, including literal `<PLACEHOLDER>`, `<FEATURE NAME>`, `<Feature name>`, `<Name or role>`, `[authoring instruction]`, or equivalent unresolved template tokens. The validator must distinguish placeholder markers from valid code examples using fixture-backed rules rather than a broad unsafe regex.
5. Do not silently delete meaningful project prose that happens to contain words such as "replace" or "placeholder"; validate only the explicit instruction/placeholder registry and approved patterns.
6. Emit sanitisation actions as an auditable transformation report, then validate the resulting document. Preserve the original generated draft as a pre-sanitisation revision when review/audit policy requires it.

### 18.1 Validation finding format

Each finding should include:

- `code` — stable machine-readable rule code.
- `severity` — `ERROR`, `WARNING`, or `INFO`.
- `file` — workspace-relative path.
- `line` — line number where available.
- `entity_id` — relevant ID where applicable.
- `message` — concise explanation.
- `expected` — expected schema/value where applicable.
- `actual` — observed schema/value where applicable.
- `repair_hint` — actionable correction.

An `ERROR` blocks the relevant stage. A `WARNING` is non-blocking unless a stage-specific rule promotes it. The validator must exit non-zero for blocking errors.

---

## 19. Versioning and schema evolution

- This document is versioned independently from the Dridh software package.
- Generated documents record the `Standards Version` used to generate them.
- Any change to a canonical table header, ID regex, required field order, or controlled status set requires a standards version increment and coordinated validator/schema migration.
- A backward-incompatible schema change requires a major standards version increment.
- Additive compatible changes may use a minor version increment only when old validators can safely reject or ignore the new structure by explicit version negotiation; never assume silent compatibility.
- Fixes to examples/clarifications that do not alter parser behaviour may use a patch version increment.
- The Coordinator must detect documents generated with an unsupported standards version and block unsafe parsing until an explicit migration or compatibility path is provided.
- Do not let different agents use different standards versions within the same active feature workflow.

---

## 19.1 Version 2.1.0 change summary

Version 2.1.0 harmonises the success-flow model and stage-aware reference definitions:

- Adds `SF-NN.N` as the unique ID for every sequential primary success-flow step.
- Requires `Origin Step` to point to an exact `SF-NN.N` flow row.
- Makes TDD the Stage 2 canonical definition site for `CMP-NN` and Stage 2 risks, avoiding references to Stage 3 documents that do not exist yet.
- Requires the 10-row Table 3 story card already defined in this standard, including `Flow IDs` and `Unlocks`.
- Clarifies staging Inquirer notes, Test Plan validation, TDD template sanitisation, and human-only Ledger approval.

## 20. Final non-negotiable rules

1. Agents MUST follow the exact table schemas in this document.
2. Validators, not LLM self-assessment, determine whether generated documents conform to the schema.
3. Use Cases are the approved functional source of truth; deviations require the Ledger and applicable human approval.
4. Every actor must have an `ACT-NN` ID; every actionable component must have a `CMP-NN` ID.
5. Every UC must have a `### UC-NN` heading and a Table 1B flow table with one unique `SF-NN.N` row per primary success step; branch origins reference exact SF IDs.
6. The Central Plan uses the expanded ten-column Table 2. `Unlocks` is consistent with the dependency graph.
7. Every story card includes originating Use Case IDs, Flow IDs, Dependencies and Unlocks.
8. Dependency graphs must be acyclic and execution order must be topologically valid.
9. Component/master/phase/central statuses must remain synchronised through Coordinator state.
10. TDD generation removes all HTML comments and instructional `<aside>` blocks, rejects leftover boilerplate/placeholders, and retains unused optional sections marked `N/A`.
11. The TDD includes the Use Case-to-Design Traceability Matrix (Table 13).
12. `LEDGER.md` is the only authorised deviation record and uses Table 14; approval requires a human CLI confirmation and cannot be invoked by an agent tool.
13. Review uses Markdown checklists and explicit human approval; readiness scores and Jev decisions cannot approve documents.
14. `TEST-PLAN.md` uses Table 15 and is validated by `validate-test-plan.py`. Prefer Testcontainers and real dependencies. Docker Compose networks must be unique per branch/worktree. WireMock is a documented last-resort exception only.
15. New mocking-library additions must be identified against the historical base diff and sent through the bounded Executor repair loop.
16. PR creation, passing verification/tests, successful GHA checks, local integration, Code Reacher re-indexing, and Coordinator reconciliation are required for dependency unlocking. Remote PR approval/merge is not required for that unlock.
17. Automatic repair is limited to three attempts before human escalation.
18. If a table/section is not applicable, mark it `N/A` or use a single all-`—` row as specified; do not invent content.
19. No agent may silently modify an approved requirement or carry approval across a changed document revision.
20. Any schema ambiguity, unsupported extension, failed integration, or unresolved policy decision must be reported explicitly rather than guessed.
