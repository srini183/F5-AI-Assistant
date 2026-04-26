# F5 AI Assistant

A Streamlit-based Python assistant that translates natural-language prompts into structured read-only F5 BIG-IP API actions. It uses local Ollama by default, with optional OpenAI support only when explicitly enabled.

## Project Structure

- `app.py`: main Streamlit chat application
- `f5_client.py`: BIG-IP REST integration
- `ai_agent.py`: OpenAI / Ollama intent parsing
- `requirements.txt`: Python dependencies

## Setup

1. Create a virtual environment:

```powershell
python -m venv .venv
```

2. Activate the environment:

```powershell
.venv\Scripts\Activate.ps1
```

3. Install dependencies:

```powershell
pip install -r requirements.txt
```

4. Configure environment variables. You can copy `.env.example` to `.env` and update the values:

```powershell
Copy-Item .env.example .env
```

Required values:

- `BIGIP_HOST` or `F5_HOST`
- `BIGIP_USERNAME` / `BIGIP_PASSWORD` or `F5_USERNAME` / `F5_PASSWORD`
- `OLLAMA_BASE_URL`
- `OLLAMA_MODEL`

Optional values:

- `BIGIP_VERIFY_SSL` or `F5_VERIFY_SSL`
- `AI_PROVIDER` (`ollama` by default, `openai` only if you want OpenAI)
- `OPENAI_MODEL`
- `OPENAI_API_KEY`

## Run

```powershell
streamlit run app.py
```

## Test

```powershell
python -m unittest discover
```

## Example Query

Input:

```text
show pool members for app1
```

Structured action:

```json
{
  "action": "get_pool_members",
  "pool": "app1",
  "reasoning": "The user wants to inspect members of the app1 pool."
}
```

## Notes

- The app keeps chat history in Streamlit session state.
- The assistant is intentionally read-only and rejects requests that would modify configuration or operational state.
- Passwords and API keys are loaded from environment variables.
- The assistant uses local Ollama by default.
- OpenAI is not used unless `AI_PROVIDER=openai` is configured.
