# 2. The telephony provider does the speech; this app does the conversation

## Context

Running speech recognition and speech synthesis in-process means streaming audio, voice-activity detection,
barge-in and codec handling, and paying for the CPU or GPU that runs the models. Twilio, Vonage, Telnyx and others
already do this well on phone audio and return text.

## Decision

The app speaks TwiML over plain webhooks: `<Gather input="speech">` to listen (UK English, the phone-call speech
model, automatic end-of-speech), `<Say>` with a neural voice to answer, `<Dial>` to transfer and `<Hangup/>` to end.
Each request is authenticated with the provider's HMAC signature. Recognition confidence is used to re-prompt rather
than act on low-confidence text.

## Consequences

- The app is stateless HTTP plus a small database, easy to host and to test with plain HTTP requests.
- Latency per turn is the provider's recognition plus the brain's time. The offline brain adds well under 50 ms at p95;
  the Claude brain adds a model call, which is why it runs at low effort.
- Recognition errors are outside the app's control, so the evaluation measures the dialogue at 0, 10 and 20% word
  error rate with the error types phone recognition actually makes.
