# F5 AI Assistant

A Streamlit-based Python assistant that translates natural-language prompts into structured read-only F5 BIG-IP API actions. It features a modern Material Design UI and powerful multi-device Reverse Lookup capabilities. It uses local Ollama by default, with optional OpenAI support only when explicitly enabled.

## Project Structure

- `app.py`: main Streamlit chat application
- `f5_client.py`: BIG-IP REST integration
- `ai_agent.py`: OpenAI / Ollama intent parsing
- `devices.json`: (Optional) Inventory file for querying multiple BIG-IPs simultaneously
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
- `BIGIP_USERNAME` / `BIGIP_PASSWORD` *(Note: Wrap passwords containing special characters like # or $ in quotes!)*
- `OLLAMA_BASE_URL`
- `OLLAMA_MODEL`

Optional values:

- `BIGIP_VERIFY_SSL` or `F5_VERIFY_SSL`
- `AI_PROVIDER` (`ollama` by default, `openai` only if you want OpenAI)
- `OPENAI_MODEL`
- `OPENAI_API_KEY`

5. (Optional) Multi-Device Inventory:

To query multiple F5 devices at once (extremely useful for Reverse Lookups), create a `devices.json` file in the root directory:
```json
{
  "Datacenter-A": "https://10.0.1.100",
  "Datacenter-B": "https://10.0.2.100"
}
```
*If `devices.json` is missing, the application will simply fall back to querying the single `BIGIP_HOST` defined in your `.env` file.*

## Run

Run locally on the default port (8501):
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
