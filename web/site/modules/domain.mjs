/** Pure, dependency-free domain logic shared by the website and mock API. */
export const DEFAULT_INPUTS = Object.freeze({ calls: 30, minutes: 3, days: 26, aiRate: 3, telecomRate: 0.8, numberRental: 300, simPlan: 399, hardwareCost: 2500, amortization: 12, hosting: 500 });
export const INPUT_RULES = Object.freeze({calls:[1,10000,true],minutes:[0.5,30,false],days:[1,31,true],aiRate:[0,100,false],telecomRate:[0,100,false],numberRental:[0,100000,false],simPlan:[0,100000,false],hardwareCost:[0,100000,false],amortization:[1,60,true],hosting:[0,100000,false]});
/** @param {Record<string, unknown>} raw */
export function calculateEstimate(raw) {
  const inputs = {};
  for (const [key, [min, max, integer]] of Object.entries(INPUT_RULES)) {
    const original = raw[key];
    const n = typeof original === 'number' ? original : (typeof original === 'string' && original.trim() !== '' ? Number(original) : NaN);
    if (!Number.isFinite(n) || n < min || n > max || (integer && !Number.isInteger(n))) throw new RangeError(`Invalid ${key}: use ${integer ? 'a whole number' : 'a number'} between ${min} and ${max}.`);
    inputs[key] = n;
  }
  const monthlyMinutes = inputs.calls * inputs.minutes * inputs.days;
  const ai = monthlyMinutes * inputs.aiRate;
  const hardwareAmortization = inputs.hardwareCost / inputs.amortization;
  const hardware = ai + inputs.hosting + inputs.simPlan + hardwareAmortization;
  const cloudTelephony = monthlyMinutes * inputs.telecomRate;
  const cloud = ai + inputs.hosting + inputs.numberRental + cloudTelephony;
  return { inputs, monthlyMinutes, ai, hardwareAmortization, cloudTelephony, hardware, cloud, difference: cloud - hardware, illustrative: true, currency: 'INR' };
}

export const SCENARIOS = Object.freeze({
  clinic: {title:'Willow Clinic',result:'Sample appointment request recorded: Tuesday, 5:30 PM. No real booking was created.',
    en:[['agent',"Hello, Willow Clinic. I'm the clinic's AI assistant. How can I help?"],['caller',"I'd like to book an appointment for Tuesday evening."],['agent',"In this example, 5:30 and 6:15 are available. Which works for you?"],['caller',"5:30, please. The name is Alex."],['agent',"Tuesday at 5:30 for Alex. May I record that appointment request?"],['caller',"Yes, that's right. Thank you!"]],
    hi:[['agent','Namaste, Willow Clinic. Main clinic ka AI assistant hoon. Kaise madad karun?'],['caller','Tuesday shaam ka appointment chahiye.'],['agent','Is demo mein 5:30 aur 6:15 ke slots hain. Kaunsa theek rahega?'],['caller','5:30 kar dijiye. Naam Alex hai.'],['agent','Alex ke liye Tuesday 5:30. Kya main appointment request note kar loon?'],['caller','Ji, bilkul. Thank you!']]},
  salon: {title:'Moss & Mirror',result:'Sample haircut request recorded: Friday afternoon. The real salon has not been contacted.',
    en:[['agent',"Hello, Moss and Mirror. I'm the salon's AI assistant. What can I help with?"],['caller',"Could I get a haircut on Friday?"],['agent',"Of course. Do you prefer morning or afternoon?"],['caller',"Afternoon, ideally after three."],['agent',"I'll record a sample request for after three on Friday. A real booking would need the salon's calendar."],['caller',"Perfect, that works for me."]],
    hi:[['agent','Namaste, Moss and Mirror. Main salon ka AI assistant hoon. Kaise madad karun?'],['caller','Friday ko haircut karwana hai.'],['agent','Ji. Morning chahiye ya afternoon?'],['caller','Teen baje ke baad ho toh achha rahega.'],['agent','Friday teen baje ke baad ki sample request note kar raha hoon. Asli booking ke liye salon calendar chahiye.'],['caller','Theek hai, thank you.']]},
  repair: {title:'Everyday Repairs',result:'Sample service callback requested. No technician or customer system was contacted.',
    en:[['agent',"Hello, Everyday Repairs. I'm the service desk's AI assistant."],['caller',"My washing machine stopped working. Can someone call me back?"],['agent',"I can help record a callback request. Please don't share private account details in this demo."],['caller',"A callback after four would be useful."],['agent',"Noted in this sample: washing machine repair, callback after four. A team member should handle the technical advice."],['caller',"Great, thank you."]],
    hi:[['agent','Namaste, Everyday Repairs. Main service desk ka AI assistant hoon.'],['caller','Washing machine band ho gayi. Koi mujhe call back kar sakta hai?'],['agent','Ji, callback request note kar sakta hoon. Is demo mein personal details share na karein.'],['caller','Chaar baje ke baad call aa jaye toh achha hoga.'],['agent','Sample request note kar li: washing machine, chaar baje ke baad callback. Technical advice team degi.'],['caller','Theek hai, thank you.']]}
});

