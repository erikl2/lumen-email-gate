# Demo video

[`demo.webm`](demo.webm) (1:46, 1280×864, VP8 video with Opus narration and burned-in captions; poster frame: [`poster.png`](poster.png)) walks through the Lumen Cloud **Outbound Email Gate** in the order of [`DEMO_SCRIPT.md`](../../DEMO_SCRIPT.md):

1. The stakes.
2. A loyalty-discount reply that is **APPROVED**, with three facts verified against the price sheet and policies, and the email sent.
3. "Business is $99/mo with a first response within 24 hours" is **BLOCKED**, even though Ezio rated it GREEN 100. "24 hours" is struck through, the evidence table names the file and field, and the email is not sent.
4. An engineering promise is **HELD FOR REVIEW**. A reviewer ("Dana (support lead)") approves it with a logged reason, it is sent after human approval, and the review appears in the **Audit trail**.
5. The **Gate simulator** runs all 16 practice drafts: 8 sent, 2 held, 6 blocked. Held and blocked drafts never reach `send_email`.
6. The **Source of truth** tab shows every threshold with its reason. The narration then states the verified practice results (16/16 exact, all six failure drills pass, 114/114 on the team's generality set), says the final judged results are in the report, and names one limitation: the claim reader is pattern-based.

**How it was made:** nothing in the video is mocked up or edited by hand. It is the real app (`python3 gate.py serve --persona customer_support --latency 1-2`: the real wrapper, the supplied mock Ezio, and the persona page), driven by a script in headless Chrome over the DevTools protocol. The script types, clicks and scrolls at a readable pace while a screencast captures frames. Every decision shown came from the wrapper's `POST /decide`. The narration is macOS `say` (voice Samantha). Frames, captions and narration were then combined in headless Chrome (canvas plus WebAudio into MediaRecorder). The WebM duration header was added afterwards, because MediaRecorder omits it.
