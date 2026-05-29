from pathlib import Path
from langchain_ollama import ChatOllama
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from chromadb import PersistentClient
from tqdm import tqdm
from litellm import completion
from tenacity import retry, wait_exponential
from ollama import Client

load_dotenv(override=True)
NAME = "marcelo Marques"
COMPANY = "Vitarts T. G. ltda"
DB_NAME = str(Path(__file__).parent.parent / "preprocessed_db")
collection_name = "docs"
embedding_model = "qwen3-embedding:4b" # "qwen/qwen3-embedding-4b" to retrive/query by OpenRouter /"openai/gpt-4o-mini"
KNOWLEDGE_BASE_PATH = Path(__file__).parent.parent / "me"
AVERAGE_CHUNK_SIZE = 250
WORKERS = 5
wait = wait_exponential(multiplier=1, min=10, max=240)

MODEL = "gemma3:4b"
ollama_url = "http://localhost:11434" 
ollama_client = Client(host=ollama_url)
llm_base = ChatOllama(model=MODEL)


class Result(BaseModel):
    page_content: str
    metadata: dict

class Chunk(BaseModel):
    headline: str = Field(description="A brief heading for this chunk, typically a few words, that is most likely to be surfaced in a query")
    summary: str = Field(description="A few sentences summarizing the content of this chunk to answer common questions")
    original_text: str = Field(description="The original text of this chunk from the provided document, exactly as is, not changed in any way")

    def as_result(self, document):
        metadata = {"source": document["source"], "type": document["type"]}
        return Result(page_content=self.headline + "\n\n" + self.summary + "\n\n" + self.original_text,metadata=metadata)


class Chunks(BaseModel):
    chunks: list[Chunk]

def fetch_documents():
    """A homemade version of the LangChain DirectoryLoader"""

    documents = []

    for folder in KNOWLEDGE_BASE_PATH.iterdir():
        doc_type = folder.name
        print(f"Load {doc_type}...")
        for file in folder.rglob("*.md"):
            with open(file, "r", encoding="utf-8") as f:
                documents.append({"type": doc_type, "source": file.as_posix(), "text": f.read()})

    print(f"Loaded {len(documents)} documents")
    return documents

def make_prompt(document):
    how_many = (len(document["text"]) // AVERAGE_CHUNK_SIZE) + 1
    return f"""
    You take a document and you split the document into overlapping chunks for a KnowledgeBase.

    The document is from {NAME}'s career, background, skills and experience, and his company called {COMPANY}.
    The document is of type: {document["type"]}
    The document has been retrieved from: {document["source"]}

    A chatbot will use these chunks to answer questions about {NAME} and the {COMPANY} company.
    You should divide up the document as you see fit, being sure that the entire document is returned in the chunks - don't leave anything out.
    This document should probably be split into {how_many} chunks, but you can have more or less as appropriate.
    There should be overlap between the chunks as appropriate; typically about 25% overlap or about 50 words, so you have the same text in multiple chunks for best retrieval results.

    For each chunk, you should provide a headline, a summary, and the original text of the chunk.
    Together your chunks should represent the entire document with overlap.

    Here is the document:

    {document["text"]}

    Respond with the chunks.
    """
    
def make_messages(document):
    return [
        {"role": "user", "content": make_prompt(document)},
    ]

#@retry(wait=wait)
def process_document(document):
    messages = make_messages(document)
    response = ollama_client.chat(model=MODEL, messages=messages, format=Chunks.model_json_schema(),
        options={
            "temperature": 0.0  # Lower values (0.0-0.2) mean more deterministic outputs
        })
    # Clear memory aggressively on the Ollama side
    #llm = llm_base.bind(options={"temperature": 0.0}, keep_alive="0m").with_structured_output(Chunks)
    #response = llm.invoke(messages)
    reply = response.message.content # response.choices[0].message.content
    doc_as_chunks = Chunks.model_validate_json(reply).chunks
    return [chunk.as_result(document) for chunk in doc_as_chunks]

def create_chunks(documents):
    """
    Create chunks using a number of workers in parallel.
    If you get a rate limit error, set the WORKERS to 1.
    """
    chunks = []
    for doc in tqdm(documents):
        chunks.extend(process_document(doc))
    return chunks


def create_embeddings(chunks):
    chroma = PersistentClient(path=DB_NAME)    
    if collection_name in [c.name for c in chroma.list_collections()]:
        chroma.delete_collection(collection_name)

    texts = [chunk.page_content for chunk in chunks]
    print("Generating vectors...")
    vectors = []
    for text in tqdm(texts):
        response = ollama_client.embeddings(model=embedding_model, prompt=text)
        vectors.append(response['embedding'])

    collection = chroma.get_or_create_collection(collection_name)
    ids = [str(i) for i in range(len(chunks))]
    metas = [chunk.metadata for chunk in chunks]
    collection.add(ids=ids, embeddings=vectors, documents=texts, metadatas=metas)
    print(f"Vectorstore created with {collection.count()} documents")

if __name__ == "__main__":
    documents = fetch_documents()
    chunks = create_chunks(documents)
    create_embeddings(chunks)
    print("Ingestion complete")
