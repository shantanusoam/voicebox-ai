# Four critique-and-fix rounds

These are structured **self-review** passes with executed evidence. No independent critic agent or external certification was used. Reusable agent prompts are included under agents/. Scores evaluate the intended **local software-lab release**, not clinical safety, phone compatibility or SaaS production readiness.

Rubric: workflow correctness 30 points; transport testability 25; usable/accurate interface 20; defensive controls 15; reproducible handoff 10. Scores are engineering judgments, not statistical confidence.

| Round | Focus | Concrete changes / evidence | Score |
|---|---|---|---:|
| 1 | End-to-end executable foundation | Implemented persistent calls, explicit local booking, provider contracts, real WS echo and console; 109 initial tests passed | 82/100 |
| 2 | Retry, security and media failure behavior | Serialized paid audio uploads, cache before TTS, validated IDs before billed work, bounded JSON, turn cap and live device revocation; 116 tests passed | 88/100 |
| 3 | Actual operator experience | Added active-session recovery, erased one-time tokens from closed dialogs, fixed India-time date labels and 320px grid overflow; strengthened small labels/contrast; 86 browser checks passed | 91/100 |
| 4 | Release and truthfulness | Added non-ASCII login rejection, lock cleanup, retention regression, complete setup/protocol/security documentation, native 100-frame loopback, C tests and packaging; 119 backend tests passed | 92/100 |

## Final scoped scoring

| Area | Points | Remaining limitation |
|---|---:|---|
| Local workflow correctness | 29/30 | Limited scheduling language and one simple daily calendar |
| Transport testability | 23/25 | Real native software gateway, but no phone/board transport |
| Operator interface | 19/20 | Browser relay limitation, not a complete accessibility audit |
| Defensive lab controls | 12/15 | Single-admin/local deployment; production hardening and review absent |
| Developer handoff | 9/10 | Reproducible commands; Docker and remote CI templates not executed |
| **Total, local-lab scope** | **92/100** | **Not a production or hardware-readiness score** |

## Important rejected shortcuts

A mock transcription is not a real microphone conversation. A network echo is not proof of Bluetooth audio. A database appointment is not a Google Calendar write. A staff task is not a live phone transfer. A server-side interruption cannot clear audio queued in unimplemented hardware. A configured provider key does not prove its validity or quality. The UI and documentation state those boundaries explicitly.

The source and tests constitute a substantial working foundation. The next acceptance gate is still the phone-side, consented two-way audio experiment on a documented phone/board/SDK combination, followed by live provider evaluation and human fallback.
