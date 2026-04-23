import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
import re
import warnings

# Suppress EbookLib warnings about syntax
warnings.filterwarnings('ignore', category=UserWarning, module='ebooklib')
warnings.filterwarnings('ignore', category=FutureWarning, module='ebooklib')

class EpubParser:
    def __init__(self):
        self.book = None
        self.chapters = [] 
    
    def load_epub(self, file_path):
        try:
            self.book = epub.read_epub(file_path)
            self._extract_toc()
            return True
        except Exception as e:
            print(f"Error loading EPUB {file_path}: {e}")
            return False

    def _extract_toc(self):
        self.chapters = []
        
        spine_ids = [item[0] for item in self.book.spine]
        
        count = 1
        for item_id in spine_ids:
            item = self.book.get_item_with_id(item_id)
            if item and item.get_type() == ebooklib.ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), 'html.parser')
                title_tag = soup.find(['h1', 'h2', 'h3', 'title'])
                title = title_tag.get_text()[:40].strip() if title_tag else f"Chapter {count}"
                if not title:
                    title = f"Chapter {count}"
                
                self.chapters.append({'title': title, 'id': item_id, 'index': len(self.chapters)})
                count += 1
                
    def get_chapter_list(self):
        return self.chapters
        
    def get_chapter_text(self, chapter_index):
        if not self.book or chapter_index < 0 or chapter_index >= len(self.chapters):
            return []
            
        item_id = self.chapters[chapter_index]['id']
        item = self.book.get_item_with_id(item_id)
        if not item:
            return []
            
        soup = BeautifulSoup(item.get_content(), 'html.parser')
        
        # Remove non-text elements
        for unwanted in soup.find_all(['img', 'table', 'figure', 'svg', 'script', 'style', 'head', 'math']):
            unwanted.decompose()
            
        text = soup.get_text(separator='\n')
        text = re.sub(r'\n\s*\n', '\n', text)
        text = text.strip()
        
        if not text:
            return []
            
        return self.split_into_sentences(text)

    def split_into_sentences(self, text):
        text = text.replace('\r', '')
        # Split by punctuation or newlines
        parts = re.split(r'(?<=[.?!。？！\n])\s*', text)
        return [p.strip() for p in parts if p.strip()]
