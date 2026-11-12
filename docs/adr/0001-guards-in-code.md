# 1. Safety rules live in the desk, not in a prompt

## Context

A phone receptionist changes real things: who is in the diary tomorrow, whose appointment is cancelled. Large
language models follow instructions well, but "only book after the caller confirms" written in a system prompt is a
request, not a guarantee. Speech recognition adds a second source of error: the caller's "no, wait" can arrive as
"know weight".

## Decision

Every read or change to the diary goes through `Desk`. It refuses, with a message the brain can act on:

- booking a slot that was not returned by `find_slots` on this call;
- booking, cancelling or moving unless `read_back_*` was called with exactly the same details **and** the caller's
  single next utterance is a clear yes (judged by the same yes/no detector for every brain);
- showing or changing a booking until the caller is verified (calling from the number on the booking, or giving its
  last four digits, or giving the full number on it and the name);
- speaking any price that is not on the price list (the sentence is replaced).

The guards can be switched off, and the evaluation does so to measure what they prevent.

## Consequences

- Swapping the rules brain for Claude (or a future model) cannot weaken the rules: 0 of 360 unsafe actions get
  through with the guards on, 360 of 360 with them off.
- A brain has to say the read-back before acting, which costs one turn per change. That is what a careful human
  receptionist does too.
- The guards found real bugs in the offline brain during development (see the README).
