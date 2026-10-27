import json
import argparse
parser=argparse.ArgumentParser(description='Browser QA; use an isolated test runtime, never production.')
parser.add_argument('--base-url',required=True)
args=parser.parse_args()
base=args.base_url.rstrip('/')
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1500,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(base+'/architecture/')
    expect(page.get_by_text('更新迭代入口已接入',exact=True)).to_be_visible()
    for tab in ['overview','execution','experience','validation','review','scoring','foundation','upgrade']:
        page.locator('[data-view="'+tab+'"]').click();assert page.locator('.node').count()>0
    page.locator('.node[data-id="upgradeCenter"]').click();page.get_by_role('button',name='编辑所属模块规范 · 架构与升级发布中心',exact=True).click()
    dialog=page.locator('dialog').first;expect(dialog).to_be_visible();dialog.locator('[name="goal"]').fill('QA: isolated UI proposal, no production switch');dialog.locator('[name="body"]').fill('# Browser test proposal\n\nNo active changes.\n');dialog.get_by_role('button',name='保存到待发布',exact=True).click()
    expect(page.locator('.upgrade-panel')).to_be_visible();expect(page.locator('.upgrade-item h3')).to_have_text('QA: isolated UI proposal, no production switch')
    expect(page.get_by_role('button',name='正式发版',exact=True)).to_be_disabled();expect(page.get_by_role('button',name='正式包预检',exact=True)).to_be_disabled()
    page.get_by_role('button',name='检查当前候选',exact=True).click();expect(page.locator('.upgrade-item').get_by_text('待接续构包与审查',exact=True)).to_be_visible()
    page.get_by_text('文件差异 · development/architecture-center/modules/architecture-center/SPEC.md',exact=True).click();expect(page.locator('.upgrade-item')).to_contain_text('+No active changes.')
    page.screenshot(path='/workspace/scratch/architecture-center-queue.png',full_page=True)
    token=page.locator('meta[name="upgrade-page-token"]').get_attribute('content')
    rejected=page.request.post(base+'/api/release-center/create',data={});assert rejected.status==403,rejected.status
    rejected=page.request.post(base+'/api/release-center/create',data={},headers={'X-Xiaoba-Token':token,'Origin':'http://evil.example'});assert rejected.status==403
    page.goto(base+'/');page.get_by_role('tab',name='架构与升级发布',exact=True).click();expect(page.locator('#viewArchitecture')).to_be_visible();frame=page.frame_locator('#viewArchitecture iframe');expect(frame.get_by_text('更新迭代入口已接入',exact=True)).to_be_visible();page.reload();expect(page.locator('#viewArchitecture')).to_be_visible()
    page.get_by_role('tab',name='运行总览',exact=True).click();expect(page.locator('#viewArchitecture')).to_be_hidden()
    mobile=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True);mobile.on('pageerror',lambda e:errors.append(str(e)));mobile.goto(base+'/architecture/#upgrade');expect(mobile.locator('#readingView')).to_be_visible();assert mobile.evaluate('document.documentElement.scrollWidth<=innerWidth')
    mobile.get_by_role('button',name='模块规范',exact=True).click();expect(mobile.locator('dialog').first).to_be_visible();mobile.screenshot(path='/workspace/scratch/architecture-center-mobile.png',full_page=True)
    assert not errors,errors
    print(json.dumps({'ok':True,'checks':['8 topology views','node module edit','stage save','syntax check','diff display','publish gated without runner/package','token and origin guards','native monitor tab and refresh','390px mobile'],'page_errors':errors},ensure_ascii=False));browser.close()
