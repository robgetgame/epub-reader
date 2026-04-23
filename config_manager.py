import json
import os

CONFIG_FILE = "bookmarks.json"

class ConfigManager:
    def __init__(self):
        self.config = {
            "recent_files": [], # List of file paths
            "last_file": None,
            "bookmarks": {}, # file_path -> {"chapter_idx": 0, "sentence_idx": 0}
            "voice_id": None,
            "speech_rate": 200,
            "sandbox_text": "",
            "sandbox_sentence_idx": 0,
            "chapter_notes": {} # file_path -> {"chapter_idx": "text"}
        }
        self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # ensure we don't overwrite with corrupted data
                    if isinstance(data, dict):
                        self.config.update(data)
            except Exception as e:
                print(f"Error loading config: {e}")

    def save(self):
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4)
        except Exception as e:
            print(f"Error saving config: {e}")

    def add_recent_file(self, file_path):
        if file_path in self.config["recent_files"]:
            self.config["recent_files"].remove(file_path)
        self.config["recent_files"].insert(0, file_path)
        # Keep only max 10 recent files
        self.config["recent_files"] = self.config["recent_files"][:10]
        self.config["last_file"] = file_path
        self.save()

    def set_bookmark(self, file_path, chapter_idx, sentence_idx):
        self.config["bookmarks"][file_path] = {
            "chapter_idx": chapter_idx,
            "sentence_idx": sentence_idx
        }
        self.save()

    def get_bookmark(self, file_path):
        return self.config["bookmarks"].get(file_path, {"chapter_idx": 0, "sentence_idx": 0})
        
    def set_voice_and_rate(self, voice_id, rate):
        self.config["voice_id"] = voice_id
        self.config["speech_rate"] = rate
        self.save()
        
    def save_sandbox(self, text, sentence_idx):
        self.config["sandbox_text"] = text
        self.config["sandbox_sentence_idx"] = sentence_idx
        self.save()
        
    def load_sandbox(self):
        return self.config.get("sandbox_text", ""), self.config.get("sandbox_sentence_idx", 0)

    def save_chapter_note(self, file_path, chapter_idx, text):
        if "chapter_notes" not in self.config:
            self.config["chapter_notes"] = {}
        if file_path not in self.config["chapter_notes"]:
            self.config["chapter_notes"][file_path] = {}
            
        self.config["chapter_notes"][file_path][str(chapter_idx)] = text
        self.save()
        
    def load_chapter_note(self, file_path, chapter_idx):
        if "chapter_notes" not in self.config:
            return ""
        file_notes = self.config["chapter_notes"].get(file_path, {})
        return file_notes.get(str(chapter_idx), "")
