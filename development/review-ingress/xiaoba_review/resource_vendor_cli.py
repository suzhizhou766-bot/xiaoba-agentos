"""Small vendor dialects. All workspaces, receipts and process handling are shared."""
import os
from pathlib import Path
from .resource_cli import CliWorklogResource
from .agent_pool import REASONING_EFFORT

# Restrict tool-capable print CLIs to file I/O. No shell, network or subagent
# tool is offered; permission hooks and normal client authentication stay active.
FILE_TOOLS = 'Read,Write,Edit,Glob,Grep'


class DshCliResource(CliWorklogResource):
    agent_type, label, provider = 'dsh', 'DSH', 'deepseek_harness_cli'

    def _evidence(self, call_id=None):
        return dict(super()._evidence(call_id), model_source='dsh_desktop_configuration')

    def _command(self, entry, timeout):
        # Match DshAdapter's headless + desktop overlay + JSON + stdin route.
        # Only pass the native path to DSH; never read/parse its configuration.
        home = Path(os.environ.get('DSH_HOME') or Path.home()/'.dsh')
        return [entry, '--profile', 'headless', '--patch',
                str(home/'profiles/desktop/cordis.patch.yml'), '--json',
                '--permission-mode', 'workspace-write', '--approval-policy', 'never', '-']


def _effort_args(agent_type, effort, flag):
    """Append the vendor effort flag, or raise a clear error for bad values."""
    if effort is None:
        return []
    allowed = REASONING_EFFORT.get(agent_type, frozenset())
    if not isinstance(effort, str) or effort not in allowed:
        raise ValueError('思考强度取值不合法（'+agent_type+'）：'+str(effort)
                         +'；可选 '+', '.join(sorted(allowed)))
    return [flag, effort]


class QoderCliResource(CliWorklogResource):
    agent_type, label, provider = 'qoder', 'Qoder', 'qoder_cli'

    def _command(self, entry, timeout):
        # qodercli --help: --reasoning-effort <level>; Extra High 实测写作 xhigh。
        return [entry, '-p', *(['--model', self.model] if self.model else []), '--permission-mode', 'accept_edits',
                '--tools', FILE_TOOLS, '--no-session-persistence', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}', '--output-format', 'stream-json',
                *_effort_args(self.agent_type, self.reasoning_effort, '--reasoning-effort')]

class CodexCliResource(CliWorklogResource):
    agent_type, label, provider = 'codex', 'Codex', 'codex_cli'

    def _command(self, entry, timeout):
        return [entry, 'exec', *(['-m', self.model] if self.model else []), '-s', 'workspace-write',
                '-C', str(self.workspace.path), '--skip-git-repo-check', '--ephemeral', '--json', '-']


class CodeBuddyCliResource(CliWorklogResource):
    """Independent CodeBuddy Code, distinct from the desktop bundled engine."""
    agent_type, label, provider = 'codebuddy', 'WorkBuddy CLI（CodeBuddy）', 'codebuddy_cli'

    def _environment(self):
        env = dict(super()._environment())
        # Normal CLI authentication/settings stay client-owned. Do not inherit
        # a desktop product, gateway, API key, or sidecar bootstrap from a host.
        for key in list(env):
            if key.startswith(('CODEBUDDY_', 'WORKBUDDY_', 'ACC_PRODUCT_CONFIG')) or key in ('SERVER__HOST', 'SERVER__PORT'):
                env.pop(key)
        env['DISABLE_AUTOUPDATER'] = '1'
        return env

    def _command(self, entry, timeout):
        # codebuddy --help: --effort <level>（不是 --reasoning-effort）。
        return [entry, '-p', *(['--model', self.model] if self.model else []),
                '--permission-mode', 'acceptEdits', '--tools', FILE_TOOLS,
                '--allowedTools', FILE_TOOLS, '--no-session-persistence',
                '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                '--output-format', 'stream-json', '--verbose',
                *_effort_args(self.agent_type, self.reasoning_effort, '--effort')]
class ClaudeCliResource(CliWorklogResource):
    agent_type, label, provider = 'claude', 'Claude', 'claude_cli'

    def _command(self, entry, timeout):
        return [entry, '-p', *(['--model', self.model] if self.model else []), '--permission-mode', 'acceptEdits',
                '--tools', FILE_TOOLS, '--no-session-persistence', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}', '--output-format', 'stream-json', '--verbose']


# 卡 45：内置 WorkBuddy 引擎（workbuddy 类型）已退役，见 resource_stdio.RetiredEngineResource。
RESOURCES = {'dsh':DshCliResource, 'qoder':QoderCliResource,
             'codebuddy':CodeBuddyCliResource, 'codex':CodexCliResource, 'claude':ClaudeCliResource}
