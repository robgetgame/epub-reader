# Design audit — 2026-09-12

Verdict: revise before implementation. The report correctly identifies substantial defects, but B1 and B5 are incomplete data/state fixes, and the proposed nonmodal AI dialog introduces another way to overwrite the wrong chapter. Fix persistence and playback lifecycle before adding AI.

Scope: audited DESIGN-REVIEW-2026-09-12.md and the six production Python files in the working directory. No production code, bookmarks, cache, or Git metadata was modified. The design describes planned changes; there is no ai_chat.py implementation to approve. Git reports missing blobs and an invalid cache-tree, and main has no commits, so an exact historical diff cannot be established. Source locations below refer to the current working files, not the report's stale line numbers.

## Priority findings

### [P1] B1 still erases chapter zero on first load

Location: main_window.py:237–255; initialization at 35–39.

`current_chapter_idx` exists from construction and equals zero. On the first `_load_chapter`, the purported first-load guard therefore saves the empty right-hand widget into the newly assigned book's chapter-zero note before reading it. Moving one save earlier in `_load_epub` does not fix this if the save inside `_load_chapter` remains. A focused fake-widget execution reproduced an existing chapter-zero note becoming an empty string.

Use an explicit displayed-note identity, initially absent: `(book_key, chapter_key)`. Save only against that identity, then replace it when loading the destination note. Keep `__scratch__` as a distinct identity. Save the old note before changing parser/book state, and commit new book state only after loading succeeds. Do not infer ownership from the destination parser's chapter count.

Migration must preserve the original bookmarks file and nonempty legacy sandbox text before deleting fields. Its origin is already unreliable: an empty destination chapter does not prove that the text belongs there. Preserve it as a recoverable legacy scratch entry, including its old position; never overwrite an existing scratch entry. Make migration versioned and idempotent. Store note playback position per note identity, separately from EPUB position, and reset/invalidate it after edits. Simply deleting `sandbox_sentence_idx` drops that required behavior.

### [P1] B5 callback IDs do not prevent canceled playback from starting

Location: tts_thread.py:152–153, 257–269; main_window.py:590–596.

A queued PLAY followed by Stop can still run: `_handle_play` clears the shared stop event when it eventually dequeues PLAY. A focused execution reproduced highlight and completion callbacks after cancellation, with the stop flag cleared. Filtering callbacks alone would hide these events while allowing unwanted audio.

Give each playback command its own cancellation token and identity. Cancel both queued and active sessions on Stop, navigation, replacement Play, and shutdown. Reject canceled commands before playback; never clear their token. Invalidate the UI session on Stop as well as Play. Check session identity when the queued UI callback actually executes.

The delayed `root.after(500, self._start_play)` is a second path outside worker callbacks. Navigation or starting note playback during that interval can still trigger an unsolicited EPUB start. Retain/cancel the timer and check the expected chapter and session at execution. Apply equivalent guards to highlight, completion, failure/status, and any queued start.

### [P1] AI generation can overwrite another chapter or newer edits

Design locations: D7/D9; sections 4.3–4.4.

The dialog is nonmodal, but completion is specified as writing to the right pane and calling the current chapter's save method. Generate in A, navigate to B, then finish: a naive implementation writes A's result into B. Even without navigation, confirmation before streaming does not authorize overwriting text typed during generation.

Capture immutable book/chapter identity, chapter text, note revision, model, and request ID. Persist against the captured identity. Update the visible right pane only if it still displays that identity. Recheck the note revision before replacement; retain the result in the dialog and require a new overwrite choice if the note changed. Bind “put into note” to the dialog's chapter too. Allow only one active request per dialog, and prevent concurrent dialogs from silently overwriting the same note. Closing a dialog invalidates already queued deltas and completion callbacks, not just future network reads. Manual book-type changes must similarly supersede a late automatic classification.

### [P1] Direct JSON rewrites can destroy every saved note

Location: config_manager.py:20–36; main_window.py:586–588.

Opening bookmarks.json with `w` truncates the only copy before serialization completes. A crash or write failure can destroy all notes; the next startup catches the parse error and continues with defaults, which later saves over the damaged file. This existing defect becomes more consequential when chat and usage persistence are added.

Write a validated snapshot to a unique sibling temporary file, flush it, then atomically replace the destination. Keep a recoverable last-good backup. Preserve malformed files and surface load/save errors in the UI. Validate nested types and bookmark bounds. Serialize mutations/writes through one owner; atomic replacement alone does not prevent two snapshots from losing each other's updates. Apply the same approach to ai_chat.json. Preserve request IDs so successful usage accounting is not counted twice.