export function createCallState(business='clinic', language='en') {
  if (!Object.hasOwn(SCENARIOS,business) || !['en','hi'].includes(language)) throw new RangeError('Unknown sample conversation.');
  return { business, language, status:'idle', cursor:0, messages:[] };
}
/** Deterministic simulator; it never places a call or executes a booking. */
export function transitionCall(state, action) {
  const script=SCENARIOS[state.business][state.language];
  switch(action) {
    case 'play': return ['idle','paused'].includes(state.status) ? {...state,status:'playing'} : state;
    case 'pause': return state.status === 'playing' ? {...state,status:'paused'} : state;
    case 'next': {
      if (!['playing','paused'].includes(state.status) || state.cursor >= script.length) return state;
      const cursor=state.cursor+1;
      return {...state,cursor,messages:[...state.messages,{speaker:script[state.cursor][0],text:script[state.cursor][1]}],status:cursor===script.length?'complete':state.status};
    }
    case 'handoff': return ['playing','paused','complete'].includes(state.status) ? {...state,status:'handed-off',messages:[...state.messages,{speaker:'system',text:'Human help requested in the demo. A real Bluetooth prototype needs local handset takeover or a callback workflow; no transfer has been placed.'}]} : state;
    case 'reset': return createCallState(state.business,state.language);
    default: throw new RangeError('Unknown simulator action.');
  }
}

export const PIPELINES = Object.freeze({
 bluetooth:{label:'BLUETOOTH / PROTOTYPE TRACK',summary:'No firmware or live telephony is connected in this demo.',nodes:[
  {icon:'phone',name:'Your phone',sub:'Existing SIM',kicker:'01 / THE FAMILIAR PART',title:'Your existing phone',description:'Your SIM and carrier still handle the telephone connection. The proposed gateway pairs as a Bluetooth hands-free device.',caveat:'Compatibility, audio routing and recovery need testing on each target phone. One phone is not a multi-line call centre.'},
  {icon:'chip',name:'CallBox',sub:'Bluetooth HFP',kicker:'02 / THE EXPERIMENT',title:'A headset with a different job',description:'An original ESP32 acts as a hands-free client. The prototype would receive call audio and feed the agent voice back through the phone.',caveat:'HFP is Bluetooth Classic, not generic BLE or music streaming. Audio framing, codec support, buffering and handset behaviour are still bench-test work.'},
  {icon:'cloud',name:'Voice gateway',sub:'Secure audio transport',kicker:'03 / THE BRIDGE',title:'Small packets. Both directions.',description:'A server adapter would receive bounded audio frames, manage turn-taking and send generated speech back to the device.',caveat:'Authenticate each device, use encrypted transport, cap buffers and test disconnects. The web demo is not this media server.'},
  {icon:'calendar',name:'Agent + tools',sub:'A useful next step',kicker:'04 / THE USEFUL PART',title:'Conversation meets a real workflow',description:'Choose a speech-to-text, language-model and text-to-speech pipeline, or a realtime speech model. Expose narrowly scoped business tools behind it.',caveat:'Only a successful backend tool result may confirm an appointment. Check authorization, duplicate requests, timezones and calendar conflicts.'},
  {icon:'shield',name:'Human fallback',sub:'Takeover / callback',kicker:'05 / PEOPLE STAY IN CONTROL',title:'Keep a person in the loop',description:'The initial phone-based build should offer local handset takeover or a clearly queued callback request, not promise a remote call transfer.',caveat:'Emergency, clinical or uncertain requests require approved escalation. Automatic transfers depend on a separately tested carrier or PBX capability.'}
 ]},
 cloud:{label:'CLOUD TELEPHONY / CARRIER-BACKED OPTION',summary:'Number availability, forwarding and caller ID depend on provider and country.',nodes:[
  {icon:'phone',name:'Business number',sub:'Provider / forwarding',kicker:'01 / THE PUBLIC NUMBER',title:'Start at the telephone network',description:'A supported provider number or a forwarding arrangement routes an incoming call into a programmable voice platform.',caveat:'Keeping a particular existing number is not universal. Confirm country, number type, ownership, routing and caller-ID rules with the provider.'},
  {icon:'cloud',name:'Telephony API',sub:'Call control + audio',kicker:'02 / THE CARRIER BRIDGE',title:'Let the provider carry the call',description:'The provider handles its supported telephone legs and exposes call-control events plus a supported bidirectional media interface.',caveat:'Verify the actual audio-streaming product, provisioning requirements and charges. An inbound call webhook alone is not a live audio stream.'},
  {icon:'chip',name:'Voice adapter',sub:'Provider-specific',kicker:'03 / A COMMON CONTRACT',title:'Translate, do not tightly couple',description:'A provider-specific adapter normalizes call IDs, media formats and lifecycle events into the same internal contract as a hardware gateway.',caveat:'Validate webhook signatures and source authorization. Codecs, sample rates, framing, interruption controls and billing differ by provider.'},
  {icon:'calendar',name:'Agent + tools',sub:'Shared business logic',kicker:'04 / THE SAME BRAIN',title:'Reuse the useful layer',description:'The conversation and permission-controlled business tools can be shared with the other routes, even though the transport differs.',caveat:'The frontend must never hold carrier credentials. Confirm actions with server results and prevent duplicate tool execution.'},
  {icon:'person',name:'Human bridge',sub:'When supported',kicker:'05 / A REAL HANDOFF PATH',title:'Route to the right person',description:'A configured provider can bridge or transfer a call to a human endpoint, subject to its supported features and account setup.',caveat:'A new outbound leg may cost extra. Avoid forwarding loops and define a fallback if the human does not answer.'}
 ]},
 sip:{label:'SIP / PBX / ENTERPRISE BUILD TRACK',summary:'A licensed carrier connection is still required for public telephone access.',nodes:[
  {icon:'phone',name:'Carrier trunk',sub:'Public network access',kicker:'01 / THE PART YOU STILL BUY',title:'Keep a licensed carrier underneath',description:'A suitable enterprise carrier delivers telephone connectivity into an approved SIP/PBX setup. Self-hosting call software does not replace the carrier.',caveat:'Numbers, call capacity, interconnect, location and commercial terms must be confirmed. This is not a way to bypass carrier restrictions.'},
  {icon:'shield',name:'SBC / PBX',sub:'Call routing',kicker:'02 / YOUR CALL CONTROL',title:'Own more of the routing',description:'A PBX or media server such as Asterisk can route calls between a trunk, extensions and a media application.',caveat:'Plan authentication, toll-fraud prevention, NAT/media handling, capacity, monitoring, failover and operational ownership.'},
  {icon:'chip',name:'Media adapter',sub:'RTP / supported WS',kicker:'03 / THE AUDIO INTERFACE',title:'Make the media contract explicit',description:'Use a media interface supported by the deployed PBX version. Adapt its audio and lifecycle events into the internal gateway protocol.',caveat:'SIP negotiates calls; it is not the audio itself. RTP or a supported WebSocket media channel carries audio. Version and codec compatibility matter.'},
  {icon:'calendar',name:'Agent workers',sub:'Multiple sessions',kicker:'04 / CONCURRENCY BY DESIGN',title:'An agent session for each call',description:'Allocate independent state, tools and audio buffers per call. Scale only after measured load tests and a clear capacity plan.',caveat:'Set admission limits, provider quotas, timeouts and costs. Never share conversation or patient state across tenants.'},
  {icon:'person',name:'Human queue',sub:'PBX extensions',kicker:'05 / THE TEAM IS STILL THERE',title:'Bring people into the same system',description:'Route suitable calls to staffed extensions or queues with context and a predictable no-answer policy.',caveat:'Warm transfer, recording and supervisor features are separate workflows to implement and test, not included in this website.'}
 ]}
});

