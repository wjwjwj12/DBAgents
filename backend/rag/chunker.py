import re
from typing import List, Dict, Any

class DocumentChunker:
    @staticmethod
    def _search_terms(text: str) -> set[str]:
        lowered = text.lower()
        terms = set(re.findall(r'[a-z0-9_]+', lowered))
        for sequence in re.findall(r'[\u4e00-\u9fff]+', lowered):
            if len(sequence) == 1:
                terms.add(sequence)
                continue
            terms.update(sequence[index:index + 2] for index in range(len(sequence) - 1))
        return terms

    @staticmethod
    def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> List[Dict[str, Any]]:
        """
        Splits text into manageable chunks with overlap to retain context boundaries.
        """
        if not text:
            return []
            
        paragraphs = text.split('\n\n')
        chunks = []
        current_chunk = ""
        chunk_idx = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
                
            if len(current_chunk) + len(para) <= chunk_size:
                current_chunk += para + "\n\n"
            else:
                if current_chunk:
                    chunks.append({
                        "chunk_index": chunk_idx,
                        "content": current_chunk.strip(),
                        "token_count": len(current_chunk) // 2 # Rough estimate
                    })
                    chunk_idx += 1
                # Handle paragraph longer than chunk_size
                if len(para) > chunk_size:
                    for i in range(0, len(para), chunk_size - overlap):
                        sub_str = para[i:i + chunk_size]
                        chunks.append({
                            "chunk_index": chunk_idx,
                            "content": sub_str,
                            "token_count": len(sub_str) // 2
                        })
                        chunk_idx += 1
                    current_chunk = ""
                else:
                    current_chunk = para + "\n\n"
                    
        if current_chunk:
            chunks.append({
                "chunk_index": chunk_idx,
                "content": current_chunk.strip(),
                "token_count": len(current_chunk) // 2
            })
            
        return chunks

    @staticmethod
    def retrieve_relevant_chunks(chunks: List[Dict[str, Any]], query: str, top_k: int = 3, max_token_budget: int = 2000) -> List[str]:
        """
        Simple keyword-relevance based chunk retrieval with strict token budget.
        """
        if not chunks:
            return []
            
        query_words = DocumentChunker._search_terms(query)
        scored_chunks = []
        
        for c in chunks:
            content_terms = DocumentChunker._search_terms(c["content"])
            score = len(query_words & content_terms)
            scored_chunks.append((score, c["content"], c["token_count"]))
            
        # Sort by relevance score descending
        scored_chunks.sort(key=lambda x: x[0], reverse=True)
        
        selected = []
        accumulated_tokens = 0
        for score, content, token_count in scored_chunks[:top_k]:
            if accumulated_tokens + token_count <= max_token_budget:
                selected.append(content)
                accumulated_tokens += token_count
                
        return selected if selected else [chunks[0]["content"]] # Fallback to first chunk if none matched