### [P1] Worker initialization can hang startup indefinitely

Location: tts_thread.py:114–125, 284–285; main_window.py:170–171.

COM initialization, Dispatch, and voice enumeration occur outside the worker's exception handler. Failure exits the thread before `ready_event.set()`, while the Tk thread waits indefinitely in `get_voices()`. Lazy pygame initialization fixes only the separate audio-device startup failure.

Always publish an initialization outcome, including errors. Poll readiness from Tk without an unbounded UI-thread wait. Neural playback should remain available when SAPI initialization fails. An audio-device failure is not necessarily repaired by switching to SAPI; expose the error and an explicit available fallback.

### [P2] B2 needs a result lifecycle and cache ownership rules

Location: tts_thread.py:34–97, 199–239.

An Event plus a lock is a sound completion signal, but an Event alone does not distinguish success, failure, cancellation, or a vanished cache file. Atomically install a job record containing result/error and completion event, signal it in `finally`, and retain results long enough for waiters. Bound downloader concurrency and pending work; navigation should not accumulate old-chapter download threads or text references. Define retry attempts/backoff and remove failed registry entries deliberately.

Waiting with `event.wait(0.1)` plus session cancellation checks is sufficient for this application. Do not hold the registry lock while waiting or downloading. Pin currently used and prefetched audio; eviction must exclude active temporary files. “Clean .tmp too” without tracking active jobs can delete an in-progress download. Unique temporary names are useful, especially if multiple app processes share the cache; a per-instance registry does not deduplicate across processes.

The report overstates permanent failure: `trigger_download` currently retries even when `.error` exists, and the wait loop can use a subsequently created .mp3. A stale error nevertheless causes immediate skips while the retry runs. The undefined `e` in the exhausted retry loop is real.

### [P2] Playback failures still silently count as success

Location: tts_thread.py:221–251; mp3_exporter.py:79–83, 101–102.

B7 covers only download waiting. pygame load/play failures and SAPI failures are swallowed and can still trigger successful chapter completion. Export skips failed note snippets and reports success. Send all failures through explicit, session-bound outcomes. If skipping remains the chosen policy, report it immediately and keep the failed chapter/sentence identifiers visible after auto-advance; chapter-end-only reporting can leave the app silent for a long time while disconnected. Complete every session with a terminal state so controls reset reliably.

### [P2] Existing selection and settings behavior remains outside the fixes

Location: main_window.py:188–193, 308–332; tts_thread.py:155–161.

Selection playback uses only the selection start's sentence tag and reads the rest of the chapter. It ignores the selected endpoint and partial-sentence boundaries; ordinary cursor positioning is also ignored. Implement a bounded selection playback range with a continuation offset, and suppress chapter auto-advance when the selection ends.

Voice changes never reach the active worker session. A shared rate variable will also do nothing unless the playback loop rereads it. Apply settings changes at a defined sentence boundary or restart the interrupted sentence with a new session. Neural prefetch/cache lookup must use the new rate and voice. Keep every SAPI operation on its owning worker thread: `stop()` currently invokes the worker-created COM object from the UI thread at tts_thread.py:270–274.

### [P2] Export names can overwrite other chapters

Location: epub_parser.py:35; mp3_exporter.py:15–21, 60–65.

Duplicate chapter titles, especially the repeated book title already identified in B8, produce the same filename and overwrite previous exports. Sanitization also collapses distinct names. Include a stable book identity and chapter ordinal; handle empty names, Windows reserved device names, and path length. Use temporary output plus atomic replacement, and define overwrite behavior. Separate chapter and note files fixes one concatenation path, but `note_bytes += ab` still concatenates independent MP3 streams. Validate resulting playback/duration or use a proper merge/remux approach if accurate metadata is required.

## Corrections to the original bug list

