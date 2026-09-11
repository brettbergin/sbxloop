---
name: deliver-pr
description: How a change becomes a pull request the repository will accept, and how to answer a review.
roles: builder
---

# Delivering the change

Read this when you are shaping work for delivery, filling in a pull request
description, or answering a review.

## The repository's conventions win

Before writing a commit message, a branch name or a description, look at
what the repository already does. Read recent history for the commit style
it uses. Look for a pull request template, a title lint, a changelog
convention, a required issue reference. If the repository states a rule
about how it wants to be changed, that rule outranks every default you have
been given, including the ones in this file.

Two things follow from that:

- **Match the surrounding code, not your preference.** Naming, layout, where
  tests live, which formatter runs. Read a neighbouring module before
  writing a new one, and put a new test where the existing ones are.
- **Change only what the work requires.** Deleting, rewriting or
  reformatting beyond the scope of the ask is a defect, and the review is
  told to treat it as one. No unrelated reformatting, no renames for taste,
  no "while I am here" fixes. Note them in your report instead, where a
  human can decide.

## Writing the description

If the repository has a pull request template, the description is that
template filled in, verbatim in structure, so a check that parses its
sections still finds them. Fill the sections honestly: a template section
you have nothing to say about should say that, not be padded.

Where your brief names a file to write the description into, write it there
rather than into your report. The description says what changed and why, and
what was verified. It is read by someone who was not watching the run.

## Answering a review

Reviews arrive from two sources and they are answered differently.

**A human reviewer** gets a real conversation. Address every point. Where
you disagree, say so with the reason, and make the change anyway if they
repeat it: it is their repository.

**An automated reviewer gets exactly one round.** Read everything it said,
decide what is genuinely worth acting on, make those changes, and answer
once. Do not enter a fix loop with a bot. Bots re-comment on the code you
just changed, and a loop costs the run's whole remaining budget without
converging. If a bot's finding is wrong, say why once and move on.

## What "finished" means

Not "the check went green". Finished is:

- the ask is fully addressed, or the part that is not is named explicitly;
- the repository's own gate is no worse than it was on the base;
- nothing changed that the ask did not require;
- the description tells a reader what happened without them reading the
  diff.

A change that meets the first four but has a known problem is finished and
flagged. A change that hides the problem is not finished.
