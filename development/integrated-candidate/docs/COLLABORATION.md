# v1.6.6 Agent 池分工与审核

Assignments 必须且只能包含这次选中的协作节点，每个节点一条。协作节点和审查节点都由用户在 Agent 池设定，没有默认名单，也没有备用补位。同一任务里协作过的节点不能审查；没有协作节点时不排队。

# Collaboration Before Independent Review

`collaborate` and `collaborate-review` use the normal task queue, verified release binding,
health gates, concurrent adapters, retry budget, execution receipts and audit chain.
They do not change provider selection or backend file permissions. Each node keeps
its own entry and model from the Agent pool; login stays in that CLI.

## Work

Prepare an assignments JSON object with exactly one non-empty instruction per selected
collaboration node. Supply a sourced UTF-8 context file (64 KiB maximum). Assign concrete,
independent subtasks and ask for precise patches or other usable contributions.

```sh
xiaoba collaborate "Implement the requested fixes" \
  --assignments-file /absolute/assignments.json \
  --context-file /absolute/source-context.txt \
  --message-id unique-work-message
xiaoba run --once
xiaoba show WORK_TASK_ID
```

Every assigned Agent must complete. Failed attempts retain completed peer outputs and
use the existing bounded recovery policy. Successful work returns `next_stage: integration`
and a contribution bundle with run IDs, output hashes and receipt hashes. It has work
provenance, no judge decision and no memory candidate. A host integrates contributions
and runs appropriate verification; backend text is never presented as an executed patch.

## Review

Freeze a new context containing the actual integrated source/diff and test evidence.
Do not include peer reviews or use work-stage suggestions as verification evidence.
Bind all relevant integrated files using repeated `--artifact` arguments.

```sh
xiaoba collaborate-review "Check the integrated fixes for regressions" \
  --work-task-id WORK_TASK_ID --context-file /absolute/integrated-context.txt \
  --artifact /absolute/source.ts --artifact /absolute/test.ts \
  --message-id unique-review-message
xiaoba run --once
xiaoba show REVIEW_TASK_ID
```

Review requires completed work, reviewers who did not collaborate, valid contribution receipts,
and new integration evidence. Every reviewer receives the same frozen task evidence;
historical per-Agent memory is excluded. Review always invokes new Agent runs in a new
task. All reviewers must succeed before the existing deterministic judge runs. The judge
selects an answer, not a semantic consensus or an automatic approval of the code.

Artifact hashes and work evidence are checked before execution and again before delivery.
Changes invalidate the review; freeze the new integration state and submit a new review.
Ordinary retries use the original task ID, never a replacement task. Repeating a command
with its same message ID returns its original task; use a new message ID for changed work.

The completed review follows normal private memory-outbox and explicit human delivery/
final approval rules. These commands do not publish memory or install a release. During
candidate acceptance use an isolated database with memory publication disabled.

## Memory Promotion Boundary

Software completion, release checks and index rebuilds never approve memory. A candidate
stays provisional (`pending_delivery_review` → `delivery_approved` → `pending_final_review`)
until a human records both approvals bound to the same candidate hash; only then may
`publish-memory` settle it as `published`. Active retrieval serves a task-derived memory
only when the database row is `published` *and* the Vault file is still `active`, so a
candidate that has not passed final review, a quarantined (`revalidation_required`) memory
and a `retired` memory can never reach an Agent context even if a Vault file is stale.
With `memory.publish_enabled=false` publication fails before any file write, so it cannot
substitute for human review. Use `agentos memory-receipt <task_id>` to audit the recorded
approvals and the current promotion state.
