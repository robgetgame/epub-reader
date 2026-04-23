# Native EPUB TTS Reader

A powerful, native Windows GUI desktop application designed for reading EPUB books aloud with perfect sentence synchronization and multi-voice character acting. 

Written in pure Python, it features a dual-engine architecture that seamlessly bridges offline Windows narrators (SAPI5) with high-fidelity, online Microsoft Azure Neural voices (`edge-tts`).

## Features

- **Painless EPUB Reading**: Parses EPUB files flawlessly, stripping out bloated HTML/CSS and splitting text precisely by punctuation into clean, readable sentences.
- **Synchronized Highlighting**: The currently spoken sentence is highlighted on-screen in real time, with the viewport automatically scrolling to keep your reading position comfortably centered.
- **Dual-Engine Architecture**: 
  - **Offline Mode**: Uses your PC's native, built-in Windows COM SAPI5 voices for extremely stable, zero-latency offline reading.
  - **Online Neural Mode**: Connects to Microsoft's Azure Edge-TTS websockets. It features an aggressive background lookahead buffer that pre-downloads the next upcoming sentences to a local cache, allowing gapless, zero-latency playback of ultra-high-quality Neural voices.
- **Chapter Notes & Multi-Voice Scripting**: 
  - A dedicated third pane for taking chapter-specific notes or pasting loose text. Notes are automatically saved to `config.json` and tied to the specific EPUB chapter you are on.
  - **Multi-Voice Acting**: In the Chapter Note area, you can dynamically assign different TTS voices to specific character dialogues by wrapping their words in `{character}` tags. The application will dynamically jump between Offline/Online rendering engines sentence-by-sentence depending on who is talking!

## Multi-Voice Scripting Guide

You can designate speakers in your notes by simply wrapping their text. Text without tags will use your global fallback voice.

### Supported Tags
- `{yunxi}` ... `{/yunxi} ` (Male, Chinese)
- `{xiaoxiao}` ... `{/xiaoxiao}` (Female, Chinese)
- `{yunyang}` ... `{/yunyang}` (Male Narrator, Chinese)
- `{guy}` ... `{/guy}` (Male, English)
- `{aria}` ... `{/aria}` (Female, English)
- `{jenny}` ... `{/jenny}` (Female Narrator, English)

*Example:*
> {yunxi} "Greetings, traveler!" {/yunxi} The barkeep smiled warmly. {aria} "Can I get a drink?" {/aria}

## Installation & Running

### Download Executeable (Windows)
1. Navigate to the **[Releases](https://github.com/robgetgame/epub-reader/releases)** page on the right side of this GitHub repository.
2. Download the latest `main.exe` binary file under **Assets**.
3. Double click the `.exe` file to run the Reader instantly! No Python or external dependencies are required.

### Run via Command Line
If you wish to run the project from standard Python source code:
1. Ensure Python 3.8+ is installed on your Windows machine.
2. Clone this repository.
3. Install dependencies:
   ```cmd
   pip install -r requirements.txt
   ```
4. Run the application:
   ```cmd
   python main.py
   ```

## Compiling Your Own EXE
To compile this project yourself into a single, portable Windows `.exe` application:
1. Ensure you have `pyinstaller` installed (`pip install pyinstaller`).
2. Run the build command:
   ```cmd
   pyinstaller --onefile --windowed main.py
   ```
3. Locate `main.exe` in the generated `/dist` folder.
