# Known limitations

- **Simulated callers, simulated recognition errors.** The evaluation's callers are goal-driven scripts with two
  phrasing banks, and recognition errors come from a channel that applies the error types phone recognisers make
  (number confusions, homophones, dropped words, fillers) at a set rate. Real callers ramble, interrupt and change
  their minds more, and real recognition errors cluster on accents and background noise.
- **Claude's conversation quality is not scored in CI.** That would need a paid API key on every run. CI checks the
  request, the tool loop and the guards with canned responses; a live evaluation of the Claude brain against the same
  simulator is a natural next step.
- **Held-out idioms the rules do not cover.** "What's the damage", "go for it", "spot on" and similar fall
  through to a clarifying question. This is why the Claude brain exists.
- **One clinic, one language.** The demo is a UK physiotherapy clinic in English. Other businesses need their own
  JSON file; other languages need new parsing rules (or the Claude brain).
- **No SMS confirmations.** The receptionist says a confirmation text will follow; sending it is left to the
  provider's messaging API.
- **SQLite.** Fine for one clinic on one server (writes are serialised); a multi-site deployment would use Postgres
  with row-level locking.
