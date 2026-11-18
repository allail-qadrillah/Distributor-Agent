"""Intent classification, the application-layer gate before the agent.

Public surface:
- schemas.IntentCategory / IntentClassification: the validated classifier
  output contract.
- classifier.MessageIntentClassifier: a separate OpenAI-compatible
  chat-completions client. It never reuses the main agent provider
  configuration (separate settings, separate instance).
- gate.IntentGate: the classification POLICY. It maps a classification
  (or any classifier failure) to allow-agent vs human-handoff, and fails
  closed: a classifier that errors, times out, or returns invalid output
  never lets a message reach the agent.
"""
