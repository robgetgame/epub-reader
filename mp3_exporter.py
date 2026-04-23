import os
import asyncio
import edge_tts
import threading

def run_export_background(book_name, chapter_name, epub_sentences, notes_objects, fallback_voice_id, status_callback, done_callback):
    def worker():
        try:
            # Setup folders
            base_dir = "Audio"
            if not os.path.exists(base_dir):
                os.makedirs(base_dir)
            
            # Clean book name
            safe_book = "".join([c for c in book_name if c.isalpha() or c.isdigit() or c==' ']).strip()
            if not safe_book: safe_book = "Unknown_Book"
            book_dir = os.path.join(base_dir, safe_book)
            if not os.path.exists(book_dir):
                os.makedirs(book_dir)
            
            safe_chapter = "".join([c for c in chapter_name if c.isalpha() or c.isdigit() or c==' ']).strip()
            
            # 1. Chunk EPUB chapter
            # epub_sentences is just a list of strings
            epub_chunks = []
            current_chunk = []
            current_len = 0
            for s in epub_sentences:
                st = s.strip()
                if not st: continue
                if current_len + len(st) > 2500 and current_chunk:
                    epub_chunks.append(current_chunk)
                    current_chunk = [st]
                    current_len = len(st)
                else:
                    current_chunk.append(st)
                    current_len += len(st)
            
            if current_chunk:
                epub_chunks.append(current_chunk)
            
            is_huge_chapter = len(epub_chunks) > 1
            
            async def render_text_to_bytes(text, voice):
                if not text.strip(): return b""
                communicate = edge_tts.Communicate(text, voice)
                audio_data = b""
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio_data += chunk["data"]
                return audio_data
                
            async def build_files():
                # Process EPUB Chunks
                for i, chunk in enumerate(epub_chunks):
                    status_callback(f"Rendering EPUB Part {i+1} / {len(epub_chunks)}...")
                    chunk_text = " ".join(chunk)
                    audio_bytes = await render_text_to_bytes(chunk_text, fallback_voice_id)
                    
                    if is_huge_chapter:
                        filename = f"{safe_chapter}.{i+1}.mp3"
                    else:
                        filename = f"{safe_chapter}.mp3"
                        
                    with open(os.path.join(book_dir, filename), "wb") as f:
                        f.write(audio_bytes)
                
                # Process Note Objects
                if notes_objects:
                    status_callback("Rendering Chapter Notes...")
                    note_bytes = b""
                    for note_obj in notes_objects:
                        text = note_obj.get("text", "").strip()
                        vid = note_obj.get("voice_id")
                        if not text or vid == "SKIP":
                            continue
                        
                        target_voice = vid if vid else fallback_voice_id
                        try:
                            ab = await render_text_to_bytes(text, target_voice)
                            note_bytes += ab
                        except Exception as e:
                            print(f"Skipped note snippet due to error: {e}")
                    
                    if note_bytes:
                        if is_huge_chapter:
                            # Save separately
                            filename = f"Notes for {safe_chapter}.mp3"
                            with open(os.path.join(book_dir, filename), "wb") as f:
                                f.write(note_bytes)
                        else:
                            # Append to the single chapter file!
                            filename = f"{safe_chapter}.mp3"
                            with open(os.path.join(book_dir, filename), "ab") as f:
                                f.write(note_bytes)
            
            # Windows Asyncio Policy for Edge-TTS
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            asyncio.run(build_files())
                
            status_callback("Export Complete!")
            done_callback(True)
        except Exception as e:
            print("Export error:", e)
            status_callback(f"Error: {str(e)}")
            done_callback(False)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
