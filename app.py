import os
import json
import streamlit as st
from openai import AzureOpenAI
from dotenv import load_dotenv
from categories import CATEGORIES, FALLBACK_MESSAGE

load_dotenv()

st.set_page_config(
    page_title="AdminGenie",
    page_icon="🧞",
    layout="centered",
)

@st.cache_resource
def get_client():
    return AzureOpenAI(
        api_key=os.getenv("AZURE_OPENAI_KEY"),
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01"),
    )

client = get_client()
DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

# ── STEP 1: AGENT ─────────────────────────────────────────────────────────────
# Routes the LATEST question to a category.
# We only pass the current question here — routing doesn't need history.

ROUTING_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "route_to_category",
            "description": "Route the admin question to the most relevant FAQ category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": list(CATEGORIES.keys()) + ["None"],
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "reasoning": {
                        "type": "string",
                    },
                },
                "required": ["category", "confidence", "reasoning"],
            },
        },
    }
]

def agent_route(question):
    response = client.chat.completions.create(
        model=DEPLOYMENT,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a routing agent for an office admin FAQ system. "
                    "Classify the user question into the best FAQ category. "
                    "Always call the route_to_category tool."
                ),
            },
            {"role": "user", "content": question},
        ],
        tools=ROUTING_TOOLS,
        tool_choice={"type": "function", "function": {"name": "route_to_category"}},
    )
    tool_call = response.choices[0].message.tool_calls[0]
    return json.loads(tool_call.function.arguments)


# ── STEP 2: RAG ───────────────────────────────────────────────────────────────
def retrieve_context(category):
    if category in CATEGORIES:
        return CATEGORIES[category]["info"]
    return None


# ── STEP 3: GENERATE with MEMORY ─────────────────────────────────────────────
# This is where multi-turn happens.
# We pass the FULL conversation history so GPT remembers everything said before.

def generate_answer(question, category, context, chat_history):
    """
    chat_history: list of {"role": "user"/"assistant", "content": "..."}
    We pass the full history so GPT understands follow-up questions.
    """
    if context:
        system_prompt = (
            "You are AdminGenie, a friendly and helpful office admin assistant. "
            "Answer using ONLY the FAQ below. Be concise and practical. "
            "You remember the full conversation — use it to understand follow-up questions. "
            "For example, if the user already asked about a business trip and now asks "
            "'what about expenses?' — you know they mean business trip expenses. "
            "If the FAQ does not cover the question, say so and suggest contacting the admin office."
            f"\n\n--- FAQ: {category} ---\n{context}"
        )
    else:
        system_prompt = (
            "You are AdminGenie, a friendly office admin assistant. "
            "You remember the full conversation — use it to understand follow-up questions. "
            "You have no FAQ information for this question. "
            "Politely say so and suggest contacting the admin office."
        )

    # Build messages: system + full history + current question
    messages = [{"role": "system", "content": system_prompt}]

    # Add conversation history (everything said before this question)
    for msg in chat_history:
        messages.append({
            "role": msg["role"],
            "content": msg["content"]
        })

    # Add the current question
    messages.append({"role": "user", "content": question})

    response = client.chat.completions.create(
        model=DEPLOYMENT,
        messages=messages,
        max_completion_tokens=500,
    )
    return response.choices[0].message.content.strip()


# ── FULL PIPELINE ─────────────────────────────────────────────────────────────
def run_pipeline(question, chat_history):
    routing  = agent_route(question)
    category = routing["category"] if routing["category"] != "None" else None
    context  = retrieve_context(category) if category else None
    answer   = generate_answer(question, category, context, chat_history) if context else FALLBACK_MESSAGE
    return {
        "answer":     answer,
        "category":   category,
        "confidence": routing.get("confidence", "—"),
        "reasoning":  routing.get("reasoning", ""),
    }


# ── UI ────────────────────────────────────────────────────────────────────────
st.title("🧞 AdminGenie")
st.caption("Your office admin assistant — ask me anything about travel, expenses, leave, IT, or facilities.")

# Quick topic buttons
BUTTON_LABELS = {
    "Travel & Business Trip":      "✈️ Travel",
    "Reimbursement & Expenses":    "💴 Expenses",
    "Leave & Absence":             "🌿 Leave",
    "IT & Systems":                "💻 IT",
    "Facilities & General Admin":  "🏢 Facilities",
}

st.markdown("**Quick topics:**")
cols = st.columns(len(CATEGORIES))
for i, cat_name in enumerate(CATEGORIES.keys()):
    label = BUTTON_LABELS.get(cat_name, cat_name)
    if cols[i].button(label, use_container_width=True, key=f"cat_{i}"):
        st.session_state["browse_category"] = cat_name

if "browse_category" in st.session_state:
    cat = st.session_state["browse_category"]
    with st.expander(f"📂 {cat}", expanded=True):
        st.markdown(CATEGORIES[cat]["info"])
    if st.button("✕ Close"):
        del st.session_state["browse_category"]

st.divider()

# ── Chat history stored in session ───────────────────────────────────────────
# session_state["messages"] stores everything shown in the UI
# We pass only role+content to GPT (not the meta/category info)

if "messages" not in st.session_state:
    st.session_state["messages"] = []

# Display all past messages
for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("meta"):
            m = msg["meta"]
            st.caption(
                f"📂 **{m['category']}** · confidence: {m['confidence']} · _{m['reasoning']}_"
            )

# ── Chat input ────────────────────────────────────────────────────────────────
if question := st.chat_input("Ask your admin question here..."):

    # Show user message
    st.session_state["messages"].append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    # Build history to pass to GPT (only role + content, no meta)
    # Exclude the message we just added (it's the current question)
    history_for_gpt = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state["messages"][:-1]  # everything except current question
    ]

    # Run pipeline with full history
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            result = run_pipeline(question, history_for_gpt)
        st.markdown(result["answer"])
        if result["category"]:
            st.caption(
                f"📂 **{result['category']}** · "
                f"confidence: {result['confidence']} · "
                f"_{result['reasoning']}_"
            )
        else:
            st.caption("📂 No category matched → please contact the admin office")

    # Save assistant reply to history
    st.session_state["messages"].append({
        "role":    "assistant",
        "content": result["answer"],
        "meta": {
            "category":   result["category"] or "None",
            "confidence": result["confidence"],
            "reasoning":  result["reasoning"],
        },
    })

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🧞 AdminGenie")
    st.markdown("Your office admin assistant.")
    st.divider()
    st.markdown("**How it works:**")
    st.markdown("1. 🤖 Agent routes your question")
    st.markdown("2. 📄 Retrieves relevant FAQ")
    st.markdown("3. ✍️ Generates a grounded answer")
    st.markdown("4. 🧠 Remembers the full conversation")
    st.divider()
    st.markdown("**Topics I can help with:**")
    for cat in CATEGORIES.keys():
        st.markdown(f"- {cat}")
    st.divider()
    st.markdown("**Need more help?**")
    st.markdown("📧 admin@office.com")
    st.markdown("📞 ext. 1100")
    st.divider()

    # Show how many messages are in memory
    msg_count = len(st.session_state.get("messages", []))
    if msg_count > 0:
        st.caption(f"🧠 {msg_count} messages in memory this session")

    if st.button("🗑️ Clear chat"):
        st.session_state["messages"] = []
        st.rerun()
    st.caption("AdminGenie v1.1 · 2025")