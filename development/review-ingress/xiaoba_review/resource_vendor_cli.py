"""Small vendor dialects. All workspaces, receipts and process handling are shared."""
import os
from pathlib import Path
from .resource_cli import CliWorklogResource
from .resource_workbuddy import WorkBuddyCliResource

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


class QoderCliResource(CliWorklogResource):
    agent_type, label, provider = 'qoder', 'Qoder', 'qoder_cli'

    def _command(self, entry, timeout):
        return [entry, '-p', *(['--model', self.model] if self.model else []), '--permission-mode', 'accept_edits',
                '--tools', FILE_TOOLS, '--no-session-persistence', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}', '--output-format', 'stream-json']


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
        return [entry, '-p', *(['--model', self.model] if self.model else []),
                '--permission-mode', 'acceptEdits', '--tools', FILE_TOOLS,
                '--allowedTools', FILE_TOOLS, '--no-session-persistence',
                '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                '--output-format', 'stream-json', '--verbose']


class ClaudeCliResource(CliWorklogResource):
    agent_type, label, provider = 'claude', 'Claude', 'claude_cli'

    def _command(self, entry, timeout):
        return [entry, '-p', *(['--model', self.model] if self.model else []), '--permission-mode', 'acceptEdits',
                '--tools', FILE_TOOLS, '--no-session-persistence', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}', '--output-format', 'stream-json', '--verbose']


RESOURCES = {'dsh':DshCliResource, 'qoder':QoderCliResource, 'workbuddy':WorkBuddyCliResource,
             'codebuddy':CodeBuddyCliResource, 'codex':CodexCliResource, 'claude':ClaudeCliResource}
