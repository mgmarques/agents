from concurrent.futures import ThreadPoolExecutor, TimeoutError as ThreadTimeout
from pathlib import Path
from dotenv import load_dotenv
from langchain_ollama import ChatOllama
from chromadb import PersistentClient
from pydantic import BaseModel, Field
from tenacity import retry, wait_exponential
from ollama import Client

load_dotenv(override=True)

MODEL = "gemma3:4b"
RANK_MODEL = "llama3.2:1b"
DB_NAME = str(Path(__file__).parent.parent / "preprocessed_db")
KNOWLEDGE_BASE_PATH = Path(__file__).parent.parent / "knowledge-base"
SUMMARIES_PATH = Path(__file__).parent.parent / "summaries"
collection_name = "docs"
embedding_model = "qwen3-embedding:4b"
chroma = PersistentClient(path=DB_NAME)
collection = chroma.get_or_create_collection(collection_name)
wait = wait_exponential(multiplier=1, min=10, max=240)
# Inicialização dos Clientes
ollama_url = "http://localhost:11434" 
ollama_client = Client(host=ollama_url)
llm = ChatOllama(temperature=0, model=MODEL)
llm_base = ChatOllama(temperature=0, model=MODEL)
RETRIEVAL_K = 10
FINAL_K = 10
SYSTEM_PROMPT = """
You are a knowledgeable, friendly assistant representing the company Insurellm.
You are chatting with a user about Insurellm.
Your answer will be evaluated for accuracy, relevance and completeness, so make sure it only answers the question and fully answers it.
If you don't know the answer, say so.
For context, here are specific extracts from the Knowledge Base that might be directly relevant to the user's question:
{context}

With this context, please answer the user's question. Be accurate, relevant and complete.
"""


class Result(BaseModel):
    page_content: str
    metadata: dict


class RankOrder(BaseModel):
    order: list[int] = Field(
        description="The order of relevance of chunks, from most relevant to least relevant, by chunk id number"
    )


#@retry(wait=wait)
def rerank(question, chunks):
    system_prompt = f"""
    You are a document re-ranker.
    You must rank order the provided chunks by relevance to the question, with the most relevant chunk first.
    Reply only with the list of ranked original CHUNK IDs, nothing else. 
    The ranked list must have the {len(chunks)} size, and don't reply to any ID greater than {len(chunks)}.
    """
    user_prompt = f"The user has asked the following question:\n\n{question}\n\nOrder all the chunks of text by relevance to the question.\n\n"
    user_prompt += "Here are the original list of chunks:\n\n"
    for index, chunk in enumerate(chunks):
        user_prompt += f"# CHUNK ID: {index + 1}:\n\n{chunk.page_content}\n\n"
    user_prompt += "Reply only with the list of ranked chunk ids, nothing else."
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Explicitly use RANK_MODEL as defined in your global configurations
    llm_rank = llm_base.bind(
        model=RANK_MODEL, 
        options={"temperature": 0.0},
        keep_alive="0m" 
    ).with_structured_output(RankOrder)

    print("Submitting reranking request to Ollama...")
    try:
        # Run the synchronous .invoke() inside a background thread to safely enforce the timeout
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(llm_rank.invoke, messages)
            response = future.result(timeout=35.0) # Enforces strict 35s timeout
        
        order = response.order[:len(chunks)]
        print(f"Reranking successful: {order}")
        return [chunks[i - 1] for i in order]
        
    except (ThreadTimeout, Exception) as e:
        print(f"Ollama timeout/abort ({type(e).__name__}). Unfreezing and returning original chunks.\nError details: {e}")
        return chunks



def make_rag_messages(question, history, chunks):
    context = "\n\n".join(
        f"Extract from {chunk.metadata['source']}:\n{chunk.page_content}" for chunk in chunks
    )
    system_prompt = SYSTEM_PROMPT.format(context=context)
    return (
        [{"role": "system", "content": system_prompt}]
        + history
        + [{"role": "user", "content": question}]
    )


#@retry(wait=wait)
def rewrite_query(question, history=[]):
    """Rewrite the user's question to be a more specific question that is more likely to surface relevant content in the Knowledge Base."""
    message = f"""
You are in a conversation with a user, answering questions about the company Insurellm.
You are about to look up information in a Knowledge Base to answer the user's question.

This is the history of your conversation so far with the user:
{history}

And this is the user's current question:
{question}

Your responde shuld be:
* Only correct grammar if necessary, never change the meaning. of the current question.
* Summarize the question if necessary.
* It shuld be a VERY short specific question. 
* Never mention the company name unless it's already present on the user's current questions.
* IMPORTANT: Respond ONLY with the knowledgebase query, nothing else.
"""
    messages = [{"role": "system", "content": message}]
    #response = completion(model=MODEL, messages=messages, base_url="http://localhost:11434")
    print("Submitting query rewrite to LLM...")
    response = llm.invoke(messages)
    return response.content #response.choices[0].message.content


def merge_chunks(original, rewritten):
    merged = original[:]
    existing = [chunk.page_content for chunk in original]
    for chunk in rewritten:
        if chunk.page_content not in existing:
            merged.append(chunk)
    return merged


def fetch_context_unranked(question):
    #query = openai.embeddings.create(model=embedding_model, input=[question]).data[0].embedding
    query = ollama_client.embeddings(model=embedding_model, prompt=question)
    results = collection.query(query_embeddings=query['embedding'], n_results=RETRIEVAL_K)
    chunks = []
    for result in zip(results["documents"][0], results["metadatas"][0]):
        chunks.append(Result(page_content=result[0], metadata=result[1]))
    return chunks


def fetch_context(original_question):
    print("Fetching context for original question...")
    rewritten_question = rewrite_query(original_question)
    print("Rewritten Question:", rewritten_question)
    chunks_original = fetch_context_unranked(original_question)
    chunks_rewritten_question = fetch_context_unranked(rewritten_question)
    chunks = merge_chunks(chunks_original, chunks_rewritten_question)
    print("Final merged chunks size:", len(chunks))
    reranked = rerank(original_question, chunks)
    return reranked[:FINAL_K]


@retry(wait=wait)
def answer_question(question: str, history: list[dict] = []) -> tuple[str, list]:
    """
    Answer a question using RAG and return the answer and the retrieved context
    """
    try:
        print("Original question:", question)
        chunks = fetch_context(question)
        print("Number of Chunks:", len(chunks))
        messages = make_rag_messages(question, history, chunks)
        print("Make the sunmit to LLM")
        #response = completion(model=MODEL, messages=messages, base_url="http://localhost:11434")
        response = llm.invoke(messages)
        print("Responsed from LLM")
        return response.content, chunks # response.choices[0].message.content, chunks
    except Exception as e:
        msg = f"Error: {e}"
        print(msg)
        return msg, chunks