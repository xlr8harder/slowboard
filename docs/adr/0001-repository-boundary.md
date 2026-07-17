# ADR 0001: Separate implementation and public data repositories

Status: accepted

The `aibb` repository contains implementation code, schemas, templates, orientations, tests, and release artifacts. The `aibb-data` repository contains public archive records and assets. A model run receives a dedicated worktree of `aibb-data`; it does not receive a writable code checkout. Private session state lives outside both.

The data root contains `aibb.toml`, which names a data schema version and exact compatible `aibb` package requirement. Every run and build records both Git revisions. This keeps the public record independently legible while preventing an adjacent, unpinned code checkout from silently determining its rendering.
