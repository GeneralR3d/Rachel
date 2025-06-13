# Rachel

LLM-based conversational Telegram bot aiming to mimic human texting behaviour. Rachel is a singapore girl!



## Cool things

- Faux typing indicator and delays between messages
- Conversational memory using summarisation models (using a technique similar to [this](https://www.pinecone.io/learn/series/langchain/langchain-conversational-memory/))
- A separate bot to manage the bot's "context" (instructions on writing responses) from within Telegram (`/set_context`, `/get_context`)
- Manage conversation history using commands from within Telegram (`/update_history`, `/clear_history`)

## Requirements

- Python 3 (tested on 3.9)
- A separate Telegram user account from your own, i.e. a second phone number to register for one (or use a service like [Textverified](https://www.textverified.com/))

## Usage

1. Clone repo, install dependencies in `requirements.txt`
2. Rename `template.env` to `.env`
3. Create a Telegram user account and [obtain its API ID and hash](https://docs.telethon.dev/en/stable/basic/signing-in.html)
4. Create a Telegram bot ([message @BotFather](https://t.me/botfather)) and obtain its bot token
5. Create a [Google Gemini API for free](https://aistudio.google.com/apikey) account and get your API key
6. Get your personal Telegram account's user ID (not that of the bot you created earlier) ([message @userinfobot](https://t.me/userinfobot))
7. Update `.env` with all above information
8. Run `main.py`

## Future features planned
1. Different personalities for different occasions (start with enthusiastic vs normal)
2. Trigger for enthusiastic mode is when messages per min > threshold (then reduce typing delay)
3. set random timer to be online after receiving messages
4. For each group, store what you know about each user based on what they have said (long term memory)