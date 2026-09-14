/* CallBox operator console. Deliberately no UI framework or build dependency. */
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const requestId = () => {
 if (crypto.randomUUID) return crypto.randomUUID();
 const bytes = crypto.getRandomValues(new Uint8Array(16));
 return [...bytes].map(v=>v.toString(16).padStart(2,'0')).join('');
};
const paths = {
  overview: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  playground: '<path d="M7 3H4a1 1 0 0 0-1 1c0 9.4 7.6 17 17 17a1 1 0 0 0 1-1v-3l-5-2-2 2c-3-1-6-4-7-7l2-2-2-5Z"/><path d="M14 3a7 7 0 0 1 7 7M14 7a3 3 0 0 1 3 3"/>',
  calls: '<path d="M4 4h16v12H9l-5 4V4Z"/><path d="M8 8h8M8 12h5"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 11h18M8 15h2M14 15h2"/>',
  devices: '<rect x="3" y="8" width="18" height="12" rx="3"/><path d="M7 8V3M8 14h1M12 14h1M16 14h1"/>',
  pipeline: '<rect x="3" y="3" width="6" height="6" rx="1"/><rect x="15" y="15" width="6" height="6" rx="1"/><path d="M6 9v9h9M9 6h9v9"/>',
  settings: '<path d="m9 3-1 3-3 1-2 5 2 5 3 1 1 3h6l1-3 3-1 2-5-2-5-3-1-1-3Z"/><circle cx="12" cy="12" r="3"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  mic: '<rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v4M8 22h8"/>',
};
const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.check}</svg>`;
const nav = [['overview','Overview'],['playground','Call playground'],['calls','Conversations'],['calendar','Local calendar'],['devices','Devices'],['pipeline','Build pipeline'],['settings','Workspace']];
const state = {user:null,summary:{},calls:[],appointments:[],tasks:[],devices:[],events:[],view:'overview',activeCall:null,actions:[],busy:false,query:'',callFilter:'all',pipeline:'browser',date:null,slots:[],echo:null,recorder:null,recording:false,mediaStream:null,lastElapsed:null};
let toastTimer, pollTimer, detailId, confirmFn, recordTimer, audioPlayer;
const timeOf = v => v ? new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',hour:'numeric',minute:'2-digit'}).format(new Date(v)) : '--';
const dateOf = v => new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'numeric',month:'short'}).format(new Date(v));
const initials = v => String(v).replace(/ - sample$/, '').split(' ').filter(Boolean).slice(0,2).map(s=>s[0]).join('').toUpperCase();
function tomorrow(){const d=new Date(Date.now()+86400000);return new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).format(d);}
function toast(message,error=false){clearTimeout(toastTimer);const el=$('#toast');el.textContent=message;el.classList.toggle('error',error);el.hidden=false;toastTimer=setTimeout(()=>el.hidden=true,6000);}
async function api(path, options={}){
 const headers={...options.headers};
 if(options.body && !(options.body instanceof Blob) && !(options.body instanceof ArrayBuffer)){headers['Content-Type']='application/json';options.body=JSON.stringify(options.body);}
 const r=await fetch('/api'+path,{credentials:'same-origin',...options,headers});
 let body;try{body=await r.json();}catch{body={};}
 if(!r.ok){
  if(r.status===401 && state.user)showLogin();
  const message=body.error?.message || (Array.isArray(body.detail)?body.detail.map(x=>x.msg).join('; '):'Request failed. Check the local server.');
  throw new Error(message);
 }
 return body;
}
const post=(path,body={})=>api(path,{method:'POST',body});
function showLogin(){state.user=null;clearInterval(pollTimer);$('#shell').hidden=true;$('#login-screen').hidden=false;stopAudio();}
async function loadWorkspace(){
 state.user=await api('/workspace');
 await refresh();
 $('#login-screen').hidden=true;$('#shell').hidden=false;
 $('#sidebar-business').textContent=state.user.settings.name;
 $('.workspace-avatar').textContent=state.user.settings.name[0].toUpperCase();
 state.view=nav.some(([id])=>id===location.hash.slice(1))?location.hash.slice(1):'overview';
 renderNav();render();
 clearInterval(pollTimer);pollTimer=setInterval(async()=>{
  try{await refresh();connection(true);if(state.view==='overview' && !document.querySelector('dialog[open]'))render();}catch{connection(false);}
 },4500);
}
async function refresh(){
 const result=await Promise.all(['/summary','/calls','/appointments','/tasks','/devices','/events'].map(p=>api(p)));
 [state.summary,state.calls,state.appointments,state.tasks,state.devices,state.events]=result;
 const count=$('[data-count="review"]');if(count)count.textContent=state.summary.review;
}
function connection(ok){const e=$('#connection-status');e.classList.toggle('offline',!ok);e.innerHTML=`<i></i>${ok?'Local server':'Server offline'}`;}
function renderNav(){$('#navigation').innerHTML=nav.map(([id,label])=>`<a class="nav-item ${id===state.view?'active':''}" href="#${id}" ${id===state.view?'aria-current="page"':''}>${icon(id)}<span>${label}</span>${id==='calls'?`<span class="nav-count" data-count="review">${state.summary.review||0}</span>`:''}</a>`).join('');}
function navigate(view){if(location.hash==='#'+view){state.view=view;render();}else location.hash=view;}
function closeMenu(){$('#sidebar').classList.remove('open');$('#nav-backdrop').hidden=true;$('#menu-button').setAttribute('aria-expanded','false');}
function pageHeading(title,description,action='',eyebrow='YOUR WORKSPACE'){
 return `<div class="page-heading"><div><span class="eyebrow">${esc(eyebrow)}</span><h1>${title}</h1><p>${description}</p></div>${action?`<div class="heading-actions">${action}</div>`:''}</div>`;
}
function render(){
 renderNav();$('#page-name').textContent=nav.find(([id])=>id===state.view)?.[1]||'Overview';
 const views={overview:overview,playground:playground,calls:callsPage,calendar:calendarPage,devices:devicesPage,pipeline:pipelinePage,settings:settingsPage};
 $('#main').innerHTML=(views[state.view]||overview)();
 if(state.view==='playground')scrollMessages();
 if(state.view==='calendar')fetchAvailability();
 if(state.view==='calls')$('#call-query')?.addEventListener('input',e=>{state.query=e.target.value;renderCallsTable();});
}
function callsTable(calls,compact=false){
 if(!calls.length)return `<div class="empty"><div class="empty-symbol">${icon('calls')}</div><strong>No conversations yet.</strong><p>Start a test session to see its transcript and outcome here.</p><button class="button secondary" data-action="go" data-view="playground">Start a test</button></div>`;
 return `<div class="table-wrap"><table class="table"><thead><tr><th>CALLER / SESSION</th><th>OUTCOME</th><th class="${compact?'mobile-hide':''}">TIME (IST)</th><th class="desktop-only">SOURCE</th><th><span class="muted">VIEW</span></th></tr></thead><tbody>${calls.map((c,i)=>`<tr><td><div class="caller-cell"><span class="caller-avatar tone-${i%4}">${esc(initials(c.label))}</span><div><button class="caller-name" data-action="detail" data-id="${esc(c.id)}">${esc(c.label)}</button><small>${c.sample?'Synthetic example':c.status==='active'?'Active test session':'Stored test session'}</small></div></div></td><td><span class="pill ${c.outcome.includes('review')?'warning':c.outcome.includes('booked')?'success':'neutral'}">${esc(c.outcome.replace(' (local calendar)','').replace('Staff review requested','Staff review').replace('In progress','Active'))}</span></td><td class="${compact?'mobile-hide':''}">${timeOf(c.started_at)}</td><td class="desktop-only"><span class="tag">${esc(c.source)}</span></td><td><button class="mini-button" data-action="detail" data-id="${esc(c.id)}" aria-label="View ${esc(c.label)}">&#8599;</button></td></tr>`).join('')}</tbody></table></div>`;
}
function overview(){
 const s=state.summary, recent=state.calls.slice(0,5), completed=state.calls.filter(c=>c.status==='ended').length;
 const reviewed=state.calls.filter(c=>c.outcome.includes('review')).length;
 const metrics=[['Test sessions',s.calls,'Includes '+s.sample_calls+' synthetic examples','playground'],['Local bookings',s.booked,'Confirmed in this workspace','calendar'],['Needs a person',s.review,'Open staff-review requests','calls'],['Gateways online',s.online,'Network connection, not Bluetooth','devices']];
 return `${pageHeading('A good day to connect.','Your conversations, tools, and gateways. All in one place.',`<button class="button primary" data-action="go" data-view="playground">${icon('playground')} New test call</button>`,esc(s.today||'LOCAL WORKSPACE'))}
 <section class="hero-banner"><div class="hero-text"><span class="eyebrow">LESS REPETITION. MORE CONNECTION.</span><h2>Your front desk.<br>A little more human.</h2><p>Test an appointment, find the right information, and leave the important decisions to your team.</p><button class="button primary" data-action="go" data-view="playground">Try the conversation <span aria-hidden="true">&rarr;</span></button></div><div class="hero-visual"><div class="orbit"></div><span class="float-tag"><span class="status-dot"></span>One shared voice platform</span><img src="/assets/callbox-device.webp" alt="CallBox concept device, shown for product visualization only"><span class="concept-tag">CONCEPT RENDER / HARDWARE UNVERIFIED</span></div></section>
 <section class="metrics" aria-label="Workspace statistics">${metrics.map(([label,value,note,name])=>`<div class="card metric"><div class="metric-head">${label}<span class="metric-icon">${icon(name)}</span></div><strong>${value??0}</strong><small>${esc(note)}</small></div>`).join('')}</section>
 <div class="grid-two"><div class="stack"><section class="card"><div class="card-header"><div><h3>Recent conversations</h3><p>Every test leaves a useful, inspectable record.</p></div><a href="#calls" class="text-link">View all &rarr;</a></div>${callsTable(recent,true)}<div class="card-foot">${s.active||0} active tests &nbsp;&middot;&nbsp; Transcripts stored locally &nbsp;&middot;&nbsp; No raw audio stored</div></section>
 <section class="card"><div class="card-header"><div><h3>The human handoff queue</h3><p>A request for staff, never a pretend phone transfer.</p></div><span class="pill warning">${s.review||0} OPEN</span></div>${state.tasks.filter(t=>t.status==='open').slice(0,3).map(t=>`<div class="review-row"><span class="activity-icon">${icon('calls')}</span><div><strong>${esc(t.reason)}</strong><p>${timeOf(t.created_at)} &middot; Awaiting operator review</p></div><button class="button secondary" data-action="resolve" data-id="${esc(t.id)}">Resolve</button></div>`).join('')||'<div class="empty">Nothing waiting for review.</div>'}</section></div>
 <div class="stack"><section class="card"><div class="card-header"><div><h3>Activity, as it happens</h3><p>From the actual application event log.</p></div><span class="status-dot"></span></div><div class="activity-list">${state.events.slice(0,4).map(e=>`<div class="activity-row"><span class="activity-icon">${icon(e.kind.includes('appointment')?'calendar':e.kind.includes('device')?'devices':'check')}</span><div><strong>${esc(e.summary)}</strong><small>${esc(e.kind)}</small></div><span class="event-time">${timeOf(e.created_at)}</span></div>`).join('')}</div></section>
 <section class="card"><div class="card-header"><div><h3>Session snapshot</h3><p>Counts from this workspace, not sales claims.</p></div></div><div class="progress-row"><div class="progress-label"><span>Completed tests</span><strong>${completed} / ${s.calls||0}</strong></div><progress max="${Math.max(s.calls||1,1)}" value="${completed}"></progress></div><div class="progress-row"><div class="progress-label"><span>Flagged for people</span><strong>${reviewed} / ${s.calls||0}</strong></div><progress max="${Math.max(s.calls||1,1)}" value="${reviewed}"></progress></div><p class="outcome-note">Includes the clearly marked example records.</p><div class="transport-mini">${icon('pipeline')}<div><strong>The browser path is ready.</strong><p>Bluetooth and phone-network paths are not connected.</p></div></div></section></div></div>`;
}
function messageList(call){return (call?.messages||[]).map(m=>`<div class="message ${m.role==='caller'?'caller':'assistant'}"><span class="message-avatar">${m.role==='caller'?'YOU':'CB'}</span><div><div class="message-content">${esc(m.text)}</div><div class="message-meta">${m.role==='caller'?'Test caller':'CallBox assistant'} &middot; ${timeOf(m.created_at)}</div></div></div>`).join('');}
function playground(){
 const c=state.activeCall, active=c?.status==='active', configured=state.user.provider_configured;
 const suggestions=[['Book tomorrow','book tomorrow'],['Opening hours','What are your opening hours?'],['Ask for a person','I want to speak to a human.'],['Clinical boundary','Can I change my medicine dose?']];
 const slots=c?.state?.step==='slot'?c.state.slots:[];
 return `${pageHeading('Make the first connection.','One test conversation, real application actions. No telephone required.',c?`<button class="button secondary" data-action="detail" data-id="${c.id}">Session record &#8599;</button>`:'','CALL PLAYGROUND')}
 <div class="playground-grid"><section class="card session-card"><div class="session-bar"><span class="session-icon">${icon('playground')}</span><div><strong>${c?esc(c.label):'A conversation starts here.'}</strong><small>${c?esc(c.provider==='local'?'Local rules + real tools':'Optional paid speech + tools'):'Choose a mode and start a session.'}</small></div><span class="pill ${active?'success':'neutral'}">${active?'TEST ACTIVE':c?'SESSION ENDED':'NOT A PHONE CALL'}</span></div><div class="messages" id="messages" aria-live="polite" aria-relevant="additions">${c?messageList(c):'<div class="message-empty"><div class="empty-symbol">&#8614;</div><h3>Say hello to CallBox.</h3><p>Book a fictional appointment or test a staff request. Every result comes from the server, not a prerecorded script.</p></div>'}</div>
 ${state.busy?'<div class="busy-label" role="status">Processing this turn...</div>':''}
 ${slots.length?`<div class="slot-options">${slots.map((s,i)=>`<button data-action="message" data-text="${i+1}" ${state.busy?'disabled':''}>${i+1}. ${esc(s.label)}</button>`).join('')}</div>`:''}
 ${c?.state?.step==='confirm'?`<div class="confirm-booking-card">Nothing is booked until you confirm.<br><button class="button primary" data-action="message" data-text="confirm" ${state.busy?'disabled':''}>Confirm local booking</button></div>`:''}
 <div class="composer"><div class="quick-prompts">${suggestions.map(([label,text])=>`<button class="prompt-chip" data-action="message" data-text="${esc(text)}" ${!active||state.busy||state.recording?'disabled':''}>${label}</button>`).join('')}</div><form class="composer-form" id="turn-form"><input id="message-input" aria-label="Your message" maxlength="2000" autocomplete="off" placeholder="Type a message as your test caller..." ${!active||state.busy||state.recording?'disabled':''} required><button class="button primary" type="submit" ${!active||state.busy||state.recording?'disabled':''}>Send &rarr;</button></form><div class="composer-note"><span>Fictional information only. Real local database writes.</span><span>${state.lastElapsed!==null?esc(state.lastElapsed)+' ms server processing':'Asia/Kolkata'}</span></div></div></section>
 <div class="stack"><section class="card"><form id="start-form" class="session-config"><h3>Set the scene.</h3><p>Start locally, then opt into speech when your provider key is ready.</p><label for="caller-label">Test caller</label><input id="caller-label" value="${esc(c?.label||'New test caller')}" maxlength="80" required ${active?'disabled':''}><label for="language">Conversation language</label><select id="language" ${active?'disabled':''}><option value="en" ${c?.language!=='hinglish'?'selected':''}>English</option><option value="hinglish" ${c?.language==='hinglish'?'selected':''}>Hinglish (local examples)</option></select><label for="provider">Processing mode</label><select id="provider" ${active?'disabled':''}><option value="local" ${c?.provider!=='openai'?'selected':''}>Local rules - no paid API</option><option value="openai" ${!configured?'disabled':''} ${c?.provider==='openai'?'selected':''}>OpenAI speech ${configured?'(paid)':'- key not configured'}</option></select><label class="checkbox"><input type="checkbox" id="voice-consent" ${active?'disabled':''} ${c?.consent?'checked':''}><span>I consent to sending this synthetic test content to the configured provider. Required only for paid mode.</span></label>${active?'<button class="button danger wide" type="button" data-action="end-call">End this test session</button>':`<button class="button primary wide" type="submit" ${state.busy?'disabled':''}>${c?'Start a new test':'Start test session'} &rarr;</button>`}<div class="hint">The local parser supports a narrow set of requests. It is not a general AI or a clinically validated triage system.</div></form><div class="voice-controls"><button class="button secondary" data-action="record" ${!active||c?.provider!=='openai'||state.busy?'disabled':''}>${state.recording?'Stop recording':'Record a turn'}</button><button class="button secondary" data-action="read-aloud" ${!c||!('speechSynthesis' in window)?'disabled':''}>Read last reply</button></div></section>
 <section class="card"><div class="card-header"><div><h3>Tool activity</h3><p>Only validated actions change the workspace.</p></div></div>${state.actions.length?state.actions.slice(-5).reverse().map(a=>`<div class="tool-row"><span class="pill success">${esc(a.status)}</span><code>${esc(a.tool)}()</code><p>${a.appointment?'Committed to the local calendar':a.date?'Date: '+esc(a.date):'Checked by the application backend'}</p></div>`).join(''):'<div class="empty small">Tool results appear after a request.<br>No fabricated success messages.</div>'}<div class="card-foot">Provider audio is transient. Text is stored locally.</div></section></div></div>`;
}
function callsPage(){
 return `${pageHeading('Every conversation, in context.','Search the stored transcript records and inspect what actually happened.',`<button class="button primary" data-action="go" data-view="playground">New test call &rarr;</button>`,'CONVERSATIONS')}<div class="search-bar"><input id="call-query" type="search" placeholder="Search caller or outcome..." value="${esc(state.query)}" aria-label="Search calls"><select id="call-filter" aria-label="Call source filter"><option value="all" ${state.callFilter==='all'?'selected':''}>All sessions</option><option value="tests" ${state.callFilter==='tests'?'selected':''}>Your test sessions</option><option value="sample" ${state.callFilter==='sample'?'selected':''}>Synthetic examples</option></select><span class="muted small">${state.calls.length} stored records</span></div><section class="card"><div id="calls-table">${callsTable(filteredCalls())}</div><div class="card-foot">JSON exports include the transcript. Handle them as personal data when using real systems.</div></section><div class="hint section-heading">This lab has no call recording, automatic diagnosis, sentiment score, or claimed AI confidence percentage.</div>`;
}
function filteredCalls(){const q=state.query.toLowerCase();return state.calls.filter(c=>(state.callFilter==='all'||(state.callFilter==='sample'?c.sample:!c.sample)) && (c.label+' '+c.outcome).toLowerCase().includes(q));}
function renderCallsTable(){const el=$('#calls-table');if(el)el.innerHTML=callsTable(filteredCalls());}
function calendarPage(){
 state.date ||= tomorrow();
 return `${pageHeading('Make room for what matters.','A local test calendar with collision checks and explicit confirmation.',`<button class="button primary" data-action="go" data-view="playground">Book through a test &rarr;</button>`,'LOCAL CALENDAR')}<div class="grid-equal"><section class="card"><div class="card-header"><div><h3>Confirmed &amp; cancelled</h3><p>Real rows in the local SQLite calendar.</p></div><span class="pill neutral">NOT GOOGLE CALENDAR</span></div>${state.appointments.length?state.appointments.map(a=>`<div class="appointment-item"><div class="date-block"><strong>${new Intl.DateTimeFormat('en',{day:'numeric',timeZone:'Asia/Kolkata'}).format(new Date(a.starts_at))}</strong><small>${new Intl.DateTimeFormat('en',{month:'short',timeZone:'Asia/Kolkata'}).format(new Date(a.starts_at))}</small></div><div class="appointment-info"><strong>${esc(a.name)}</strong><p>${timeOf(a.starts_at)} IST &middot; ${a.duration} minutes</p><span class="pill ${a.status==='confirmed'?'success':'neutral'}">${a.status.toUpperCase()}</span></div>${a.status==='confirmed'?`<button class="button secondary" data-action="cancel-appointment" data-id="${a.id}">Cancel</button>`:''}</div>`).join(''):'<div class="empty"><div class="empty-symbol">'+icon('calendar')+'</div><strong>Your first appointment goes here.</strong><p>Try "book tomorrow" in the call playground.</p></div>'}</section><section class="card card-pad"><div class="calendar-head"><h3>Available slots</h3><input type="date" id="availability-date" value="${esc(state.date)}" aria-label="Availability date"></div><p class="small muted">All times are Asia/Kolkata. The demo schedule runs every day; holidays and multiple practitioners are not modeled.</p><div id="available-slots"><div class="loading">Checking calendar...</div></div></section></div>`;
}
async function fetchAvailability(){
 const date=state.date;
 try{const slots=await api('/availability?date='+encodeURIComponent(date));if(state.view!=='calendar'||date!==state.date)return;state.slots=slots;$('#available-slots').innerHTML=slots.length?`<div class="slots-grid">${slots.map(s=>`<div class="slot-card">${esc(s.label)}<small>${s.duration} min &middot; available</small></div>`).join('')}</div>`:'<div class="empty">No slots available on this date.</div>';}
 catch(e){if($('#available-slots'))$('#available-slots').innerHTML=`<p class="inline-error">${esc(e.message)}</p>`;}
}
function devicesPage(){
 return `${pageHeading('The bridge to your phone.','Provision a gateway identity. Prove the network path before adding Bluetooth.',`<button class="button primary" data-action="new-device">+ Add gateway</button>`,'DEVICES & TRANSPORT')}
 <section class="card test-panel"><div><span class="eyebrow">A REAL NETWORK TEST. NOT A REAL PHONE CALL.</span><h3>Listen. Send. Receive.</h3><p>The browser sends 50 synthetic PCM frames through the authenticated WebSocket gateway. The server returns them. We compare every byte and measure the round trip.</p><button class="button primary" data-action="echo" ${state.echo?.running?'disabled':''}>${state.echo?.running?'Testing gateway...':'Run audio loopback test'} &rarr;</button>${state.echo?.report?'<button class="button secondary" data-action="export-echo">Export result</button>':''}<progress id="echo-progress" class="test-progress" max="50" value="${state.echo?.frames||0}" aria-label="Audio test frames verified"></progress></div><div class="terminal"><div class="terminal-caption"><span>CALLBOX / TRANSPORT LAB</span><span>PCM16 &middot; 16 kHz</span></div><div id="echo-log">${state.echo?.log?.map(line=>`<p>${esc(line)}</p>`).join('')||'<p>&gt; Awaiting a test.</p><p>&gt; Device tokens are authenticated.</p><p>&gt; Echoed audio is not saved.</p><p>&gt; Bluetooth / HFP still needs hardware proof.</p>'}</div></div></section>
 <div class="section-heading"><h3>Your provisioned gateways</h3><span class="muted small">${state.devices.length} device identities</span></div><section class="device-grid">${state.devices.map(d=>`<article class="card device-card"><div class="device-top"><span class="device-box">${icon('devices')}</span><span class="pill ${d.revoked?'warning':d.status==='online'?'success':'neutral'}">${d.revoked?'REVOKED':d.status.toUpperCase()}</span></div><h3>${esc(d.name)}</h3><p class="device-id">${esc(d.id)}</p><div class="device-detail"><span>Type</span><strong>${esc(d.kind)}</strong></div><div class="device-detail"><span>Audio frames</span><strong>${d.frames}</strong></div><div class="device-detail"><span>Last connection</span><strong>${d.last_seen?timeOf(d.last_seen):'Not connected'}</strong></div><div class="device-detail"><span>Hardware proof</span><strong>Not validated</strong></div>${!d.revoked?`<button class="button secondary" data-action="revoke-device" data-id="${d.id}">Revoke credentials</button>`:''}</article>`).join('')||'<div class="card empty"><div class="empty-symbol">'+icon('devices')+'</div><strong>No gateways provisioned.</strong><p>Add a simulator or an experimental device identity.</p></div>'}</section>`;
}
const pipelines={
 browser:{pill:'WORKING IN THIS BUILD',title:'A useful conversation, end to end.',description:'The browser and simulator reach the same application tools. Local rules work with no paid API. The optional speech adapter is implemented, but needs your API key and a live provider smoke test.',steps:[['Browser / simulator','Text, or opt-in microphone turn'],['API / gateway','Auth, limits, lifecycle, framing'],['Rules / intent','Narrow request interpretation'],['Validated tools','Confirmation + availability checks'],['Local records','Calendar, transcript, staff queue']]},
 bluetooth:{pill:'HARDWARE VALIDATION REQUIRED',title:'Your existing phone, a new interface.',description:'The concept is a Bluetooth hands-free bridge. This release supplies a protocol, device provisioning, audio tests and a portable firmware buffer. It does not contain a validated ESP32 HFP driver, pairing experience, or working phone handoff.',steps:[['Phone + SIM','Real cellular call on your number'],['HFP-capable ESP32','Unbuilt phone-specific adapter'],['PCM gateway','Implemented test protocol'],['Voice pipeline','Turn-based provider integration'],['Human fallback','Must prove handset recovery']]},
 sip:{pill:'DESIGN ONLY / NOT CONNECTED',title:'A path for more than one line.',description:'For multi-line deployment, a licensed carrier and a properly configured PBX would feed an adapter into this application. SIP, carrier routing, billing, live transfers and CPaaS webhooks are not implemented in this release.',steps:[['Licensed carrier','Number + network connectivity'],['SIP / PBX','Call control + media extraction'],['Carrier adapter','To implement and certify'],['CallBox tools','Reuse the server workflow layer'],['Operator queue','Real telephony transfer needed']]},
};
function pipelinePage(){
 const p=pipelines[state.pipeline];
 const milestones=[['01','Application foundation','Persistent sessions, calendar tools, staff queue and operator UI.','Implemented'],['02','Transport laboratory','Authenticated PCM echo, bounded buffers, interruption epochs, simulator.','Implemented'],['03','Paid speech validation','Adapter code and mock HTTP tests. Requires an API-key smoke test.','Needs live test'],['04','Phone / ESP32 audio proof','Answer a consented test call and prove two-way HFP audio on the target handset.','Not built'],['05','Supervised pilot','Telecom terms, security review, fallback, language evaluation and clinic-approved safety.','Blocked']];
 return `${pageHeading('One platform. Several ways in.','Connection hardware and agent intelligence are separate workstreams.','','BUILD PIPELINE')}<div class="pipeline-tabs" role="tablist" aria-label="Connection route">${[['browser','Browser & simulator'],['bluetooth','Bluetooth concept'],['sip','SIP / carrier']].map(([id,label])=>`<button role="tab" aria-selected="${state.pipeline===id}" class="${state.pipeline===id?'active':''}" data-action="pipeline" data-pipeline="${id}">${label}</button>`).join('')}</div><section class="card pipeline-overview"><span class="pill ${state.pipeline==='browser'?'success':'warning'}">${p.pill}</span><h2>${p.title}</h2><p>${p.description}</p><div class="pipeline-track">${p.steps.map(([title,description],i)=>`<div class="pipeline-node"><span class="step">0${i+1}</span><strong>${title}</strong><p>${description}</p></div>`).join('')}</div><div class="hint">A confirmed database action is not a confirmed phone connection. Status and error messages keep that distinction explicit.</div></section><div class="grid-equal section-heading"><section class="card"><div class="card-header"><h3>The implementation gates</h3></div><div class="milestone-list">${milestones.map(([n,title,description,status],i)=>`<div class="milestone"><span class="milestone-bullet ${i>1?'pending':''}">${i<2?'&#10003;':n}</span><div><strong>${title}</strong><p>${description}</p></div><span class="pill ${i<2?'success':'neutral'}">${status}</span></div>`).join('')}</div></section><section class="card"><div class="card-header"><div><h3>Device contract</h3><p>Versioned and exercised by the simulator.</p></div></div><div class="card-pad"><pre class="code">${esc(JSON.stringify({type:'hello',protocol:'callbox.v1',device_id:'dev_...',token:'<one-time provisioned token>'},null,2))}</pre><br><p class="small muted">Follow with call.start, 20 ms PCM16 frames, interrupt, and call.end. Each frame carries the active call ID, monotonic sequence, and playback epoch.</p><br><div class="hint">Never put a device token in a URL. No public phone-number spoofing, SIM farming, or unapproved outbound dialing is included.</div><br><a class="text-link" href="/docs" target="_blank" rel="noopener">Explore the HTTP API &#8599;</a></div></section></div>`;
}
function settingsPage(){
 const s=state.user.settings, configured=state.user.provider_configured;
 return `${pageHeading('A workspace that sounds like you.','Configure the example business. Keep real patient information out of this lab.','','WORKSPACE SETTINGS')}<div class="grid-equal"><form class="card settings-card" id="settings-form"><h3>Business details</h3><p>Saved to this workspace, used by the receptionist tools.</p><label for="business-name">Business name</label><input name="name" id="business-name" value="${esc(s.name)}" minlength="2" maxlength="80" required><div class="field-row"><div><label for="business-type">Business type</label><select name="business" id="business-type">${[['clinic','Clinic'],['salon','Salon'],['service','Service desk']].map(([id,n])=>`<option value="${id}" ${s.business===id?'selected':''}>${n}</option>`).join('')}</select></div><div><label for="fee">Demo fee (INR)</label><input id="fee" name="fee" type="number" min="0" max="100000" value="${s.fee}" required></div></div><label for="business-address">Demo address</label><input id="business-address" name="address" value="${esc(s.address)}" minlength="2" maxlength="180" required><div class="field-row"><div><label for="open-hour">Opening hour (0-22)</label><input id="open-hour" name="open_hour" type="number" min="0" max="22" value="${s.open_hour}" required></div><div><label for="close-hour">Closing hour (1-23)</label><input id="close-hour" name="close_hour" type="number" min="1" max="23" value="${s.close_hour}" required></div></div><div class="field-row"><div><label for="slot-minutes">Slot duration</label><select id="slot-minutes" name="slot_minutes">${[15,20,30,60].map(v=>`<option value="${v}" ${s.slot_minutes===v?'selected':''}>${v} minutes</option>`).join('')}</select></div><div><label for="tz">Timezone</label><input id="tz" value="Asia/Kolkata" readonly aria-label="Timezone: Asia/Kolkata"></div></div><label for="staff-label">Staff queue name</label><input id="staff-label" name="staff_label" value="${esc(s.staff_label)}" minlength="2" maxlength="80" required><button class="button primary" type="submit">Save workspace &rarr;</button><p class="inline-error" id="settings-error" role="alert"></p></form><div class="stack"><section class="card"><div class="card-header"><div><h3>Connections, honestly.</h3><p>Nothing is silently connected to a real account.</p></div></div>${[['DB','Local calendar','SQLite with transactional booking','CONNECTED',true],['AI','OpenAI speech',configured?'Server key present; live testing still required':'Set OPENAI_API_KEY on the server',configured?'CONFIGURED':'NOT CONFIGURED',configured],['GC','Google Calendar','OAuth and synchronization not implemented','NOT CONNECTED',false],['BT','Bluetooth / ESP32','Physical audio-path validation still required','NOT VALIDATED',false],['PH','Phone provider / SIP','Carrier connection and routing not implemented','NOT CONNECTED',false]].map(([letter,title,description,status,ok])=>`<div class="integration-row"><span class="integration-letter">${letter}</span><div><strong>${title}</strong><p>${description}</p></div><span class="pill ${ok?'success':'neutral'}">${status}</span></div>`).join('')}<p class="integration-note">API keys live only on the backend. A connection to Calendar in ChatGPT does not automatically authorize this separate application.</p></section><section class="card card-pad"><span class="eyebrow">SECURITY & PRIVACY</span><h3>Stay in the lab.</h3><br><p class="small muted">This is a single-process development release with local authentication, device tokens, scoped records, bounded media, and no raw-audio storage. It is not a production security or healthcare compliance certification.</p><br><div class="hint">Raw recordings are transient. Transcripts, names and provider-derived text are retained in SQLite until you delete or prune them. Use the supplied retention command.</div></section></div></div>`;
}
function scrollMessages(){const el=$('#messages');if(el)el.scrollTop=el.scrollHeight;}
async function startSession(form){
 if(state.busy)return;
 state.busy=true;
 try{
  const body={label:$('#caller-label').value,language:$('#language').value,source:'browser',provider:$('#provider').value,consent:$('#voice-consent').checked};
  state.activeCall=await post('/calls',body);state.actions=[];state.lastElapsed=null;await refresh();toast('Test session started. No phone call was placed.');
 }catch(e){toast(e.message,true);}finally{state.busy=false;render();$('#message-input')?.focus();}
}
async function sendMessage(text){
 if(!state.activeCall||state.activeCall.status!=='active'||state.busy||state.recording||!text.trim())return;
 stopAudio();state.busy=true;render();
 try{
  const result=await post(`/calls/${state.activeCall.id}/turn`,{text,request_id:requestId()});
  state.activeCall=result.call;state.actions.push(...result.actions);state.lastElapsed=result.elapsed_ms;await refresh();
 }catch(e){toast(e.message,true);}finally{state.busy=false;render();$('#message-input')?.focus();}
}
async function endSession(){
 stopRecording(true);stopAudio();
 if(!state.activeCall||state.busy)return;
 try{state.activeCall=await post(`/calls/${state.activeCall.id}/end`);await refresh();render();toast('Session ended. Your transcript is saved locally.');}catch(e){toast(e.message,true);}
}
async function openDetail(id){
 try{const call=await api('/calls/'+id);detailId=id;$('#detail-title').textContent=call.label;$('#detail-body').innerHTML=`<div class="record-meta"><span class="pill neutral">${esc(call.status.toUpperCase())}</span><span>${dateOf(call.started_at)} &middot; ${timeOf(call.started_at)} IST</span><span>${esc(call.source)}</span></div><div class="hint">${esc(call.outcome)}. ${call.sample?'Synthetic example record.':'Test record in the local database.'}</div><div class="messages">${messageList(call)}</div>`;$('#resume-call').hidden=call.status!=='active'||call.source==='device-lab';$('#end-record').hidden=call.status!=='active'||call.source==='device-lab';$('#detail-dialog').showModal();}catch(e){toast(e.message,true);}
}
function confirmAction(title,message,callback){$('#confirm-title').textContent=title;$('#confirm-text').textContent=message;confirmFn=callback;$('#confirm-dialog').showModal();}
function download(name,data){const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function newDevice(){$('#device-form').hidden=false;$('#device-secret').hidden=true;$('#device-secret').textContent='';$('#device-form').reset();$('#device-dialog').showModal();}
async function createDevice(){
 const button=$('#device-form button[type="submit"]');button.disabled=true;
 try{const device=await post('/devices',{name:$('#device-name').value,kind:$('#device-kind').value});$('#device-form').hidden=true;$('#device-secret').hidden=false;$('#device-secret').innerHTML=`<div class="device-secret"><span class="pill success">IDENTITY CREATED</span><h3>Keep this token safe.</h3><br><p class="small muted">Shown once. It will not be returned by the device-list API.</p><br><label>Device ID</label><pre class="code">${esc(device.id)}</pre><label>Device token</label><pre class="code" id="one-time-token">${esc(device.token)}</pre><button class="button secondary" data-action="copy-token">Copy token</button><div class="hint">${esc(device.warning)}</div></div>`;await refresh();if(state.view==='devices')render();}catch(e){toast(e.message,true);}finally{button.disabled=false;}
}
async function runEcho(){
 if(state.echo?.running)return;
 state.echo={running:true,frames:0,log:['> Provisioning a temporary simulator...'],report:null};render();
 let socket, device, cid, token, sentTime, expected, latencies=[], failures=0;
 const log=line=>{state.echo.log.push(line);if($('#echo-log'))$('#echo-log').innerHTML=state.echo.log.slice(-8).map(l=>`<p>${esc(l)}</p>`).join('');};
 try{
  device=await post('/devices',{name:'Browser loopback '+new Date().toLocaleTimeString(),kind:'simulator'});token=device.token;
  log('> Connecting with callbox.v1 credentials.');
  await new Promise((resolve,reject)=>{
   let finished=false,timer;
   socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/device`);
   const send=p=>socket.send(JSON.stringify(p));
   function finish(error){if(finished)return;finished=true;clearTimeout(timer);socket.close();error?reject(error):resolve();}
   function frame(){
    const bytes=new Uint8Array(640),view=new DataView(bytes.buffer);
    for(let n=0;n<320;n++)view.setInt16(n*2,Math.round(3000*Math.sin(2*Math.PI*440*(n+state.echo.frames*320)/16000)),true);
    expected=btoa(String.fromCharCode(...bytes));sentTime=performance.now();
    send({type:'audio',call_id:cid,seq:state.echo.frames,epoch:0,sample_rate:16000,pcm16:expected});
   }
   timer=setTimeout(()=>finish(new Error('Gateway test timed out.')),15000);
   socket.onopen=()=>send({type:'hello',protocol:'callbox.v1',device_id:device.id,token});
   socket.onerror=()=>finish(new Error('WebSocket connection failed.'));
   socket.onclose=()=>{if(!finished)finish(new Error('Gateway disconnected before test completion.'));};
   socket.onmessage=event=>{
    try{
     const m=JSON.parse(event.data);
     if(m.type==='error')return finish(new Error(m.message));
     if(m.type==='ready'){log('> Authenticated. PCM16 / mono / 16000 Hz.');send({type:'call.start',mode:'echo',consent:true});}
     else if(m.type==='call.started'){cid=m.call_id;log('> Sending 50 frames (1 second of test audio).');frame();}
     else if(m.type==='audio.output'){
      if(m.pcm16!==expected||m.seq!==state.echo.frames)failures++;
      latencies.push(performance.now()-sentTime);state.echo.frames++;const p=$('#echo-progress');if(p)p.value=state.echo.frames;
      if(state.echo.frames<50)setTimeout(frame,20);else send({type:'interrupt',call_id:cid});
     }else if(m.type==='playback.clear'){log('> Interrupt acknowledged. Playback epoch: '+m.epoch);send({type:'call.end',call_id:cid});}
     else if(m.type==='call.ended')finish();
    }catch(error){finish(error);}
   };
  });
  latencies.sort((a,b)=>a-b);
  const report={kind:'software-loopback',timestamp:new Date().toISOString(),frames:50,exact_matches:50-failures,p50_ms:+latencies[24].toFixed(2),p95_ms:+latencies[47].toFixed(2),hardware_verified:false,phone_call_placed:false,interrupt_acknowledged:true,device_id:device.id};
  state.echo.report=report;
  log(`> ${report.exact_matches}/50 frames identical. p50 ${report.p50_ms} ms / p95 ${report.p95_ms} ms.`);
  log('> Network path proved. Bluetooth is NOT tested.');
  if(failures)throw new Error('Some returned audio bytes did not match.');
  toast('50 audio frames verified through the server.');
 }catch(e){log('> ERROR: '+e.message);toast(e.message,true);}
 finally{
  token=null;if(socket&&socket.readyState<2)socket.close();
  if(device)try{await post(`/devices/${device.id}/revoke`,{confirm:true});log('> Temporary device credentials revoked.');}catch{}
  state.echo.running=false;await refresh().catch(()=>{});if(state.view==='devices')render();
 }
}
function stopAudio(){if('speechSynthesis' in window)window.speechSynthesis.cancel();if(audioPlayer){audioPlayer.pause();audioPlayer=null;}}
function stopRecording(discard=false){
 clearTimeout(recordTimer);
 if(state.recorder&&state.recorder.state!=='inactive'){state.recorder.discard=discard;state.recorder.stop();}
 state.mediaStream?.getTracks().forEach(t=>t.stop());state.mediaStream=null;state.recording=false;
}
async function recordTurn(){
 if(state.recording){stopRecording();return;}
 if(!state.activeCall||state.activeCall.provider!=='openai'||state.busy)return;
 if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){toast('Microphone recording needs a supported browser on localhost or HTTPS.',true);return;}
 try{
  stopAudio();const stream=await navigator.mediaDevices.getUserMedia({audio:true});state.mediaStream=stream;
  const mime=['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus'].find(x=>MediaRecorder.isTypeSupported(x));
  const recorder=new MediaRecorder(stream,mime?{mimeType:mime}:{});state.recorder=recorder;state.recording=true;
  const chunks=[],callId=state.activeCall.id;
  recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};
  recorder.onstop=async()=>{
   stream.getTracks().forEach(t=>t.stop());state.recording=false;state.mediaStream=null;
   if(recorder.discard||!chunks.length){if(state.view==='playground')render();return;}
   const blob=new Blob(chunks,{type:recorder.mimeType});state.busy=true;if(state.view==='playground')render();
   try{
    const result=await api(`/calls/${callId}/audio?request_id=${requestId()}`,{method:'POST',headers:{'Content-Type':blob.type},body:blob});
    if(state.activeCall?.id===callId){state.activeCall=result.call;state.actions.push(...result.actions);state.lastElapsed=result.elapsed_ms;}
    if(result.audio_base64){audioPlayer=new Audio('data:'+result.audio_mime+';base64,'+result.audio_base64);await audioPlayer.play().catch(()=>toast('Reply saved. Your browser blocked automatic audio playback.'));}
    if(result.speech_error)toast(result.speech_error,true);
    await refresh();
   }catch(e){toast(e.message,true);}finally{state.busy=false;if(state.view==='playground')render();}
  };
  recorder.start();recordTimer=setTimeout(()=>stopRecording(),20000);render();toast('Recording a test turn. Stops after 20 seconds.');
 }catch(e){state.recording=false;toast('Microphone unavailable: '+e.message,true);}
}
async function saveSettings(form){
 const data=Object.fromEntries(new FormData(form));for(const key of ['fee','open_hour','close_hour','slot_minutes'])data[key]=Number(data[key]);data.timezone='Asia/Kolkata';
 const btn=form.querySelector('[type="submit"]');btn.disabled=true;
 try{state.user.settings=await api('/settings',{method:'PUT',body:data});$('#sidebar-business').textContent=data.name;$('.workspace-avatar').textContent=data.name[0].toUpperCase();toast('Workspace settings saved.');await refresh();}
 catch(e){$('#settings-error').textContent=e.message;toast(e.message,true);}finally{btn.disabled=false;}
}
async function actions(event){
 const el=event.target.closest('[data-action]');if(!el||el.disabled)return;
 const action=el.dataset.action;
 if(action==='go')navigate(el.dataset.view);
 else if(action==='message')await sendMessage(el.dataset.text);
 else if(action==='detail')await openDetail(el.dataset.id);
 else if(action==='end-call')await endSession();
 else if(action==='new-device')newDevice();
 else if(action==='pipeline'){state.pipeline=el.dataset.pipeline;render();}
 else if(action==='copy-token'){try{await navigator.clipboard.writeText($('#one-time-token').textContent);toast('Token copied. Keep it private.');}catch{toast('Clipboard is unavailable. Select and copy the token manually.',true);}}
 else if(action==='echo')await runEcho();
 else if(action==='export-echo')download('callbox-loopback-result.json',state.echo.report);
 else if(action==='record')await recordTurn();
 else if(action==='read-aloud'){
  const text=[...(state.activeCall?.messages||[])].reverse().find(m=>m.role==='assistant')?.text;
  if(text){stopAudio();const u=new SpeechSynthesisUtterance(text);u.lang=state.activeCall.language==='hinglish'?'hi-IN':'en-IN';u.rate=.96;speechSynthesis.speak(u);toast('Using your browser voice service, not the phone network.');}
 }
 else if(action==='cancel-appointment')confirmAction('Cancel this local appointment?','This cancels the appointment only in the CallBox test calendar. No patient message or external-calendar update will be sent.',async()=>{await post(`/appointments/${el.dataset.id}/cancel`,{confirm:true});await refresh();render();toast('Local appointment cancelled.');});
 else if(action==='revoke-device')confirmAction('Revoke this gateway?','The device token will stop working and any active gateway connection will be closed.',async()=>{await post(`/devices/${el.dataset.id}/revoke`,{confirm:true});await refresh();render();toast('Gateway credentials revoked.');});
 else if(action==='resolve'){try{await post(`/tasks/${el.dataset.id}/resolve`,{confirm:true});await refresh();render();toast('Staff request marked as resolved.');}catch(e){toast(e.message,true);}}
}
// Delegation survives view updates. All untrusted values are HTML-escaped.
document.addEventListener('click',actions);
document.addEventListener('click',e=>{const b=e.target.closest('[data-close]');if(b)$('#'+b.dataset.close).close();});
document.addEventListener('submit',e=>{
 if(e.target.id==='turn-form'){e.preventDefault();sendMessage($('#message-input').value);}
 else if(e.target.id==='start-form'){e.preventDefault();startSession(e.target);}
 else if(e.target.id==='device-form'){e.preventDefault();createDevice();}
 else if(e.target.id==='settings-form'){e.preventDefault();saveSettings(e.target);}
});
document.addEventListener('change',e=>{
 if(e.target.id==='call-filter'){state.callFilter=e.target.value;renderCallsTable();}
 else if(e.target.id==='availability-date'){state.date=e.target.value;fetchAvailability();}
});
window.addEventListener('hashchange',()=>{
 const view=location.hash.slice(1);if(!nav.some(([id])=>id===view))return;
 stopRecording(true);stopAudio();state.view=view;closeMenu();if(state.user){render();window.scrollTo({top:0,behavior:'instant'});$('#main').focus({preventScroll:true});}
});
$('#menu-button').addEventListener('click',()=>{const open=!$('#sidebar').classList.contains('open');$('#sidebar').classList.toggle('open',open);$('#nav-backdrop').hidden=!open;$('#menu-button').setAttribute('aria-expanded',String(open));});
$('#nav-backdrop').addEventListener('click',closeMenu);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('#sidebar').classList.contains('open')){closeMenu();$('#menu-button').focus();}});
$('#view-pipeline').addEventListener('click',()=>navigate('pipeline'));
$('#workspace-switch').addEventListener('click',()=>navigate('settings'));
$('#logout').addEventListener('click',async()=>{try{stopRecording(true);await post('/auth/logout');showLogin();}catch(e){toast(e.message,true);}});
$('#confirm-action').addEventListener('click',async()=>{const b=$('#confirm-action');b.disabled=true;try{await confirmFn?.();$('#confirm-dialog').close();}catch(e){toast(e.message,true);}finally{b.disabled=false;}});
$('#device-dialog').addEventListener('close',()=>{$('#device-secret').textContent='';$('#device-secret').hidden=true;});
$('#confirm-dialog').addEventListener('close',()=>{confirmFn=null;});
$('#resume-call').addEventListener('click',async()=>{
 try{stopRecording(true);stopAudio();state.activeCall=await api('/calls/'+detailId);state.actions=[];state.lastElapsed=null;$('#detail-dialog').close();navigate('playground');}catch(e){toast(e.message,true);}
});
$('#end-record').addEventListener('click',async()=>{
 const id=detailId;$('#detail-dialog').close();
 confirmAction('End this test session?','The saved transcript and committed appointments will be kept.',async()=>{const result=await post('/calls/'+id+'/end');if(state.activeCall?.id===id)state.activeCall=result;await refresh();render();toast('Stored session ended.');});
});
$('#export-call').addEventListener('click',async()=>{try{download(detailId+'.json',await api(`/calls/${detailId}/export`));}catch(e){toast(e.message,true);}});
$('#demo-login').addEventListener('click',async()=>{const b=$('#demo-login');b.disabled=true;try{await post('/auth/demo');await loadWorkspace();}catch(e){$('#login-error').textContent=e.message;}finally{b.disabled=false;}});
$('#login-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{await post('/auth/login',{token:$('#admin-token').value});$('#admin-token').value='';await loadWorkspace();}catch(error){$('#login-error').textContent=error.message;}finally{b.disabled=false;}});
window.addEventListener('beforeunload',()=>{stopRecording(true);stopAudio();});
async function init(){
 try{const b=await api('/bootstrap');$('#demo-login').hidden=!b.demo_login;await loadWorkspace();}
 catch(e){if(!state.user){$('#login-screen').hidden=false;$('#shell').hidden=true;}else toast(e.message,true);}
}
init();
