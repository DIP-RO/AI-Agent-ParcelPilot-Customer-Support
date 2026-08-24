# AI Tool Usage

- **Claude Code (Anthropic)** was used as the primary development environment
  for this submission: scaffolding the FastAPI backend and React frontend,
  extracting the data pack, writing the rules engines and their tests, and
  drafting the documentation. All design decisions (deterministic engines,
  tool-layer access control, two-phase confirmation, authority-tiered
  retrieval, the Ops Radar) were reviewed, and the generated code was tested —
  69 backend tests plus manual end-to-end runs through the UI, and an
  adversarial multi-agent code review whose confirmed findings were fixed.
- **Claude (Sonnet 5) at runtime** powers the chatbot itself via the Anthropic
  Messages API with tool use.
- `scripts/compile_contracts.py` uses Claude with a strict JSON schema to
  compile the signed agreements into the structured entitlements registry;
  its output is checked in and human-reviewed.
