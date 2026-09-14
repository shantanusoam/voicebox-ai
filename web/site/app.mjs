import {DEFAULT_INPUTS, calculateEstimate, SCENARIOS, createCallState, transitionCall, PIPELINES, PHASES, createBuildBrief} from './modules/domain.mjs';

const $=(selector,scope=document)=>scope.querySelector(selector);
const $$=(selector,scope=document)=>Array.from(scope.querySelectorAll(selector));
const svgIcon=(name)=>{const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('class','icon');svg.setAttribute('aria-hidden','true');const use=document.createElementNS(svg.namespaceURI,'use');use.setAttribute('href',`#i-${name}`);svg.append(use);return svg;};
const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)');
const money=n=>new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',maximumFractionDigits:0}).format(n);
let toastTimer;
function toast(text){clearTimeout(toastTimer);const el=$('#toast');el.textContent=text;el.hidden=false;toastTimer=setTimeout(()=>{el.hidden=true;},5000);}
function downloadJSON(filename,payload){const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download=filename;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),2000);}

// Navigation remains usable without any third-party library.
const menuButton=$('.menu-toggle'),mobileNav=$('#mobile-nav');
function setMenu(open){mobileNav.hidden=!open;menuButton.setAttribute('aria-expanded',String(open));menuButton.setAttribute('aria-label',open?'Close navigation':'Open navigation');}
menuButton.addEventListener('click',()=>setMenu(mobileNav.hidden));
$$('a',mobileNav).forEach(a=>a.addEventListener('click',()=>setMenu(false)));
window.addEventListener('resize',()=>{if(innerWidth>760)setMenu(false);});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!mobileNav.hidden){setMenu(false);menuButton.focus();}});

