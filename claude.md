# Project: Native Windows EPUB Text-to-Speech (TTS) Reader

## 1. Functional Requirements
* **Core Objective:** Build a Windows desktop application capable of opening `.epub` files and reading them aloud, strictly processing one chapter at a time to handle massive text payloads (1,000,000+ words).
* **EPUB Parsing & Pagination:**
    * Implement an "Open File" dialog to select an `.epub`.
    * Parse the EPUB spine. Extract and clean the text of the chapter. Ensure all non-text content (images, tables, etc.) is scrubbed and ignored.
    * Provide a Table of Contents (TOC) list/dropdown to jump between chapters, alongside sequential "Next Chapter" and "Previous Chapter" buttons.
    * When navigating chapters, the application MUST clear the main text area and flush the previous chapter's string data from memory to prevent bloat.
    * **Continuous Play (Auto-Advance):** When TTS finishes reading the final sentence of a chapter, automatically load and begin reading the next chapter.
    * **Chapter Navigation During Playback:** If TTS is actively playing when the user navigates chapters, immediately stop playback and load the new chapter.
* **Playback Controls:**
    * **Play:** Triggers speech synthesis for the currently loaded chapter. If the user has a text selection active, read only the selected text first, then on the next Play continue from where the selection ended. If no selection, read from the current bookmark/cursor position.
    * **Stop:** Immediately halts the active speech engine. *Crucial:* To prioritize stability over native engine pausing, the TTS will process text strictly sentence-by-sentence. "Stop" will break the read loop. "Play" will simply restart from the beginning of the interrupted sentence, avoiding nasty Windows COM deadlock bugs.
* **Raw Text Sandbox:** 
    * Add a permanent 3rd column to the right of the main EPUB reader.
    * This text area is fully editable (unlike the EPUB reading area) allowing users to paste raw strings of text.
    * Includes Multi-Voice Scripting: Users can wrap text in `{yunxi}` / `{/yunxi}`, `{xiaoxiao}`, `{guy}`, or `{aria}` tags. The TTS engine will dynamically override the active fallback voice and stream these blocks using the designated Male/Female Edge-TTS online voices.
    * Provide a completely separate, dedicated set of Play/Stop buttons directly underneath this Paste Box to trigger TTS for its contents.
    * When playing the Sandbox, it chops the contents into sentences via the exact same Chinese/English punctuation logic. Highlighting should apply to this box independently.
    * Bookmarking for this Sandbox must be kept strictly separate from the EPUB bookmarks in `bookmarks.json`.
* **Word Highlighting:** As TTS reads, highlight the current sentence in the main text area in real time, keeping the view scrolled to follow the reading position. Segment text (especially Chinese) by punctuation-delimited sentences for reliable syncing without bugs.
* **Speed Control:** Implement a UI slider bound to the TTS engine's speech rate.
* **Voice Selection (Dual-Engine):** Provide a dropdown listing all installed Windows SAPI5 voices alongside exactly four premium Microsoft Neural voices (`zh-CN-XiaoxiaoNeural`, `zh-CN-YunxiNeural`, `en-US-AriaNeural`, `en-US-GuyNeural`). Changing the voice mid-session seamlessly swaps between the native offline engine (SAPI5) and the online Edge-TTS engine.
* **Edge-TTS Neural Buffering & Cache:** When a Neural voice is playing, the application uses an aggressive lookahead buffer to download upcoming sentences/chunks (B and C) as `.mp3` files while chunk (A) is actively playing, guaranteeing zero latency. Rendered audio files are stored in a local `/temp_audio/` folder. Files are only deleted when the directory size exceeds 500 MB to save repetitive downloads during chapter scrubbing.
* **Session Bookmarking:** Automatically save the last-read position (EPUB file path, chapter index, and word/character offset) to a local config file (e.g., `bookmarks.json`) on Stop or app close. On next launch with the same EPUB, offer to resume from the saved position.
* **Recent Files:** Maintain a "Recent Files" list in a File menu to quickly reopen previously read books.
* **UI/UX:** Provide a Tkinter desktop interface locked to a Dark Mode aesthetic. Maintain a fixed, readable font size suitable for large Chinese characters. Include a 3-pane layout: [TOC Sidebar] | [Main EPUB Text & Controls] | [Raw Paste Box & Separate Controls]. Implement keyboard shortcuts: `Spacebar` for Play/Stop of the primary EPUB reader, `Left/Right Arrows` for Previous/Next Chapter.

## 2. Technical Architecture & Constraints
* **Stack:** Python 3.x.
* **UI Framework:** `tkinter`.
* **EPUB Processing:** Utilize `EbookLib` to parse the `.epub` archive and `beautifulsoup4` to strip HTML tags from the raw chapter content, yielding clean string data.
* **TTS Engine:** Utilize `pyttsx3` for native offline Windows SAPI5 voice integration.
* **Concurrency (Critical):** The TTS playback (`engine.say()` and `engine.runAndWait()`) MUST execute on a separate daemon thread via the `threading` module. Do not block the Tkinter main loop.
* **Packaging:** First, build and rigorously test the Python script to ensure stability and absence of bugs. ONLY after the script is fully functional and verified, autonomously compile it into a standalone Windows executable. Execute `pip install pyinstaller pyttsx3 EbookLib beautifulsoup4` and then run `pyinstaller --onefile --windowed main.py`.

## 3. Claude Code Agentic Directives
* **Cognitive Framework:** Operate in High-Level Communication (HLC) mode. Assume a senior-level engineering context.
* **Zero Fluff:** Omit all conversational filler, apologies, or explanations. Output only functional code and terminal commands.
* **Code Completeness:** Placeholders (e.g., `# existing code...`) are strictly forbidden. All Python scripts must be fully written and production-ready.
* **Autonomy:** If a library dependency is missing, install it via `pip`. If PyInstaller compilation fails, read the terminal traceback, diagnose the root cause, and implement the fix natively.