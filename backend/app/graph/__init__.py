"""LangGraph version of the decision pipeline (step 8e).

state.py   shared state and graph shape (no langgraph needed)
engine.py  NUDGEIQ_ENGINE setting (no langgraph needed)
builder.py the compiled graph (needs langgraph; imported only when the graph engine is on)
"""
