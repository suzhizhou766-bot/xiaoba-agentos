import importlib.util,json,pathlib,re,subprocess,sys,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
FROZEN_COMPAT=['第二大脑','00_小八AgentOS.md','knowlp']
def load(name, file):
 spec=importlib.util.spec_from_file_location(name,file);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
class CurrentReleaseTest(unittest.TestCase):
 def test_exact_enabled_pair_and_policy(self):
  c=json.loads((ROOT/'config/agentos.json').read_text())
  self.assertEqual({'agy_review','dsh_review'},{a['name'] for a in c['agents'] if a['enabled'] and not a.get('standby')})
  # v1.6.5: exactly one standby, which only ever covers a failed primary.
  self.assertEqual(['qoder_review'],[a['name'] for a in c['agents'] if a.get('standby')])
  self.assertEqual('qoder',next(a for a in c['agents'] if a.get('standby'))['driver'])
  self.assertEqual(2,c['worker']['minimum_successful_agents'])
  self.assertTrue(c['release']['final_publish_requires_human'])
 def test_generator_repeatable_and_tree_valid(self):
  m=load('current_brain',ROOT/'scripts/build_second_brain.py')
  a=m.build(ROOT);b=m.build(ROOT);self.assertEqual(a,b);self.assertEqual([],m.validate(a))
  self.assertIn('AGY',a['10_治理与裁决/模型链路与认证.md'])
  self.assertIn('DSH',a['10_治理与裁决/模型链路与认证.md'])
 def test_indices_version_and_history_current_only(self):
  version=load('agentos_version',ROOT/'scripts/agentos_version.py').software_version(ROOT)
  self.assertRegex(version,r'^v\d+\.\d+\.\d+$','pyproject.toml 声明的软件版本必须是 vX.Y.Z')
  # 技能索引与价值文件索引是内容，不在包里，也不写软件版本号：内容不该因为发版而变。
  self.assertFalse((ROOT/'skills').exists() or (ROOT/'knowledge').exists(),'技能与价值文件索引已出包，运行包里不得再有这两个目录')
  for kind in ['skills','knowledge']:
   index=json.loads((ROOT/'tests/fixtures/content'/kind/'index.json').read_text())
   self.assertNotIn('release',index,f'{kind} 索引不得写软件版本号')
   self.assertEqual(1,index['schema_version'],f'{kind} 索引的 schema_version 是索引格式版本，不随软件版本变化')
  records=json.loads((ROOT/'docs/HISTORY_VERSIONS.json').read_text())['records']
  versions=[r['version'] for r in records]
  self.assertEqual(len(versions),len(set(versions)),'历史版本记录不得重复')
  self.assertIn(version,versions,'当前软件版本必须已在历史版本记录中')
  dates=[r['release_date'] for r in records]
  self.assertEqual(sorted(dates),dates,'历史版本记录必须按发布日期升序追加，不得重排')
  for record in records:
   note=ROOT/('docs/RELEASE_'+record['version'].replace('v','V')+'.md')
   self.assertTrue(note.is_file(),f'每个历史版本都需要对应版本方案文件：{note.name}')
 def test_current_documents_declare_same_software_version(self):
  version=load('agentos_version',ROOT/'scripts/agentos_version.py').software_version(ROOT)
  config=json.loads((ROOT/'config/agentos.json').read_text())
  self.assertEqual(version,config['release']['expected_version'],'活动配置期望的发布版本必须等于当前软件版本')
  heads={
   'README.md':1,
   'docs/ACCEPTANCE.md':1,'docs/ARCHITECTURE.md':1,
   'docs/COLLABORATION.md':1,'docs/OPERATIONS.md':1,
   '.agents/skills/xiaoba-agentos/SKILL.md':6,
  }
  for relative,line_number in heads.items():
   line=(ROOT/relative).read_text().splitlines()[line_number-1]
   self.assertIn(version,line,f'{relative} 第{line_number}行必须声明当前软件版本')
  description=(ROOT/'.agents/skills/xiaoba-agentos/SKILL.md').read_text().splitlines()[2]
  self.assertIn(version,description,'入口 SKILL.md 的 description 必须声明当前软件版本')
 def test_compatibility_identifiers_are_never_version_bumped(self):
  version=load('agentos_version',ROOT/'scripts/agentos_version.py').software_version(ROOT)
  config=json.loads((ROOT/'config/agentos.json').read_text())
  for section in ['memory','paths']:
   self.assertEqual('第二大脑',pathlib.Path(config[section]['obsidian_vault']).name,f'{section}.obsidian_vault 目录名是 Obsidian 绑定兼容名，不随软件版本改名')
   self.assertEqual('第二大脑',pathlib.Path(config[section]['knowlp_vault']).name,f'{section}.knowlp_vault 目录名是兼容名，不随软件版本改名')
   self.assertEqual('knowlp',pathlib.Path(config[section]['knowlp_graph_dir']).name,f'{section}.knowlp_graph_dir 是格式兼容名，改名需要独立的格式迁移证据')
  brain_module=load('current_brain_contract',ROOT/'scripts/build_second_brain.py')
  brain=brain_module.build(ROOT)
  self.assertIn('00_小八AgentOS.md',brain,'Obsidian 根笔记文件名是兼容名，不得随软件版本改名')
  self.assertNotIn(version,'00_小八AgentOS.md','根笔记文件名属于 v1.4 树格式族，不含完整软件版本号')
  self.assertEqual([],brain_module.validate(brain))
 def test_current_state_note_declares_version_without_pinned_release_dir(self):
  version=load('agentos_version',ROOT/'scripts/agentos_version.py').software_version(ROOT)
  note=load('current_brain_state',ROOT/'scripts/build_second_brain.py').build(ROOT)['60_当前上下文/系统当前态.md']
  self.assertIn(version,note,'系统当前态必须声明当前软件版本')
  self.assertIn('唯一跨 Agent 入口',note)
  self.assertIn('运行源码',note)
  self.assertIn('不可变发布内容',note)
  self.assertIn('当前 Obsidian Vault',note)
  self.assertNotRegex(note,r'\.runtime/releases/v\d','系统当前态不得写死带版本号的发布目录，必须由发布索引与 verify-release 解析')
  self.assertIn('.runtime/releases/index.json',note,'系统当前态必须指向发布索引这个唯一活动指针')
  stripped=note
  for compat in FROZEN_COMPAT:
   stripped=stripped.replace(compat,'')
  for stale in ['v1.4.0','v1.4.1','v1.4.2']:
   # 按完整版本号匹配：v1.4.10 里包含 v1.4.1 这串字符，但它不是过时版本号
   if stale!=version:self.assertNotRegex(stripped,re.escape(stale)+r'(?!\d)',f'系统当前态在冻结兼容名之外残留过时版本号：{stale}')
 def test_generators_read_version_from_single_declaration_point(self):
  for name in ['build_second_brain.py','build_release.py']:
   text=(ROOT/'scripts'/name).read_text()
   self.assertIn('from agentos_version import software_version',text,f'{name} 必须从 scripts/agentos_version.py 读取软件版本，不得自己内联')
   stripped=text
   for compat in FROZEN_COMPAT:
    stripped=stripped.replace(compat,'')
   inline=re.findall(r'v\d+\.\d+\.\d+',stripped)
   self.assertEqual([],inline,f'{name} 在冻结兼容名之外内联了完整软件版本号，会造成下一次版本漂移：{inline}')
 def test_missing_config_fail_closed(self):
  for name in ['xiaoba','verify-release']:
   p=ROOT/'.agents/skills/xiaoba-agentos/scripts'/name
   r=subprocess.run([sys.executable,str(p),'--config','/nonexistent-agentos-v142.json'],capture_output=True,text=True)
   self.assertNotEqual(0,r.returncode)
   self.assertIn('verification_or_dispatch_failed',r.stdout+r.stderr)
 def test_script_pair_not_divergent(self):
  p=ROOT/'.agents/skills/xiaoba-agentos/scripts'
  a=(p/'xiaoba').read_text().replace('VERIFY_ONLY = False','VERIFY_ONLY = True')
  self.assertEqual(a,(p/'verify-release').read_text())
