/* Full-duplex voice client. Streams mic PCM up and plays model PCM down.
   The provider key lives on the server; this only ever talks to /ws/voice. */
export class RealtimeVoice {
  constructor(handlers = {}) {
    this.on = handlers;
    this.socket = null;
    this.context = null;
    this.node = null;
    this.stream = null;
    this.queue = [];      // scheduled playback sources
    this.playAt = 0;
    this.rate = 24000;
    this.active = false;
  }

  emit(name, detail) { if (this.on[name]) this.on[name](detail); }

  async start() {
    if (this.active) return;
    this.active = true;
    try {
      // Ask the browser for 24 kHz directly; it resamples the mic for us.
      this.context = new AudioContext({ sampleRate: this.rate });
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true,
                 autoGainControl: true },
      });
      await this.context.audioWorklet.addModule('/app/pcm-worklet.js');
    } catch (error) {
      this.active = false;
      this.emit('error', error.name === 'NotAllowedError'
        ? 'Microphone permission was denied. Allow it in the address bar and try again.'
        : 'Could not open the microphone: ' + error.message);
      return this.stop();
    }

    const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/voice';
    this.socket = new WebSocket(url);
    this.socket.onmessage = (event) => this.handle(JSON.parse(event.data));
    this.socket.onerror = () => this.emit('error', 'The voice connection failed.');
    this.socket.onclose = () => { if (this.active) this.stop(); };
    this.socket.onopen = () => {
      const source = this.context.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.context, 'pcm-capture');
      this.node.port.onmessage = ({ data }) => {
        if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
        this.emit('level', data.peak);
        this.socket.send(JSON.stringify({ type: 'audio', pcm16: toBase64(data.pcm) }));
      };
      source.connect(this.node);
      // Keep the worklet pulling without routing the mic to the speakers.
      const mute = this.context.createGain();
      mute.gain.value = 0;
      this.node.connect(mute).connect(this.context.destination);
      this.emit('connecting');
    };
  }

  handle(event) {
    switch (event.type) {
      case 'ready':
        this.rate = event.rate || this.rate;
        this.emit('ready', event);
        break;
      case 'audio':
        this.play(event.pcm16);
        break;
      case 'playback.clear':
        this.clear();               // barge-in: drop anything still queued
        break;
      case 'caller.said':
        this.emit('caller', event.text);
        break;
      case 'assistant.done':
        this.emit('assistant', event.text);
        break;
      case 'tool':
        this.emit('tool', event);
        break;
      case 'error':
        this.emit('error', event.message || 'Voice error.');
        break;
      case 'turn.done':
        this.emit('turnDone');
        break;
    }
  }

  play(base64) {
    if (!this.context || !base64) return;
    const bytes = fromBase64(base64);
    const samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
    const buffer = this.context.createBuffer(1, samples.length, this.rate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 0x8000;
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    const now = this.context.currentTime;
    this.playAt = Math.max(this.playAt, now + 0.03);
    source.start(this.playAt);
    this.playAt += buffer.duration;
    this.queue.push(source);
    source.onended = () => { this.queue = this.queue.filter((s) => s !== source); };
    this.emit('speaking', true);
  }

  clear() {
    for (const source of this.queue) { try { source.stop(); } catch (e) { /* already ended */ } }
    this.queue = [];
    this.playAt = 0;
    this.emit('speaking', false);
  }

  interrupt() {
    this.clear();
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: 'interrupt' }));
    }
  }

  stop() {
    this.active = false;
    this.clear();
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      try { this.socket.send(JSON.stringify({ type: 'end' })); } catch (e) { /* closing */ }
    }
    if (this.socket) { this.socket.close(); this.socket = null; }
    if (this.node) { this.node.disconnect(); this.node = null; }
    if (this.stream) { this.stream.getTracks().forEach((t) => t.stop()); this.stream = null; }
    if (this.context) { this.context.close(); this.context = null; }
    this.emit('stopped');
  }
}

function toBase64(arrayBuffer) {
  const bytes = new Uint8Array(arrayBuffer);
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function fromBase64(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
