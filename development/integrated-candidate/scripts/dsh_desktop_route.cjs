// Read only routing metadata; upstream settings stay in the native Desktop profile.
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const nonempty = value => typeof value === 'string' && value.trim().length > 0;
// 卡 16: a Desktop profile is a bounded regular file, never a link or a device.
const MAX_CONFIG_BYTES = 256 * 1024;
const readConfig = file => {
  if (typeof file !== 'string' || !path.isAbsolute(file)) throw Error();
  const stats = fs.lstatSync(file);
  if (stats.isSymbolicLink() || !stats.isFile() || stats.size > MAX_CONFIG_BYTES) throw Error();
  return fs.readFileSync(file, 'utf8');
};
try {
  const configText = readConfig(process.argv[3]);
  const cliRequire = createRequire(path.resolve(process.argv[2]));
  const bootRequire = createRequire(cliRequire.resolve('@deepseek-ai/dsh-app-boot'));
  const yaml = bootRequire('js-yaml');
  const entries = yaml.load(configText);
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
