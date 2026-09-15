## What does this change?

<!-- One or two sentences. What was wrong, or what is new. -->

## What did you test it against?

<!-- This is the important one, and it is not optional for a hardware fix.
     WebATEM has only ever run on ATEM 1 M/E Constellation HD switchers, so
     for anything model-specific your report IS the verification. Say which
     switcher, which video mode, and what you saw. "No hardware, code change
     only" is a perfectly good answer when it is true. -->

- Switcher model:
- Video mode:
- What I saw:

## Checklist

- [ ] `pytest -q` passes
- [ ] A test covers the change, or it is not the kind of change a test can cover
- [ ] Nothing here is specific to one organisation (that belongs in a hooks class)
- [ ] Wire-level protocol changes went to bmdwire instead
