"""75P Command-line interface for release preparation, freeze, and review.

Usage:
  python3.14 -B cli.py --stage prepare [--plan] [--dry-run]
  python3.14 -B cli.py --stage freeze  [--plan] [--dry-run]
  python3.14 -B cli.py --stage review  [--plan] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import release_context as rc
from freeze import run_freeze
from prepare import run_prepare
from review import run_review
from nodes import prepare_nodes


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='cli.py',
        description='小八 AgentOS 75P 准备、冻结与审查交接工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--stage',
        choices=['prepare', 'freeze', 'nodes', 'review'],
        required=False,
        help='Target pipeline stage to execute: prepare, freeze, nodes, or review',
    )
    parser.add_argument(
        '--plan',
        action='store_true',
        help='Display execution plan without writing files or calling stages',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Validate read-only context and gates without mutating state or triggering heavy processes',
    )
    parser.add_argument(
        '--work-dir',
        type=str,
        default=None,
        help='Override work directory (default: parent directory of driver75p)',
    )
    parser.add_argument(
        '--evidence-dir',
        type=str,
        default=None,
        help='Evidence directory for the nodes stage (default: reports/<round>/driver75p-r2/nodes)',
    )
    return parser.parse_args(argv)


def display_plan(ctx: dict[str, Any], stage: str | None) -> None:
    print('========================================')
    print('  小八 AgentOS 75P 阶段执行计划 (Plan)')
    print('========================================')
    print(f'版本: {ctx["version"]}')
    print(f'轮次: {ctx["round"]}')
    print(f'根目录: {ctx["root"]}')
    print(f'当前 Attempt: {ctx["record"]["attempt"]}')
    print(f'BASE: {ctx["assigned"]["base"]}')
    print(f'Diff Base: {ctx["assigned"]["diff_base"]}')
    print(f'候选包目标: {ctx["record"]["releases_package_dir"]}')
    print('----------------------------------------')

    if stage == 'prepare' or stage is None:
        print('[Stage: prepare]')
        print('  1. 冷启动写前合同：旧 live SOURCE_MANIFEST==previous_source_sha256 + 75A 监看三文件授权与同源集合 + 唯一 record.diff 权威')
        print('  2. 从当前零 source 状态核 BASE Git 对象并按六范围派生 roots 完整导出 archive（development 整体 + 监看 + docs/architecture-topology）')
        print('  3. 逐 blob 计算 sha1 并核验 git ls-tree（同一 roots，不硬编码文件数）')
        print('  4. 4 套 baseline (core, ingress, architecture, monitor) 真实运行并动态解析结果')
        print('  5. candidate scripts/create_manifest.py --verify 动态解析文件数（review-ingress 投影）')
        print('  6. 生成并校验第二大脑树')
        print('  7. 绑定唯一 record.diff.diff_path 权威（与 params scope 实际 git diff 同字节/hash）')
        print('  8. 绑定 PREPARATION.json 与 BASE_READY.json')

    if stage == 'freeze' or stage is None:
        print('[Stage: freeze]')
        print('  1. 重核准备产物：导出 blob、全量 baseline 日志、第二大脑树、唯一 record.diff 权威')
        print('  2. 创建 ROOT15 内可参数化隔离工作现场（freeze-site，非系统 tmp）')
        print('  3. 调用 build_release.py 构建候选包（实算 release_tier，不强设 migration）')
        print('  4. 生成 review prompt review-card49.txt (<32KB，scope 从 params.diff.paths 生成)')
        print('  5. 组装 acceptance-config.json 并严格执行 workday_isolation 门禁')
        print('  6. 写入 FREEZE.json 与 STATE.json（只冻结材料+现场；nodes/review 随后受控写 cfg/db）')

    if stage == 'nodes' or stage is None:
        print('[Stage: nodes]')
        print('  1. 写前只读门禁：两工具清单 + 指派 BASE/source 逐文件 + PREP/READY/FREEZE + 活动 config hash(B09)')
        print('  2. 读取 MANAGEMENT-STATE.review_node_ids 明确选定的两个节点（无默认、无 fallback）')
        print('  3. 只读复制 live ri_sources 两条启用配置（SQL id==reviewer_id，保留 model/effort/entry，不复制旧 test）')
        print('  4. 构建隔离 AppConfig/EventStore/ReviewStore/AgentPool 并绑定工作区')
        print('  5. 真实 NodeRuntime.test_run 测试主体（重型 CLI 可注入；生产路径用真实 json_stdio_factory）')
        print('  6. 两个真实 passed 后才写 node_tests_passed；失败保留原文，不重试、不换模型')
        try:
            rc.preflight_write_gate(ctx, 'nodes')
            print('  [写前门禁] 当前输入齐全，preflight 通过')
        except SystemExit as exc:
            print(f'  [写前门禁] 当前缺项/拒绝（未通过，不谎称成功）：{exc}')

    if stage == 'review' or stage is None:
        print('[Stage: review]')
        print('  1. 核验 STATE.json、FREEZE.json、prompt 与 diff 哈希')
        print('  2. 记录单次提审标记 SUBMIT.started')
        print('  3. 调用 xiaoba_agentos.cli submit --material 声明本地材料')
        print('  4. 后台 Orchestrator 驱动审查')
        print('  5. 执行至少两独立合格票规则（无硬编码 Agent，通过 build_release._passed_verdict）')
        print('  6. 校验 source manifest，输出 11-acceptance.json，保留单次执行日志')

    print('========================================')


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if not args.stage and not args.plan:
        print('Error: --stage [prepare|freeze|nodes|review] or --plan required.\n')
        parse_args(['--help'])
        sys.exit(2)

    # 1. Interpreter check
    rc.check_interpreter()

    # 2. Load context
    ctx = rc.load_context(args.work_dir)

    # 3. Handle --plan
    if args.plan:
        display_plan(ctx, args.stage)
        sys.exit(0)

    # 4. Handle --dry-run
    if args.dry_run:
        print(f'[DRY-RUN] Validating context for stage: {args.stage}...')
        if args.stage == 'nodes':
            ids = ctx['state'].get('review_node_ids')
            if not isinstance(ids, list) or len(ids) != 2 or len(set(ids)) != 2:
                raise SystemExit('STOP: review_node_ids must name exactly two distinct nodes')
            rc.preflight_write_gate(ctx, 'nodes')
            print(f'[DRY-RUN] nodes write-before preflight passed; selected nodes: {ids}')
        else:
            rc.validate_context(ctx, args.stage)
            print(f'[DRY-RUN] Context validation passed successfully for stage: {args.stage}')
        sys.exit(0)

    # 5. Execute stage
    stage = args.stage
    if stage == 'prepare':
        res = run_prepare(ctx)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif stage == 'freeze':
        res = run_freeze(ctx)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif stage == 'nodes':
        evidence_dir = args.evidence_dir or str(Path(ctx['reports']) / 'driver75p-r2' / 'nodes')
        res = prepare_nodes(ctx, evidence_dir=evidence_dir)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif stage == 'review':
        res = run_review(ctx)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        raise SystemExit(f'STOP: unhandled stage {stage}')


if __name__ == '__main__':
    main()
