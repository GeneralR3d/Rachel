def parse_history(chat_log):
    """Convert a chat_log list of (sender_user_id, text, telegram_message_id) tuples
    into the per-message dict format expected by rewrite_history."""
    return [
        {"telegram_message_id": entry[2], "sender_user_id": entry[0], "content": entry[1]}
        for entry in chat_log
    ]
