# Final Workflow Summary

You close out the completed workflow run. Produce one concise, evidence-based
summary that a human can trust without re-reading the whole transcript.

## Read the evidence

- `$ARTIFACTS_DIR/` holds every gate artifact: the validation report, the
  hardware screen-capture evidence (`hermes-hardware-e2e.md`), the acoustic
  voice-loop evidence (`robodog-voice-loop-e2e.md`), review findings and
  fix commits (if any), and the final production-health report.
- If a PR exists for the run's branch, read its current state with `gh` before
  posting.

## Write the summary

Create `$ARTIFACTS_DIR/workflow-summary.md` containing, in this order:

1. **Feature** — one line restating what was delivered.
2. **Change surface** — the branch, its HEAD commit (short SHA + subject), and
   the files it touches (from `git diff --stat <base>..HEAD`).
3. **Gate results decision matrix** — one row per gate that ran, with
   PASS / FAIL / SKIP (with reason) / not-run:

   | Gate | Result | Evidence |
   |---|---|---|
   | validate (units/lint/mypy/BUILD/firmware compile) | … | … |
   | hermes screen-capture E2E (8/8 frames + vision readback) | … | … |
   | voice-loop E2E (acoustic + readout completeness + relisten) | … | … |
   | production health (.gate-head owner) | … | … |
   | code review (scope → 5 reviewers → synthesize) | … | … |
   | review-fix implementation + final gate chain re-run | … | … |

   Only mark a gate PASS from the artifact that proves it — never from memory.
4. **Review outcome** — synthesized verdict, the count of findings by severity,
   and what was fixed.
5. **Known limitations / notes** — anything a reader should weigh (flaky legs
   that fell back to injection, skipped sub-checks, hardware notes).

## Post the GitHub comment

Post the summary as a comment on the PR (or as a run-completion comment if no
PR exists). Keep it to the same structure — table included. End with a link to
the artifacts directory so the reader can open the evidence.

Do not edit project files. Write only to `$ARTIFACTS_DIR` and post only the
GitHub comment.
