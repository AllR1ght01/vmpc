import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["VMPC_HOME"] = tempfile.mkdtemp(prefix="vmpc-chats-")

from vmpc.chats import (
    ChatRecord,
    chats_dir,
    delete_chat,
    latest_chat,
    list_chats,
    load_chat,
    new_id,
    save_chat,
)
from vmpc.session import Session

print("home", chats_dir())
session = Session()
session.add_user("почини рендер маркдауна")
session.add_assistant("Готово, вот патч.")
session.total_tokens = 412
session.mode = "dev"

first = new_id()
record = ChatRecord.from_session(
    session, first, provider_name="rose", model="claude-opus-5", title="Рендер маркдауна"
)
save_chat(record)

second = new_id()
print("ids distinct:", first != second, first, second)
save_chat(ChatRecord.from_session(Session(), second, title="Пустой чат"))

listed = list_chats()
print("listed:", [(r.id, r.label, r.turns) for r in listed])
print("latest:", latest_chat().id == second)

back = load_chat(first)
print("title", repr(back.title), "mode", repr(back.mode), "tokens", back.total_tokens)
print("messages", back.messages)

fresh = Session()
back.into_session(fresh)
print("resumed", len(fresh.messages), fresh.total_tokens, repr(fresh.mode))
print("system preserved:", fresh.system == session.system)

# a chat with no title falls back to the first message
untitled = new_id()
s = Session()
s.add_user("как поставить розовую тему в терминале")
save_chat(ChatRecord.from_session(s, untitled))
print("fallback label:", repr(load_chat(untitled).label))

print("delete:", delete_chat(first), "left:", len(list_chats()))
print("delete bogus:", delete_chat("../../etc/passwd"))

# a corrupt file must not break the listing
(chats_dir() / "20260101-000000.json").write_text("{ not json", encoding="utf-8")
print("survives corrupt file:", len(list_chats()))
