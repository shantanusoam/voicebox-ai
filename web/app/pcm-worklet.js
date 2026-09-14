/* Captures mic audio as PCM16 frames for the realtime voice socket.
   Runs off the main thread so recording never competes with rendering. */
class PCMCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = [];
    this.target = 960; // 40 ms at 24 kHz
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      for (let i = 0; i < channel.length; i++) this.buffer.push(channel[i]);
      while (this.buffer.length >= this.target) {
        const slice = this.buffer.splice(0, this.target);
        const pcm = new Int16Array(slice.length);
        let peak = 0;
        for (let i = 0; i < slice.length; i++) {
          const clamped = Math.max(-1, Math.min(1, slice[i]));
          pcm[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
          peak = Math.max(peak, Math.abs(clamped));
        }
        this.port.postMessage({ pcm: pcm.buffer, peak }, [pcm.buffer]);
      }
    }
    return true;
  }
}
registerProcessor('pcm-capture', PCMCapture);
