"""
retrieval_agent.py
--------------------
Retrieval Agent for the school management platform, built from the RAG
prototype in school_RAG_based_system.py.

Combines a FAISS vector store (built from a school knowledge-base text
file) with an NVIDIA-hosted LLM (via NIM / ChatNVIDIA) to answer
natural-language questions grounded in that document set.

Unlike data_agent.py and prediction_agent.py, this agent DOES depend on
external services:
    - NVIDIA's hosted inference API, for answer generation
    - A one-time download of a sentence-transformer embedding model
      (from Hugging Face) the first time an index is built

Everything else about its calling convention matches the shared
OrchestratorAgentInterface contract used across all agents:

    handler = RetrievalAgentHandler(knowledge_base_path="school_knowledge_base.txt")
    result = handler.handle({"query": "What are the fee payment policies?"})
    # -> {"status": "success"/"error", "agent": "retrieval_agent", "result"/"error": ...}

Environment:
    NVIDIA_API_KEY must be set for this agent to work.
    Required packages: faiss-cpu, langchain, langchain-community,
    langchain-huggingface, langchain-nvidia-ai-endpoints, langchain-text-splitters

Index caching:
    The FAISS index is built once from the knowledge-base file and saved
    to disk (index_cache_dir). Subsequent runs load the cached index
    instead of re-embedding the document, until the cache is cleared.

Streaming:
    answer() uses ChatNVIDIA's .stream() interface rather than a single
    blocking call -- matching this endpoint's intended usage:

        client = ChatNVIDIA(model="z-ai/glm-5.2", temperature=1, top_p=1,
                             max_tokens=16384, seed=42)
        for chunk in client.stream([{"role": "user", "content": "..."}]):
            print(chunk.content, end="")

    This also mitigates read-timeout errors: a blocking call has to wait
    for the ENTIRE up-to-16384-token response to finish generating before
    anything comes back, which is exactly what was hitting the 60s read
    timeout. Streaming gets the first tokens back quickly and each
    subsequent chunk keeps the connection active, instead of one long
    silent wait.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Iterator
from dotenv import load_dotenv

# No hardcoded path: python-dotenv's default load_dotenv() walks up from the
# current working directory looking for a .env file, which works the same
# on any machine/deployment target instead of only on one person's desktop.
load_dotenv(dotenv_path=r"C:\Users\Joseph\Desktop\Database\agents\.env")

logger = logging.getLogger("retrieval_agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def strip_markdown(text: str) -> str:
    """
    Mandatory post-processing, not optional cleanup: prompt instructions
    like "never use markdown" are a request, not an enforcement mechanism.
    Models -- especially when the retrieved context itself is heavily
    formatted with bold text, tables, and headers -- tend to mirror that
    structure regardless of being told not to. This strips it in code
    every time, so a terminal (or any plain-text consumer) never has to
    depend on the model's compliance that particular run.

    Not a full markdown parser -- just removes the constructs that show
    up as literal, unreadable clutter in a non-rendering context:
    **bold**, *italics*, # headers, table pipes, and bullet markers.
    """
    # Bold / italic markers (order matters: bold before single-asterisk italic)
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'(?<!\*)\*(?!\*)([^\n*]+?)(?<!\*)\*(?!\*)', r'\1', text)
    # Headers ("# ", "## ", ... at line start)
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
    # Bullet markers at line start -> plain dash, keeps list structure readable
    text = re.sub(r'^[ \t]*[\*\-\u2022]\s+', '- ', text, flags=re.MULTILINE)
    # Markdown table pipes -> spaces (rough, but removes the visual clutter)
    text = re.sub(r'\s*\|\s*', '  ', text)
    # Horizontal rules
    text = re.sub(r'^[-=]{3,}$', '', text, flags=re.MULTILINE)
    # Collapse blank lines left behind by the above
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


class OrchestratorAgentInterface:
    """Same minimal contract every agent exposes to the orchestrator:
    a task dict in, a JSON-safe result dict out."""

    def handle(self, task: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class RetrievalAgent:
    """
    Builds (or loads a cached) FAISS index from a knowledge-base text
    file, and answers questions against it using an NVIDIA-hosted LLM.

    The index is built once and cached — both in memory for the life of
    this object, and on disk (via FAISS.save_local / load_local) so a
    fresh process doesn't have to re-embed the document either.
    """

    def __init__(
        self,
        knowledge_base_path: str = "school_knowledge_base.txt",
        index_cache_dir: str = "faiss_index_cache",
        embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        nvidia_model_name: str = "nvidia/nemotron-3-ultra-550b-a55b",
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        request_timeout: int = 60,
    ):
        self.knowledge_base_path = Path(knowledge_base_path)
        self.index_cache_dir = Path(index_cache_dir)
        self.embedding_model_name = embedding_model_name
        self.nvidia_model_name = nvidia_model_name
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.request_timeout = request_timeout

        self._vectorstore = None
        self._retriever = None
        self._prompt = None
        self._llm = None

    def _ensure_ready(self) -> None:
        """Lazily builds the index + client on first use. Cheap, local
        checks (API key, file existence) fail fast before anything that
        touches the network or a heavy import."""
        if self._llm is not None:
            return

        api_key = os.environ.get("NVIDIA_API_KEY")
        if not api_key:
            raise RuntimeError(
                "NVIDIA_API_KEY is not set. Export it in your environment "
                "before using the retrieval agent."
            )

        if not self.knowledge_base_path.exists():
            raise FileNotFoundError(f"Knowledge base file not found: {self.knowledge_base_path}")

        try:
            from langchain_community.document_loaders import TextLoader
            from langchain_text_splitters import CharacterTextSplitter
            from langchain_huggingface import HuggingFaceEmbeddings
            from langchain_community.vectorstores import FAISS
            from langchain_nvidia_ai_endpoints import ChatNVIDIA
            from langchain_core.prompts import ChatPromptTemplate
        except ImportError as exc:
            raise RuntimeError(
                "Missing dependency for the retrieval agent. Install with: "
                "pip install faiss-cpu langchain langchain-community "
                "langchain-huggingface langchain-nvidia-ai-endpoints langchain-text-splitters"
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

        self._prompt = ChatPromptTemplate.from_messages([
            ("system", "You are a helpful AI assistant for answering questions about this school. "
                       "Use ONLY the provided context to answer the user's question.\n"
                       "Rules:\n"
                       "- Answer naturally in clear conversational English.\n"
                       "- Do NOT copy the source document verbatim.\n"
                       "- Summarize the relevant information in your own words.\n"
                       "- Answer only the question that was asked.\n"
                       "- Ignore unrelated sections of the retrieved context.\n"
                       "- Never reproduce markdown tables, headings, bullet formatting, or document structure.\n"
                       "- If the context contains a table, explain its meaning in plain language instead.\n"
                       "- Keep answers concise unless the user explicitly asks for detail.\n"
                       "- If the answer cannot be found in the provided context, simply say you don't know.\n\n"
                       "{context}"),
            ("human", "{question}"),
        ])

        # Matches the target endpoint's constructor exactly (model, temperature,
        # top_p, max_tokens, seed), plus a timeout raised from the library
        # default -- the read-timeout error came from waiting on a full
        # up-to-16384-token blocking response; this alone helps, and
        # switching answer()/stream_answer() below to .stream() helps more.
        self._llm = ChatNVIDIA(
            model=self.nvidia_model_name,
            temperature=1,
            top_p=1,
            max_tokens=16384,
            seed=42,
            api_key=api_key,
            timeout=self.request_timeout,
        )

    def _build_prompt_messages(self, query: str) -> list[dict[str, str]]:
        """Retrieves context and renders it into the same system/human
        message shape the target endpoint expects: a plain list of
        {"role", "content"} dicts, not a LangChain-chain-specific object --
        keeps this agent's use of ChatNVIDIA identical to the reference
        usage pattern rather than hidden inside LCEL's `|` composition."""
        docs = self._retriever.invoke(query)
        context = "\n\n".join(doc.page_content for doc in docs)
        rendered = self._prompt.format_messages(context=context, question=query)
        return [{"role": m.type if m.type != "human" else "user", "content": m.content} for m in rendered]

    def stream_answer(self, query: str) -> Iterator[str]:
        """Yields answer text incrementally as it's generated, matching
        the target endpoint's `for chunk in client.stream(...)` pattern.
        Use this directly for a terminal/chat UI that should print tokens
        as they arrive instead of waiting for the full response."""
        self._ensure_ready()
        messages = self._build_prompt_messages(query)
        for chunk in self._llm.stream(messages):
            if chunk.content:
                yield chunk.content

    def answer(self, query: str) -> dict[str, Any]:
        """Blocking convenience wrapper for callers (orchestrator, planner,
        narration_agent) that need one complete result dict rather than a
        live stream. Internally still streams -- just collects the chunks
        before returning -- so it keeps the timeout benefit of streaming
        without changing the {"query", "answer", "source_snippets"}
        contract the rest of the system already depends on.

        Runs the joined text through strip_markdown() before returning --
        unconditionally, not just when the model happens to comply with
        the "don't use markdown" prompt instruction."""
        self._ensure_ready()
        chunks = list(self.stream_answer(query))
        answer_text = strip_markdown("".join(chunks))

        retrieved_docs = self._retriever.invoke(query)
        return {
            "query": query,
            "answer": answer_text,
            "source_snippets": [doc.page_content[:300] for doc in retrieved_docs],
        }

    def clear_index_cache(self) -> None:
        """Forces a rebuild next time -- use after the knowledge base file changes."""
        import shutil
        if self.index_cache_dir.exists():
            shutil.rmtree(self.index_cache_dir)
        self._vectorstore = None
        self._retriever = None
        self._llm = None


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

    # Live-streaming demo, matching the target endpoint's print pattern directly:
    print("Streaming: ", end="")
    for token in handler.agent.stream_answer("Who is gregory Ogwuche?"):
        print(token, end="")
    print()