export const PHASES = [
 ['Concept delivered.','The website, simulator and planning tools work locally. No telephone hardware or patient systems are connected.'],
 ['Prove the audio before adding AI.','Test an original ESP32 and a target phone: pairing, answer/hang-up, two-way audio, interruption, reconnection and long-call stability. Measure first; set pass criteria before a pilot.'],
 ['Make actions verifiable.','Implement authenticated transport adapters, one voice stack and a sandbox calendar. Add idempotent booking, privacy controls and a reliable human escalation path.'],
 ['Pilot with a person available.','Use consented test calls, monitor failures and retain an immediate rollback. Broader rollout needs evidence on reliability, costs, carrier terms and appropriate safety review.']
];

export function createBuildBrief({business,connection,language,calls,voicePipeline='composable'}, estimate) {
  if (!['composable','realtime'].includes(voicePipeline) || !['clinic','salon','repair','office'].includes(business) || !Object.hasOwn(PIPELINES,connection) || !['hinglish','english'].includes(language)) throw new RangeError('Select a supported setup.');
  const n=Number(calls);
  if(!Number.isInteger(n) || n<1 || n>10000) throw new RangeError('Calls per day must be between 1 and 10,000.');
  return {schema:'callbox.build-brief/v1',createdAt:new Date().toISOString(),status:'planning-only',business,connection,voicePipeline,language,callsPerDay:n,estimate:calculateEstimate({...estimate.inputs,calls:n}),constraints:['No live calling is enabled by this brief.','Validate phone and carrier compatibility before purchasing or deployment.','Keep a tested human fallback.','Use verified backend results for every business action.','No autonomous diagnosis, prescribing or medical clearance.'],nextSteps:connection==='bluetooth'?['Validate original ESP32 HFP pairing and two-way audio.','Measure stability and disconnect recovery.','Connect one sandbox voice model and one business tool.']:connection==='cloud'?['Confirm provider number, streaming access, rates and regional eligibility.','Implement signed webhook verification and a media adapter.','Test a sandbox workflow and human call bridge.']:['Confirm carrier SIP terms and deployment requirements.','Test PBX media interface and secure trunk configuration.','Load-test isolated call sessions and fallback queues.']};
}
