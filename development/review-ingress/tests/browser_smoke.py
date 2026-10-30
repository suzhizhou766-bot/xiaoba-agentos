"""Optional real Chromium smoke test; all identities/receipts are TEST fixtures.

Requires playwright and a local Chromium binary. No model calls or user data.
"""
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / 'development/integrated-candidate/src'))
from xiaoba_agentos.store import EventStore
from playwright.sync_api import sync_playwright, expect


def main():
    with tempfile.TemporaryDirectory(prefix='xiaoba-browser-') as temp:
        runtime = Path(temp)
        native = EventStore(runtime / 'agentos.sqlite3')
        native.initialize()
        candidate = native.create_authored_candidate('TEST 浏览器联调卡', 'TEST 夹具，不是真实经验或 DSH 输出。',
                      runtime / 'memory-outbox', author='browser-test', experience_kind='method')
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1',0))
            port=reservation.getsockname()[1]
        with (runtime/'server.log').open('w') as output:
            process = subprocess.Popen([sys.executable,'-B',str(HERE/'run.py'),'--runtime',str(runtime),'--port',str(port)],
                                       stdout=output,stderr=subprocess.STDOUT,cwd=HERE)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError((runtime/'server.log').read_text())
                    try:
                        conn=http.client.HTTPConnection('127.0.0.1',port,timeout=.2)
                        conn.request('GET','/healthz')
                        ready=conn.getresponse().status==200
                        conn.close()
                        if ready:
                            break
                    except OSError:
                        time.sleep(.05)
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=os.environ.get('TEST_CHROMIUM','/usr/bin/chromium'),
                                                headless=True,args=['--no-sandbox'])
                    page=browser.new_page(viewport={'width':1280,'height':900},accept_downloads=True)
                    page.goto('http://127.0.0.1:%d/open-review'%port)
                    expect(page.locator('#connection')).to_contain_text('已连接')
                    page.locator('summary').filter(has_text='登记审查来源').click()
                    for name,kind,ref in [('TEST-DSH','dsh','test-source-dsh'),('TEST-PEER','peer','test-source-peer')]:
                        form=page.locator('#sourceForm')
                        for key,value in {'reviewer_id':name,'source_ref':ref,'provenance':'TEST 夹具来源，不是实际认证'}.items():
                            form.locator('[name="%s"]'%key).fill(value)
                        form.locator('[name="kind"]').select_option(kind)
                        form.get_by_role('button',name='保存来源').click()
                        expect(page.locator('#peerSelect')).to_contain_text(name)
                    form=page.locator('#requestForm')
                    form.locator('[name="candidate_id"]').fill(candidate['task_id'])
                    form.locator('[name="dsh_id"]').select_option('TEST-DSH')
                    form.locator('[name="peer_id"]').select_option('TEST-PEER')
                    form.locator('[name="scope"]').fill('TEST 浏览器流程')
                    form.locator('[name="evidence"]').fill(json.dumps([{'ref':'TEST','sha256':'a'*64}]))
                    form.get_by_role('button',name='生成两份身份任务卡').click()
                    expect(page.locator('#requests')).to_contain_text('等待独立回执')
                    with page.expect_download() as download:
                        page.get_by_role('button',name='导出身份任务卡').first.click()
                    exported=json.loads(Path(download.value.path()).read_text())
                    assert exported['candidate_id']==candidate['task_id'] and exported['card_digest']
                    state=page.request.get('http://127.0.0.1:%d/api/open-review/state'%port).json()
                    for assignment in state['requests'][0]['assignments']:
                        card=assignment['card']
                        payload={key:card[key] for key in ('review_request_id','assignment_id','reviewer_role','plan_revision','reviewer_id','source_ref',
                                                          'candidate_sha256','input_digest','card_digest')}
                        payload.update(receipt_id='TEST-'+assignment['id'],verdict='FAIL' if assignment['role']=='peer' else 'PASS',
                                       findings='TEST 浏览器回执，不是实际 DSH 输出。',evidence_refs=['TEST'],
                                       started_at=time.time()-1,finished_at=time.time())
                        page.locator('#receiptBody').fill(json.dumps(payload,ensure_ascii=False))
                        page.get_by_role('button',name='接收回执',exact=True).click()
                        button=page.get_by_role('button',name='核对实际来源与交接依据',exact=True)
                        expect(button).to_have_count(1)
                        page.once('dialog',lambda dialog:dialog.accept('TEST 操作者核对夹具来源'))
                        button.click()
                        expect(button).to_have_count(0)
                    expect(page.locator('#requests')).to_contain_text('裁决材料中心已接收')
                    expect(page.locator('#decisions')).to_contain_text('FAIL')
                    page.screenshot(path=os.environ.get('TEST_SCREENSHOT','/tmp/xiaoba-open-review.png'),full_page=True)
                    page.goto('http://127.0.0.1:%d/#review'%port)
                    page.get_by_role('tab',name='裁决中心').click()
                    expect(page.locator('#viewReview')).to_contain_text('独立审查材料')
                    expect(page.locator('#viewReview')).to_contain_text('FAIL')
                    browser.close()
                with sqlite3.connect(native.path) as db:
                    assert db.execute('SELECT count(*) FROM ri_inbox WHERE current=1').fetchone()[0]==1
                    assert db.execute('SELECT count(*) FROM memory_reviews').fetchone()[0]==0
                assert native.verify_event_chain()['ok']
                print(json.dumps({'ok':True,'real_browser':True,'real_dsh':False,
                    'checks':['UI source registration','card export','receipt import','operator attestation',
                              'PASS/FAIL inbox','native decision-tab integration','zero human approval records',
                              'native event chain']},ensure_ascii=False))
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__=='__main__':
    main()
