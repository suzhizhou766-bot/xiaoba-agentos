// Read only routing metadata; upstream settings stay in the native Desktop profile.
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const nonempty = value => typeof value === 'string' && value.trim().length > 0;
try {
  const cliRequire = createRequire(path.resolve(process.argv[2]));
  const bootRequire = createRequire(cliRequire.resolve('@deepseek-ai/dsh-app-boot'));
  const yaml = bootRequire('js-yaml');
  const entries = yaml.load(fs.readFileSync(process.argv[3], 'utf8'));
  if (!Array.isArray(entries)) throw Error();
  const defaults = entries.filter(x => x && x.id === 'agent-default-model');
  const llms = entries.filter(x => x && x.id === 'llm-pi-ai');
  if (defaults.length !== 1) throw Error();
  const {provider, model} = defaults[0].config;
  if (!nonempty(provider) || !nonempty(model)) throw Error();
  // A native provider may use account login or an installed catalog. The
  // Harness, not this metadata reader, resolves and validates its upstream.
  const explicit = [...llms].reverse().find(x => x.config && x.config.providers &&
      Object.hasOwn(x.config.providers, provider));
  const displayName = explicit && explicit.config.providers[provider] &&
      explicit.config.providers[provider].displayName;
  const display = nonempty(displayName) ? displayName : provider;
  process.stdout.write(JSON.stringify({provider: display, provider_id: provider,
    model, model_source: 'dsh_desktop'}));
} catch {
  process.stderr.write('DSH Desktop routing configuration is unavailable or invalid\n');
  process.exitCode = 1;
}
