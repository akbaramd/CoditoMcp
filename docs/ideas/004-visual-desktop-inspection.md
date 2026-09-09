# Visual Desktop Inspection

## Status

Enhancement idea. Builds on the existing `screen_list` and `screenshot_capture` capabilities.

## Existing Capability

Codito already lets ChatGPT list Windows displays and capture an approved selected display as an image. This is useful and should remain a core capability.

## Idea

Evolve screen capture from a single screenshot primitive into a stronger visual inspection layer for development and troubleshooting while preserving the existing security boundary.

Possible future improvements include:

- richer capture metadata such as exact display dimensions and capture timestamp,
- optional capture of an explicitly selected application/window if a safe Windows authorization model is designed,
- bounded repeated captures for comparing visual state during a workflow,
- explicit visual-state verification workflows after browser or application actions,
- tighter integration with browser automation so semantic browser state and actual rendered pixels can be compared.

## Why This Matters

A coding agent often needs to verify the actual result of its work: layout problems, error dialogs, browser rendering, desktop applications, and other issues that are invisible in source code or terminal logs.

Visual inspection complements code intelligence rather than replacing it:

```text
code -> build/run -> visual result -> diagnose -> patch -> verify
```

## Constraint

This idea must not implicitly become unrestricted desktop control. Screen reading, browser automation, and any future mouse/keyboard or window-level control should remain distinct capabilities with separate permissions and explicit security decisions.

## Next Step

Keep the current screen capture implementation as the baseline and revisit this idea when browser/process orchestration is mature enough to benefit from richer visual verification.
