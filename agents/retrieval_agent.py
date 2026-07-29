"""
retrieval_agent.py
--------------------
Retrieval Agent for the school management platform, built from the RAG
prototype in school_RAG_based_system.py.

Combines a FAISS vector store (built from a school knowledge-base text
file) with Google's Gemini LLM to answer natural-language questions
grounded in that document set.

Unlike data_agent.py and prediction_agent.py, this agent DOES depend on
external services:
    - Google's Generative AI API (Gemini), for answer generation
    - A one-time download of a sentence-transformer embedding model
      (from Hugging Face) the first time an index is built

Everything else about its calling convention matches the shared
OrchestratorAgentInterface contract used across all three agents:

    handler = RetrievalAgentHandler(knowledge_base_path="school_knowledge_base.txt")
    result = handler.handle({"query": "What are the fee payment policies?"})
    # -> {"status": "success"/"error", "agent": "retrieval_agent", "result"/"error": ...}

Environment:
    GEMINI_API_KEY (or GOOGLE_API_KEY) must be set for this agent to work.
    Required packages: faiss-cpu, langchain, langchain-community,
    langchain-huggingface, langchain-google-genai, langchain-text-splitters

Index caching:
    The FAISS index is built once from the knowledge-base file and saved
    to disk (index_cache_dir). Subsequent runs load the cached index
    instead of re-embedding the document, until the cache is cleared.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("retrieval_agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


class OrchestratorAgentInterface:
    """Same minimal contract every agent exposes to the orchestrator:
    a task dict in, a JSON-safe result dict out."""

    def handle(self, task: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class RetrievalAgent:
    """
    Builds (or loads a cached) FAISS index from a knowledge-base text
    file, and answers questions against it using Gemini.

    The index is built once and cached — both in memory for the life of
    this object, and on disk (via FAISS.save_local / load_local) so a
    fresh process doesn't have to re-embed the document either.
    """

    def __init__(
        self,
        knowledge_base_path: str,
        index_cache_dir: str = "faiss_index_cache",
        embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        gemini_model_name: str = "gemini-2.5-flash",
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
    ):
        self.knowledge_base_path = Path(knowledge_base_path)
        self.index_cache_dir = Path(index_cache_dir)
        self.embedding_model_name = embedding_model_name
        self.gemini_model_name = gemini_model_name
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

        self._vectorstore = None
        self._retriever = None
        self._qa_chain = None

    def _ensure_ready(self) -> None:
        """Lazily builds the index + chain on first use. Cheap, local
        checks (API key, file existence) fail fast before anything that
        touches the network or a heavy import."""
        if self._qa_chain is not None:
            return

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set. "
                "Export it in your environment before using the retrieval agent."
            )

        if not self.knowledge_base_path.exists():
            raise FileNotFoundError(f"Knowledge base file not found: {self.knowledge_base_path}")

        try:
            from langchain_community.document_loaders import TextLoader
            from langchain_text_splitters import CharacterTextSplitter
            from langchain_huggingface import HuggingFaceEmbeddings
            from langchain_community.vectorstores import FAISS
            from langchain_google_genai import ChatGoogleGenerativeAI
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_core.output_parsers import StrOutputParser
            from langchain_core.runnables import RunnablePassthrough
        except ImportError as exc:
            raise RuntimeError(
                "Missing dependency for the retrieval agent. Install with: "
                "pip install faiss-cpu langchain langchain-community "
                "langchain-huggingface langchain-google-genai langchain-text-splitters"
            ) from exc

        embeddings = HuggingFaceEmbeddings(model_name=self.embedding_model_name)

        if self.index_cache_dir.exists():
            logger.info("Loading cached FAISS index from %s", self.index_cache_dir)
            vectorstore = FAISS.load_local(
                str(self.index_cache_dir), embeddings, allow_dangerous_deserialization=True
            )
        else:
            logger.info("Building FAISS index from %s", self.knowledge_base_path)
            loader = TextLoader(str(self.knowledge_base_path))
            documents = loader.load()
            splitter = CharacterTextSplitter(chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
            docs = splitter.split_documents(documents)
            vectorstore = FAISS.from_documents(docs, embeddings)
            vectorstore.save_local(str(self.index_cache_dir))
            logger.info("Saved FAISS index to %s for reuse next run", self.index_cache_dir)

        self._vectorstore = vectorstore
        self._retriever = vectorstore.as_retriever()

        gemini_llm = ChatGoogleGenerativeAI(
            model=self.gemini_model_name, temperature=0.3, google_api_key=api_key
        )

        prompt = ChatPromptTemplate.from_messages([
            ("system", "You are an assistant for question-answering tasks about this school. "
                       "Use the following context to answer the question. "
                       "If you don't know the answer, say that you don't know.\n\n{context}"),
            ("human", "{question}"),
        ])

        self._qa_chain = (
            {"context": self._retriever, "question": RunnablePassthrough()}
            | prompt
            | gemini_llm
            | StrOutputParser()
        )

    def answer(self, query: str) -> dict[str, Any]:
        self._ensure_ready()
        answer_text = self._qa_chain.invoke(query)
        retrieved_docs = self._retriever.invoke(query)
        return {
            "query": query,
            "answer": answer_text,
            "source_snippets": [doc.page_content[:300] for doc in retrieved_docs],
        }

    def clear_index_cache(self) -> None:
        """Forces a rebuild next time — use after the knowledge base file changes."""
        import shutil
        if self.index_cache_dir.exists():
            shutil.rmtree(self.index_cache_dir)
        self._vectorstore = None
        self._retriever = None
        self._qa_chain = None


class RetrievalAgentHandler(OrchestratorAgentInterface):
    """
    What the orchestrator instantiates and calls whenever a
    document/knowledge-base question comes in.

    Expected task shape:
        {"query": "What are the school's fee payment policies?"}
    """

    def __init__(self, knowledge_base_path: str = "school_knowledge_base.txt", **kwargs):
        self.agent = RetrievalAgent(knowledge_base_path=knowledge_base_path, **kwargs)

    def handle(self, task: dict[str, Any]) -> dict[str, Any]:
        query = task.get("query")
        if not query:
            return {"status": "error", "agent": "retrieval_agent", "error": "Task must include 'query'."}
        try:
            result = self.agent.answer(query)
            return {"status": "success", "agent": "retrieval_agent", "result": result}
        except Exception as exc:
            logger.exception("RetrievalAgent failed for query=%r", query)
            return {"status": "error", "agent": "retrieval_agent", "error": str(exc)}


if __name__ == "__main__":
    handler = RetrievalAgentHandler(knowledge_base_path="school_knowledge_base.txt")
    print(handler.handle({"query": "What are the school's fee payment policies?"}))
