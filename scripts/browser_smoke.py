#!/usr/bin/env python3
"""Browser workflow checks. Default: native localhost. --relay: restricted-browser harness.

The relay loads unmodified application source and makes real HTTP/WebSocket
requests via Python. It does NOT verify native browser networking, microphone,
CSP enforcement, TLS, browser cookie policy, or paid voice-provider behavior.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import httpx
from playwright.sync_api import sync_playwright, expect
from browser_harness import setup, ROOT


def run(args):
    output=ROOT/'qa';output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='callbox-browser-') as tmp:
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        token='browser-test-only-'+'x'*40
        env={**os.environ,'CALLBOX_DATA_DIR':tmp,'CALLBOX_ADMIN_TOKEN':token,'CALLBOX_PORT':str(port),'CALLBOX_DEMO':'1','OPENAI_API_KEY':''}
        log=open(Path(tmp)/'server.log','w')
        server=subprocess.Popen([sys.executable,'-m','callbox'],cwd=ROOT,env=env,stdout=log,stderr=log)
        checks=[];errors=[];clients=[];live_sockets=[]
        def check(name,condition=True):
            if not condition:raise AssertionError(name)
            checks.append({'name':name,'passed':True});print('PASS '+name,flush=True)
        try:
            for _ in range(100):
                try:
                    if httpx.get(base+'/healthz',timeout=.5).status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(.08)
            else:raise RuntimeError('Server did not start')
            with sync_playwright() as p:
                executable=args.chromium or shutil.which('chromium')
                options={'headless':True,'args':['--no-sandbox']}
                if executable:options['executable_path']=executable
                browser=p.chromium.launch(**options)
                page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce',accept_downloads=True)
                page.on('pageerror',lambda e:errors.append(str(e)))
                def screenshot(name):
                    if page.locator('#toast').is_visible():expect(page.locator('#toast')).not_to_be_visible(timeout=8000)
                    page.evaluate('document.activeElement?.blur();window.scrollTo(0,0)')
                    page.wait_for_timeout(100)
                    page.screenshot(path=str(output/name),full_page=True)
                def load():
                    if args.relay:
                        client,sockets=setup(page,base);clients.append(client);live_sockets.append(sockets)
                    else:page.goto(base+'/app/')
                load()
                expect(page.locator('#demo-login')).to_be_visible()
                check('Local demo sign-in is visible')
                expect(page.locator('#admin-token')).to_have_attribute('type','password');check('Administrator token input is masked')
                page.locator('#demo-login').click();expect(page.locator('#shell')).to_be_visible()
                expect(page.locator('#main h1')).to_contain_text('A good day');check('Demo sign-in loads server-backed overview')
                check('Synthetic examples explicitly labelled','Synthetic example' in page.locator('#main').inner_text())
                check('Metrics load from database',page.locator('.metric').count()==4)
                def go(view):
                    page.evaluate('(v)=>{location.hash=v}',view)
                    expect(page.locator(f'#navigation a[href="#{view}"]')).to_have_attribute('aria-current','page')
                go('playground')
                expect(page.locator('#provider option[value="openai"]')).to_be_disabled();check('Paid provider disabled without server key')
                expect(page.locator('[data-action="record"]')).to_be_disabled();check('Microphone disabled in local mode')
                page.locator('#caller-label').fill('Mira Demo')
                page.locator('#start-form button[type=submit]').click()
                expect(page.locator('#messages')).to_contain_text('AI assistant');check('Real API session greeting is displayed')
                def turn(text, expected):
                    field=page.locator('#message-input');expect(field).to_be_enabled();field.fill(text)
                    page.locator('#turn-form button[type=submit]').click()
                    expect(page.locator('#messages')).to_contain_text(expected,timeout=15000)
                    expect(page.locator('#message-input')).to_be_enabled()
                turn('What are your hours?','10:00');check('Clinic hours come from workspace settings')
                turn('book tomorrow','Reply with the option number')
                check('Available slots exposed as actionable buttons',page.locator('.slot-options button').count()>0)
                page.locator('.slot-options button').first.click();expect(page.locator('#messages')).to_contain_text('What name');check('Slot selection advances workflow')
                turn('Mira Demo','Please confirm: Mira Demo')
                expect(page.locator('.confirm-booking-card')).to_be_visible();check('Explicit confirmation required before mutation')
                page.locator('.confirm-booking-card button').click();expect(page.locator('#messages')).to_contain_text('Confirmed in the local calendar',timeout=15000)
                check('Confirmed booking acknowledged after API result')
                screenshot('playground-desktop.png')
                go('calls');page.locator('#call-query').fill('Mira Demo')
                expect(page.locator('#calls-table .caller-name')).to_have_count(1);check('Conversation search filters records')
                page.locator('#calls-table .caller-name').click();expect(page.locator('#detail-dialog')).to_be_visible();check('Stored transcript opens in native dialog')
                expect(page.locator('#resume-call')).to_be_visible();check('Active session can be recovered from history')
                with page.expect_download() as d:page.locator('#export-call').click()
                payload=json.loads(Path(d.value.path()).read_text());check('Transcript JSON export contains session data','Mira Demo' in json.dumps(payload))
                page.locator('#resume-call').click();expect(page.locator('#message-input')).to_be_enabled();check('Resume restores active conversation state')
                turn('I want to speak to a human','callback request');check('Staff request created without claiming transfer')
                page.locator('[data-action="end-call"]').click();expect(page.locator('.session-bar')).to_contain_text('SESSION ENDED');check('Session ends and remains stored')
                go('calendar');expect(page.locator('.appointment-item')).to_contain_text('Mira Demo');check('Confirmed appointment appears in calendar')
                check('External calendar integration not misrepresented','NOT GOOGLE CALENDAR' in page.locator('#main').inner_text())
                page.locator('[data-action="cancel-appointment"]').click();expect(page.locator('#confirm-dialog')).to_be_visible();check('Cancellation requires operator confirmation')
                page.locator('#confirm-action').click();expect(page.locator('.appointment-item')).to_contain_text('CANCELLED');check('Confirmed cancellation updates database-backed UI')
                go('devices');page.locator('[data-action="new-device"]').click();page.locator('#device-name').fill('UI test gateway')
                page.locator('#device-form [type=submit]').click();expect(page.locator('#one-time-token')).to_be_visible();check('Device provisioning returns one-time credential')
                secret=page.locator('#one-time-token').inner_text();check('Device secret has substantial random length',len(secret)>=32)
                page.locator('[aria-label="Close device dialog"]').click();expect(page.locator('#device-secret')).to_be_empty();check('One-time token erased from DOM on close')
                card=page.locator('.device-card').filter(has_text='UI test gateway');card.locator('[data-action="revoke-device"]').click();page.locator('#confirm-action').click()
                expect(card).to_contain_text('REVOKED');check('Device can be revoked')
                page.locator('[data-action="echo"]').click();expect(page.locator('[data-action="export-echo"]')).to_be_visible(timeout=20000)
                with page.expect_download() as d:page.locator('[data-action="export-echo"]').click()
                report=json.loads(Path(d.value.path()).read_text())
                check('Browser loopback verifies all 50 frames',report['exact_matches']==50)
                check('Browser loopback exercises interruption',report['interrupt_acknowledged'])
                check('Loopback result does not claim hardware proof',not report['hardware_verified'] and not report['phone_call_placed'])
                report['test_environment']='Python HTTP/WebSocket relay' if args.relay else 'Native browser networking'
                (output/'browser-loopback.json').write_text(json.dumps(report,indent=2)+'\n')
                screenshot('devices-desktop.png')
                go('settings');page.locator('#business-name').fill('Willow Test Clinic');page.locator('#settings-form [type=submit]').click()
                expect(page.locator('#sidebar-business')).to_have_text('Willow Test Clinic');check('Workspace edits are persisted and reflected in navigation')
                expect(page.locator('#settings-error')).to_be_empty();check('Workspace update reports no validation error')
                page.locator('#business-name').fill('Willow Clinic');page.locator('#settings-form [type=submit]').click();expect(page.locator('#sidebar-business')).to_have_text('Willow Clinic')
                check('Settings show truthful provider availability','NOT CONFIGURED' in page.locator('#main').inner_text())
                go('pipeline')
                for key,label in [('bluetooth','HARDWARE VALIDATION REQUIRED'),('sip','DESIGN ONLY'),('browser','WORKING IN THIS BUILD')]:
                    page.locator(f'[data-pipeline="{key}"]').click();expect(page.locator('.pipeline-overview')).to_contain_text(label);check('Pipeline status: '+key)
                go('overview');check('Staff queue displays requests',page.locator('[data-action="resolve"]').count()>=1)
                page.locator('[data-action="resolve"]').first.click();expect(page.locator('#toast')).to_contain_text('resolved');check('Staff request can be resolved')
                screenshot('console-desktop.png')
                for width in [320,360,390,768,1024,1440]:
                    page.set_viewport_size({'width':width,'height':900})
                    for view in ['overview','playground','calls','calendar','devices','pipeline','settings']:
                        go(view)
                        dims=page.evaluate('({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth})')
                        if dims['scroll']>dims['width']:
                            print(json.dumps(page.evaluate('''[...document.querySelectorAll("#shell *")].filter(e=>e.getBoundingClientRect().right>innerWidth+1&&e.getBoundingClientRect().width>0).map(e=>({tag:e.tagName,cls:e.className,width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right})).slice(0,30)''')))
                            page.screenshot(path=str(output/'overflow-debug.png'),full_page=True)
                        check(f'No page overflow: {view} at {width}px',dims['scroll']<=dims['width'])
                page.set_viewport_size({'width':390,'height':844});go('overview')
                page.locator('#menu-button').click();expect(page.locator('#menu-button')).to_have_attribute('aria-expanded','true');check('Mobile navigation opens')
                page.keyboard.press('Escape');expect(page.locator('#menu-button')).to_have_attribute('aria-expanded','false');check('Escape closes mobile navigation')
                check('Navigation focus restored',page.locator('#menu-button').evaluate('(e)=>e===document.activeElement'))
                screenshot('console-mobile.png')
                go('devices');page.locator('[data-action="new-device"]').click();page.keyboard.press('Escape');expect(page.locator('#device-dialog')).not_to_be_visible();check('Escape dismisses native device dialog')
                check('No browser page errors',not errors)
                browser.close()
            result={'test_mode':'restricted-browser relay' if args.relay else 'native-browser','checks':checks,'passed':len(checks),'page_errors':errors,'paid_api_calls':0,'hardware_tested':False,'native_browser_networking_tested':not args.relay}
            (output/'browser-results.json').write_text(json.dumps(result,indent=2)+'\n')
            print(json.dumps({'checks_passed':len(checks),'errors':errors,'mode':result['test_mode']}))
        finally:
            for sockets in live_sockets:
                for ws in sockets.values():ws.close()
            for client in clients:client.close()
            server.terminate()
            try:server.wait(timeout=5)
            except subprocess.TimeoutExpired:server.kill();server.wait()
            log.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--relay',action='store_true',help='Use restricted-browser transport shim, not native browser fetch/WebSocket')
    parser.add_argument('--chromium',help='Optional Chromium executable path')
    run(parser.parse_args())
