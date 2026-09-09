# Idea 001 — ChatGPT Browser Scheduler Agent

**Status:** Idea / Exploration
**Product:** Codito
**Created:** 2026-09-09

## Idea

Codito could provide a scheduler whose scheduled jobs are executed through a dedicated ChatGPT agent session.

The core concept is to use Chromium, or another controllable browser runtime, together with browser automation tooling such as Python-based automation. Codito would open ChatGPT in that browser, authenticate once, and preserve the authenticated browser profile/session so the user does not need to log in again for every scheduled execution.

When a scheduled job becomes due, Codito would:

1. Start or attach to the dedicated browser profile.
2. Open ChatGPT.
3. Open the intended agent/chat context for that schedule.
4. Insert the scheduled instruction or prompt into the chat composer.
5. Send the message.
6. Confirm that submission occurred.
7. Close the browser or release the browser worker after dispatch.

This would allow Codito schedules to trigger work inside ChatGPT even when the scheduling capability itself is implemented by Codito.

## Why an Agent Context Is Needed

A schedule should run in a predictable execution context rather than in an arbitrary user chat. Each scheduled task therefore needs an explicit target agent/chat context so that its instructions, tools, project context, and accumulated state remain consistent between runs.

## Initial Technical Shape

- **Scheduler:** Determines when a job is due.
- **Schedule definition:** Stores the prompt/instruction and target agent/chat context.
- **Browser worker:** Launches or attaches to Chromium and performs the UI interaction.
- **Persistent browser profile:** Keeps the authenticated ChatGPT session available between executions.
- **Dispatch flow:** Opens the target context, writes the scheduled prompt, and sends it.
- **Execution record:** Records whether dispatch succeeded or failed and enough metadata for later diagnosis.

## Important Constraints to Investigate

- Authentication/session persistence must be handled securely; credentials should not be stored directly in schedule definitions.
- Browser automation must detect expired sessions and require re-authentication when necessary.
- A successful UI click is not necessarily proof that the ChatGPT task completed; dispatch state and task-completion state may need to be modeled separately.
- Concurrent schedules need isolated tabs, browser contexts, or workers so jobs do not interfere with one another.
- The design needs retry/idempotency rules to avoid sending the same scheduled prompt twice after crashes or uncertain browser state.
- Changes to the ChatGPT web UI can break selectors, so browser interaction should be encapsulated behind a dedicated adapter rather than spread throughout scheduler code.
- The feasibility and permitted use of browser automation for this workflow should be evaluated before implementation.

## Current Decision

No implementation decision has been made yet. Keep this as an idea until the browser lifecycle, persistent session model, scheduling semantics, reliability model, and ChatGPT interaction strategy have been researched.
