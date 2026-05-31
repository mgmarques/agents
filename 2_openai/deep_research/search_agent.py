import os
from agents import Agent, ModelSettings, function_tool, OpenAIChatCompletionsModel
from openai import AsyncOpenAI
from ddgs import DDGS
from dotenv import load_dotenv

load_dotenv(override=True)

google_api_key = os.getenv('GOOGLE_API_KEY')
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL = "gemini-2.5-flash"
gemini_client = AsyncOpenAI(base_url=GEMINI_BASE_URL, api_key=google_api_key)
gemini_model = OpenAIChatCompletionsModel(model=GEMINI_MODEL, openai_client=gemini_client)

INSTRUCTIONS = (
    "You are a research assistant. Given a search term, you search the web for that term and "
    "produce a concise summary of the results. The summary must 2-3 paragraphs and less than 300 "
    "words. Capture the main points. Write succintly, no need to have complete sentences or good "
    "grammar. This will be consumed by someone synthesizing a report, so its vital you capture the "
    "essence and ignore any fluff. Do not include any additional commentary other than the summary itself."
)

@function_tool
def web_search(query: str, max_results:int = 3) -> str:
    """
    Search the web for recent or real-time information.

    Use this tool when:
    - The user asks for current events.
    - The user asks for recent releases or news.
    - The answer may have changed after the model's training date.

    Args:
        query: Search query.
        max_results: Number of search results to retrieve.
    """
    try:
        # O uso do gerenciador de contexto permanece idêntico
        with DDGS() as ddgs:
            results = [r for r in ddgs.text(query, max_results=max_results)]
            if not results:
                return "No results found."
            
            formatted = []
            for r in results:
                formatted.append(f"Title: {r['title']}\nLink: {r['href']}\nContent: {r['body']}\n---")
            return "\n".join(formatted)
    except Exception as e:
        return f"Error performing the search: {str(e)}"

search_agent = Agent(
    name="Search agent",
    instructions=INSTRUCTIONS,
    tools=[web_search],
    model=gemini_model,
    model_settings=ModelSettings(tool_choice="required"),
)