| Item | Audit result |
| --- | --- |
| B1 | Confirmed, with the additional first-load erasure and migration/position requirements above. |
| B2 | Duplicate writers, stale errors, and undefined `e` confirmed. “Permanent” is too strong because retry still starts. Cache cleanup must respect active jobs. |
| B3 | Root shortcuts affect editable controls. Returning for every Text also disables desired shortcuts when the read-only EPUB pane has focus. Exempt editable Text/Entry/Combobox and controls consuming arrows, while preserving reader shortcuts. The claim that the cursor never moves is too strong: the Text class binding runs first. |
| B4 | Confirmed. Restore through the chapter-loading API without an intermediate persisted zero; validate chapter and sentence bounds and persist the actual restored position. |
| B5 | Stale updates confirmed. The specific claim that a larger index necessarily causes TclError is incorrect: `_highlight_sentence` checks the sentence count before tag_add. It still assigns the wrong in-memory position, and an in-range stale index can overwrite the new bookmark. |
| B6 | Confirmed with closing Chinese quotes, decimals, ellipses, and repeated punctuation. Preserve text/offset mappings and punctuation groups. Standalone punctuation should be nonspoken, not sent to Edge; short real utterances must remain readable. |
| B7 | Timeout skipping confirmed, but the remedy must also cover playback/export errors and guarantee a terminal UI state. |
| B8 | Copy binding, queued rate updates, missing export rate, import-time mixer initialization, title priority, and synchronous title parsing are real. The cited exporter lines 343/390/395 do not exist; the file has 109 lines. |

Two B8 remedies need revision. A directory beside the executable may be unwritable; prefer a per-user application-data directory, with an explicit portable mode and migration of existing files. Reading only the first 4 KB is a heuristic, not a reliable title parser: use EPUB navigation labels/metadata with fallback titles. A thread-local event loop avoids export changing process-global policy. Section 4.7 says to leave exporter and SAPI unchanged, contradicting necessary fixes in both.

## AI protocol, context, and persistence decisions

