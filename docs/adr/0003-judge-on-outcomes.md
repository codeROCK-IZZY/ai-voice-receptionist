# 3. Judge simulated calls on the diary, with a held-out phrasing bank

## Context

Dialogue systems are easy to evaluate badly: check that the reply "sounds right", or test only phrasings the rules
were written for.

## Decision

- Each simulated caller has a goal and a persona; the judge reads the diary and message book after the call and
  decides success from the outcome (the right service, day, time window, name and number, booked exactly once; the
  right booking cancelled; nothing changed on a misuse call).
- Wrong changes are counted separately from failures: a call that ends with a transfer is a failure, a call that
  books the wrong time is a wrong change. The gate requires zero wrong changes.
- Callers speak from one of two phrasing banks. The rules were tuned on `dev` only; `heldout` was written
  separately and is reported on its own, including idioms the rules do not understand.
- Every call starts from the same, realistically busy diary (a copy of a prefilled template), and booking references
  come from a seeded generator, so results are reproducible run to run.

## Consequences

- The numbers mean "the caller got what they wanted", which is what a clinic cares about.
- The simulated callers are still simpler than people. They are cooperative, they speak in short turns, and their
  errors come from a statistical channel rather than real audio (see known limitations).
