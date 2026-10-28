"""Real macOS executions and explicitly synthetic reviewer outputs for TEST data."""
from pathlib import Path
from types import SimpleNamespace
import sys
import uuid
from xiaoba_agentos.experience_workflow import (
    execute_trial, prepare_trial, reserve_validation_attempt, save_execution_receipt,
    complete_audit, digest,
)
from xiaoba_agentos.validation_evidence import binding


def make_execution(runtime, passed=True, card=None):
    workspace = runtime / "trial-workspaces" / str(uuid.uuid4())
    workspace.mkdir(parents=True)
    card = dict(card or {}, title="隔离执行测试", objective="真实验证断言与来源",
                scope="仅临时TEST空间", acceptance="命令输出断言与独立审查均通过",
                stop_conditions="失败停止采用", problem_key=(card or {}).get("problem_key","fixture-only"), workspace=str(workspace),
                cases=[{"argv":[str(Path(sys.executable).resolve()),"-I","-B","-c",
                                "print('TEST_CASE_PASS'); raise SystemExit(%d)" % (0 if passed else 1)],
                        "expect_stdout":"TEST_CASE_PASS"}])
    cfg = SimpleNamespace(runtime_dir=runtime)
    return card, execute_trial(cfg, card)


def native_trial(store, cand, passed=True, names=("agy_review","dsh_review"), output=None,
                 settle=True, card=None, execution=None, runtime=None):
    runtime = runtime or Path(cand["staged_path"]).parent.parent
    if execution is None:
        card, execution = make_execution(runtime, passed, card)
    attempt = reserve_validation_attempt(store,cand["task_id"],card) if store.is_validation_extension_installed() else None
    cfg = SimpleNamespace(runtime_dir=runtime)
    save_execution_receipt(cfg,cand["task_id"],card,execution)
    trial,prompt = prepare_trial(store,cand["task_id"],card,execution,attempt_id=attempt)
    tid = store.submit(prompt,1,snapshot={"experience_policy":{"version":1,"kind":"audit"},
             "experience_trial":trial,"agents":[{"name":n,"enabled":True} for n in names]})
    for name in names:
        run=store.start_agent_run(tid,name)
        store.finish_agent_run(run,"completed",output or "TEST independent review\nTRIAL_VALIDATION: PASS",None,1)
    store.append_event(tid,"judge.decided",{"successful_agents":len(names),"winner":names[0]})
    if settle:
        complete_audit(store,tid,"TEST independent audit receipt")
    return {"task_id":tid,"plan_digest":trial["card_sha256"],"execution_digest":trial["execution_sha256"],
            "execution":execution,"trial":trial}


def bound_diff(store,cid):
    records=store.get_validation_records(cid)
    return {"initial_failure":"第一轮断言或审查发现确定性失败，需整改后复验",
            "remediation":"修复失败条件，补充隔离断言并保留两轮独立证据",
            "evidence_changes":{"round_one":binding(records[0]),"round_two":binding(records[1])},
            "outcome_changes":"第一轮失败，第二轮实际执行与独立审查全部通过",
            "remaining_limitations":"仅临时夹具；不代表真实模型或生产已发布"}