1. **SSE and cancellation.** Parse complete SSE events, skip comments, handle split UTF-8 and data fields, and detect in-stream errors even after HTTP 200. Consume the terminal usage frame and finish reason; EOF, empty output, `length`, and cancellation must not silently replace a note as a complete result. OpenRouter documents these protocol cases in its [streaming reference](https://openrouter.ai/docs/api_reference/streaming).

   A cancellation Event cannot interrupt a blocking readline. With urllib, a practical first version can immediately invalidate the UI request and let the network worker terminate on a finite read timeout; document that bound and cap outstanding workers. If prompt transport cancellation is required, use a tested transport with explicit connection ownership. Do not repeatedly resume a buffered readline after socket timeouts: Python warns the buffer may become inconsistent. Cross-thread closing through private socket attributes is not a robust public API. See [Python socket.makefile](https://docs.python.org/3/library/socket.html#socket.socket.makefile). Add an overall deadline as well as an inactivity timeout, so keepalives cannot prolong work indefinitely.

2. **Model and structured JSON.** The proposed model ID exists. Its current OpenRouter page lists a 1M context and the quoted starting prices, but provider prices differ. It supports JSON output without schema enforcement. Validate `kind` against a single canonical enum (`fiction` / `nonfiction`; the report also says `non-fiction`) and validate language values. Missing fields or invalid enum values must take the manual fallback path, even when the JSON parses. See the [model page](https://openrouter.ai/deepseek/deepseek-v4.1-flash). This was a documentation check, not an authenticated model test.

3. **Costs.** Use reported `usage.cost` for completed request accounting, plus nested `prompt_tokens_details.cached_tokens`; retain the model/provider/request identity. A hardcoded model-only tariff cannot represent routing differences. Mark absent usage as unknown, including interrupted requests, rather than zero. Estimates may be shown separately. The quoted 6,000-in/600-out example computes to $0.00126 at the stated uncached tariff, but Chinese token counts and cache hits are estimates. See [OpenRouter usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting).

4. **Context growth.** A replaceable model invalidates reliance on one model's 1M window. Budget full input plus output and any reasoning allowance against the selected model's limits. Retain history on disk but send a bounded recent subset, visibly indicating omitted history. Do not duplicate the chapter and summary inside every stored turn. Avoid loading every book's entire chat history into memory and rewriting it on each delta. Preserve speaker roles, response status, request identity, and summary provenance. Distinguish generated summaries from arbitrary chapter notes when constructing previous-chapter context.

5. **Length and language.** `len(chapter_text)//10` is a character count, not an English word count. Use an explicit language-appropriate unit in counting and prompting. The 300 minimum is the approved rule but can exceed a short chapter's original length; instruct the model not to invent material to fill it and show actual output length. Skip empty chapters. Bound output by model limits and handle truncation explicitly. `target_chars*2` is a heuristic, not a completeness guarantee, especially for reasoning models. Include detected language explicitly while treating current chapter language as authoritative for mixed-language books.

6. **Prompt boundaries.** Keep book text and previous notes in untrusted data messages; delimiters alone cannot enforce the instruction hierarchy. No tools or executable actions is a reasonable scope. Validate output as nonempty plain text and do not automatically execute or interpret generated content. In particular, generated `{voice}` tags currently have an application effect through the existing note parser: reject/escape those for single-narrator generated summaries without breaking manually authored scripts. No classifier can guarantee immunity to prompt injection or factual errors.

7. **API configuration.** A running Windows process inherits an environment snapshot; a user-level variable added after the application starts may require restarting its launcher/app. Never log keys or raw request headers. Bound and sanitize error text, including redacting the actual secret if echoed. An arbitrary `base_url` would receive the same bearer key: restrict to the intended HTTPS origin by default and do not forward authorization on a cross-origin redirect. Keep model configuration available independently from credential-destination changes.

8. **Thread/UI ownership.** Prefer worker-to-UI queues drained by a Tk-owned timer. This makes shutdown and stale-result rejection explicit and avoids background calls into destroyed Tk state. Apply it to export callbacks too: users can close the export Toplevel while its thread still schedules updates to its destroyed label. For long chapters, batch text insertion and stream deltas instead of scheduling an unbounded number of individual UI callbacks.

## Repository, memory, and verification

- S1 is partly stale: `.gitignore` contains 16 NUL bytes and does not ignore root bookmarks.json or test.mp3, but dist/ is already ignored, including dist/bookmarks.json. Ignore migration backups, private chat files, and logs containing local data too. Ignore rules do not remove files already tracked or published. Remote publication/privacy was not independently audited.
- S2 is confirmed by `git fsck --full`. Preserve the entire workspace and recoverable Git data before reconstruction; copy only intended source into a clean clone, not every untracked file. “Clean git status” is insufficient evidence of recovered work or private-data exclusion. This audit did not clone, delete, commit, or push anything.
- The parser retains `self.book` and synchronously parses every spine document. Lazy title extraction does not by itself prove chapter-bounded memory. Validate the pinned EbookLib version's loading behavior and measure resident memory on a million-word EPUB with large resources. The current interpreter lacks EbookLib, so its installed runtime behavior was not measured here. Also release obsolete playback/export/dialog chapter snapshots; nonmodal dialogs holding full chapter text need an explicit lifetime limit.
- S6's absence of extraction/path traversal is useful but does not imply no resource risks: bound expanded EPUB entry sizes and total parsing work. Treat this as robustness for malformed or huge books, not an assertion of a demonstrated exploit.
- Existing requirements also differ from the project instructions: six Neural voices rather than exactly four, direct win32com rather than pyttsx3, and automatic restoration rather than an offer to resume. The report explicitly preserves the user's Yunyang workflow; reconcile these specifications before treating the older requirements as acceptance criteria.

Executed checks: in-memory syntax compilation of all six production files passed on Python 3.14.5. AST-extracted real methods with controlled dependencies reproduced closing-quote/decimal/ellipsis splitting, queued-PLAY cancellation loss, and startup chapter-zero note erasure. Git integrity and ignore checks confirmed the results above. These isolated checks did not exercise real Tk widgets, COM, audio, live API calls, or packaging. The first probe hit console encoding limitations; the corrected probe completed. No credentials or note contents were read for these tests.

Before implementation sign-off, retain repeatable tests even if they are small standalone scripts: first-open/migration without note loss; A-to-B switching; Stop before PLAY is dequeued; Stop during downloads/audio; delayed auto-advance cancellation; stale callbacks; one writer per cache key with retry/eviction; all-failure terminal UI states; note edits/navigation during AI generation; close during a blocked/partial stream; truncated JSON recovery; invalid classification; usage-only and error SSE frames; and large-book memory. Add manual real-Windows checks for voice changes, punctuation playback, keyboard/copy behavior, and packaged startup. `py_compile`, one successful live request, and zero .error files do not establish these properties.

Recommended implementation order: preserve data and reconstruct Git; atomic persistence and lossless migration; displayed-note ownership and bookmark restoration; playback cancellation/terminal states and COM initialization; downloader/cache lifecycle; remaining controls/export fixes; AI transport tests; request-bound dialog/persistence; real application and packaged validation. Do not package merely because syntax checks pass.