// Interactive scripted call. No microphone, speech recognition, booking or real phone API.
let call=createCallState();
let playTimer=null,speechFallback=null,speechGeneration=0,sound=false;
function stopPlayback(){clearTimeout(playTimer);clearTimeout(speechFallback);playTimer=null;speechFallback=null;speechGeneration++;if('speechSynthesis' in window)window.speechSynthesis.cancel();}
function scheduleNext(delay=2300){clearTimeout(playTimer);if(call.status==='playing')playTimer=setTimeout(advanceCall,delay);}
function speak(text){
 if(!sound || !('speechSynthesis' in window)){scheduleNext();return;}
 const token=++speechGeneration;
 const utterance=new SpeechSynthesisUtterance(text);
 utterance.lang=call.language==='hi'?'hi-IN':'en-IN';
 const voices=window.speechSynthesis.getVoices();
 const matching=voices.find(v=>v.lang.toLowerCase()===utterance.lang.toLowerCase()) || voices.find(v=>v.lang.startsWith(call.language==='hi'?'hi':'en'));
 if(matching)utterance.voice=matching;
 utterance.rate=1;
 let finished=false;
 const done=()=>{if(finished || token!==speechGeneration)return;finished=true;clearTimeout(speechFallback);scheduleNext(700);};
 utterance.onend=done;
 utterance.onerror=()=>{if(token===speechGeneration){toast('Voice playback is unavailable here. The transcript still works.');done();}};
 // Some browsers expose speechSynthesis but never start audio. Do not stall the demo.
 speechFallback=setTimeout(()=>{if(token===speechGeneration){window.speechSynthesis.cancel();done();}},18000);
 window.speechSynthesis.speak(utterance);
}
function renderCall(){
 $('#demo-business-title').textContent=SCENARIOS[call.business].title;
 $('#demo-turn-count').textContent=`${String(call.cursor).padStart(2,'0')} / ${String(SCENARIOS[call.business][call.language].length).padStart(2,'0')}`;
 const statuses={idle:'Ready when you are',playing:'Sample call in progress',paused:'Paused - take your time',complete:'Sample conversation complete','handed-off':'Demo human-help request recorded'};
 const status=$('#demo-status');status.replaceChildren(Object.assign(document.createElement('span'),{className:'status-dot'}),document.createTextNode(statuses[call.status]));
 const transcript=$('#transcript');
 if(call.messages.length){
   const rendered=transcript.querySelectorAll('.transcript-row').length;
   if(transcript.querySelector('.transcript-empty')||rendered>call.messages.length)transcript.replaceChildren();
   const start=transcript.querySelectorAll('.transcript-row').length;
   for(const line of call.messages.slice(start)){const row=document.createElement('div');row.className=`transcript-row ${line.speaker}`;const speaker=document.createElement('span');speaker.className='speaker';speaker.textContent=line.speaker==='agent'?'CallBox':line.speaker==='caller'?'Sample caller':'Demo system';const bubble=document.createElement('div');bubble.className='bubble';bubble.textContent=line.text;row.append(speaker,bubble);transcript.append(row);}
   transcript.scrollTo({top:transcript.scrollHeight,behavior:reducedMotion.matches?'instant':'smooth'});
 }else{
   transcript.replaceChildren();const empty=document.createElement('div');empty.className='transcript-empty';const wave=document.createElement('div');wave.className='waiting-wave';wave.setAttribute('aria-hidden','true');for(let i=0;i<9;i++)wave.append(document.createElement('i'));const title=document.createElement('b');title.textContent='A better hello starts here.';const p=document.createElement('p');p.textContent='Press play to meet your AI front desk.';empty.append(wave,title,p);transcript.append(empty);
 }
 const play=$('#demo-play');const playing=call.status==='playing';$('use',play).setAttribute('href',playing?'#i-pause':'#i-play');$('span',play).textContent=playing?'Pause conversation':call.status==='paused'?'Resume call':['complete','handed-off'].includes(call.status)?'Play again':'Play sample call';
 $('#demo-next').disabled=!['playing','paused'].includes(call.status);
 $('#demo-handoff').disabled=!['playing','paused','complete'].includes(call.status);
 const result=$('#demo-result'),p=document.createElement('p');p.textContent=call.status==='complete'?SCENARIOS[call.business].result:call.status==='handed-off'?'A human-help request was recorded in the demo. No person has been called.':'Every conversation should end with a clear next step.';result.replaceChildren(svgIcon(call.status==='handed-off'?'person':'calendar'),p);
}
function advanceCall(){clearTimeout(playTimer);const previous=call.cursor;call=transitionCall(call,'next');renderCall();if(call.cursor>previous)speak(call.messages[call.messages.length-1].text);}
function resetCall(business=call.business,language=call.language){stopPlayback();call=createCallState(business,language);renderCall();}
$('#demo-play').addEventListener('click',()=>{
 if(call.status==='playing'){stopPlayback();call=transitionCall(call,'pause');renderCall();return;}
 if(['complete','handed-off'].includes(call.status))resetCall();
 call=transitionCall(call,'play');renderCall();if(call.cursor===0)advanceCall();else scheduleNext(300);
});
$('#demo-next').addEventListener('click',()=>{stopPlayback();advanceCall();});
$('#demo-reset').addEventListener('click',()=>resetCall());
$('#demo-handoff').addEventListener('click',()=>{stopPlayback();call=transitionCall(call,'handoff');renderCall();});
$$('[data-business]').forEach(button=>button.addEventListener('click',()=>{resetCall(button.dataset.business,$('#demo-language').value);$$('[data-business]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));}));
$('#demo-language').addEventListener('change',e=>resetCall(call.business,e.target.value));
$('#sound-toggle').addEventListener('click',()=>{if(!('speechSynthesis' in window)){toast('This browser has no speech playback. Use the transcript demo.');return;}sound=!sound;$('#sound-toggle').setAttribute('aria-pressed',String(sound));$('#sound-toggle span').textContent=sound?'Sound on':'Sound off';if(!sound){stopPlayback();scheduleNext(1000);}else toast('Optional browser voice enabled. No microphone access.');});
window.addEventListener('pagehide',stopPlayback);
document.addEventListener('visibilitychange',()=>{if(document.hidden&&call.status==='playing'){stopPlayback();call=transitionCall(call,'pause');renderCall();}});

// Clickable architecture explorer and cancellable two-way signal tracing.
let pipeline='bluetooth',voicePipeline='composable',selectedNode=0,traceTimer=null;
const voiceDescriptions={composable:'A composable pipeline transcribes speech, runs a text agent, then generates voice. More component control; more interfaces and latency budgets to manage. No model is connected in this demo.',realtime:'A realtime speech model handles audio input and output while calling your backend tools. It can reduce orchestration steps, but quality, cost and interruption behaviour still need measurement. No model is connected in this demo.'};
$$('[data-voice]').forEach(button=>button.addEventListener('click',()=>{voicePipeline=button.dataset.voice;$$('[data-voice]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));$('#voice-description').textContent=voiceDescriptions[voicePipeline];}));
function stopTrace(){clearTimeout(traceTimer);$$('.pipeline-node').forEach(node=>node.classList.remove('tracing'));$('#trace-call').disabled=false;}
function inspectNode(index){selectedNode=index;const node=PIPELINES[pipeline].nodes[index];$('#node-kicker').textContent=node.kicker;$('#node-title').textContent=node.title;$('#node-description').textContent=node.description;$('#node-caveat').textContent=node.caveat;$$('.pipeline-node').forEach((button,i)=>button.setAttribute('aria-pressed',String(i===index)));}
function renderPipeline(){
 stopTrace();const data=PIPELINES[pipeline];const label=$('#pipeline-label');label.replaceChildren(Object.assign(document.createElement('span'),{className:'status-dot'}),document.createTextNode(data.label));$('#pipeline-summary').textContent=data.summary;
 $('#pipeline-nodes').replaceChildren();data.nodes.forEach((node,index)=>{const button=document.createElement('button');button.className='pipeline-node';button.setAttribute('aria-pressed',String(index===selectedNode));button.setAttribute('aria-label',`Inspect ${node.name}`);const b=document.createElement('b');b.textContent=node.name;const small=document.createElement('small');small.textContent=node.sub;button.append(svgIcon(node.icon),b,small);button.addEventListener('click',()=>{stopTrace();inspectNode(index);});$('#pipeline-nodes').append(button);});
 inspectNode(0);
}
$$('[data-pipeline]').forEach(button=>button.addEventListener('click',()=>{pipeline=button.dataset.pipeline;selectedNode=0;$$('[data-pipeline]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));renderPipeline();}));
$('#trace-call').addEventListener('click',()=>{stopTrace();$('#trace-call').disabled=true;const path=[0,1,2,3,4,3,2,1,0];let step=0;const tick=()=>{$$('.pipeline-node').forEach(n=>n.classList.remove('tracing'));if(step>=path.length){stopTrace();return;}const index=path[step++];inspectNode(index);$$('.pipeline-node')[index].classList.add('tracing');traceTimer=setTimeout(tick,reducedMotion.matches?350:650);};tick();});

// No market prices are hardcoded: all numbers are explicitly illustrative user-editable assumptions.
const inputMap={calls:'calls-per-day',minutes:'call-length',days:'working-days',aiRate:'ai-rate',telecomRate:'telecom-rate',numberRental:'number-rental',simPlan:'sim-plan',hardwareCost:'hardware-cost',amortization:'amortization',hosting:'hosting-cost'};
let estimate=calculateEstimate(DEFAULT_INPUTS);
function updateEstimate(){
 const raw={};for(const[key,id]of Object.entries(inputMap)){raw[key]=$(`#${id}`).value;$(`#${id}`).removeAttribute('aria-invalid');}
 try{estimate=calculateEstimate(raw);$('#calc-error').hidden=true;$('#export-estimate').disabled=false;$('#calls-output').textContent=String(estimate.inputs.calls);$('#hardware-total').textContent=money(estimate.hardware);$('#cloud-total').textContent=money(estimate.cloud);$('#minutes-total').textContent=`${new Intl.NumberFormat('en-IN',{maximumFractionDigits:1}).format(estimate.monthlyMinutes)} minutes / month`;$('#savings-total').textContent=Math.abs(estimate.difference)<.5?'Same modeled monthly cost':`${money(Math.abs(estimate.difference))} ${estimate.difference>0?'lower with hardware':'higher with hardware'}`;
 }catch(error){estimate=null;$('#calc-error').textContent=error.message;$('#calc-error').hidden=false;$('#export-estimate').disabled=true;$('#hardware-total').textContent='\u2014';$('#cloud-total').textContent='\u2014';$('#savings-total').textContent='Correct the input to compare';const key=Object.keys(inputMap).find(k=>error.message.startsWith(`Invalid ${k}:`));if(key)$(`#${inputMap[key]}`).setAttribute('aria-invalid','true');}
}
Object.values(inputMap).forEach(id=>$(`#${id}`).addEventListener('input',updateEstimate));
$('#export-estimate').addEventListener('click',()=>{downloadJSON('callbox-illustrative-estimate.json',{...estimate,createdAt:new Date().toISOString(),scope:'Illustrative monthly comparison, one cloud telephony leg. Not a vendor quotation.',excludes:['Tax','Forwarding','Additional call legs','Messaging','Engineering','Support','Compliance'],formula:{hardware:'minutes * aiRate + hosting + simPlan + hardwareCost / amortization',cloud:'minutes * (aiRate + telecomRate) + hosting + numberRental'}});toast('Your editable estimate has been exported.');});

$$('[data-phase]').forEach(button=>button.addEventListener('click',()=>{const phase=PHASES[Number(button.dataset.phase)];$$('[data-phase]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));const p=$('#phase-detail p');const strong=document.createElement('strong');strong.textContent=phase[0];p.replaceChildren(strong,document.createTextNode(` ${phase[1]}`));}));

// Native dialogs provide focus trapping and Escape support. No fake lead collection.
const setupDialog=$('#setup-dialog'),conceptDialog=$('#concept-dialog');let lastDialogTrigger=null;
function openDialog(dialog,trigger){lastDialogTrigger=trigger;dialog.showModal();document.body.classList.add('dialog-open');}
function closeDialog(dialog){dialog.close();}
for(const dialog of [setupDialog,conceptDialog]){dialog.addEventListener('close',()=>{document.body.classList.remove('dialog-open');lastDialogTrigger?.focus();});dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeDialog(dialog);}});}
$$('[data-open-setup]').forEach(button=>button.addEventListener('click',()=>{if(!estimate){toast('Correct the cost planner inputs before creating a build brief.');$('#planner').scrollIntoView({behavior:reducedMotion.matches?'instant':'smooth'});return;}setMenu(false);$('#setup-form [name="connection"]').value=pipeline;$('#setup-form [name="calls"]').value=String(estimate.inputs.calls);openDialog(setupDialog,button);}));
$('[data-close-dialog]').addEventListener('click',()=>closeDialog(setupDialog));
$('#view-concept').addEventListener('click',e=>openDialog(conceptDialog,e.currentTarget));
$('[data-close-concept]').addEventListener('click',()=>closeDialog(conceptDialog));
$('#setup-form').addEventListener('submit',e=>{e.preventDefault();const form=e.currentTarget;if(!form.reportValidity())return;try{if(!estimate)throw new Error('Correct the cost planner inputs first.');const data=Object.fromEntries(new FormData(form));const brief=createBuildBrief({...data,voicePipeline},estimate);downloadJSON('callbox-build-brief.json',brief);closeDialog(setupDialog);toast('Build brief created locally. Nothing was submitted.');}catch(error){toast(error.message);}});

renderCall();renderPipeline();updateEstimate();
