from langgraph.graph import StateGraph, START, END
from agents.orchestrator_agent import orchestrator_node, route_orchestrator
from agents.context_agent import context_node
from agents.summarizer_agent import summarizer_node
from agents.remediation_agent import remediation_node
from agents.validation_agent import validation_node
from agents.documentation_agent import documentation_node
from agents.vcs_agent import vcs_setup, vcs_finalize
from state import PipelineState

# ── Build graph ─────────────────────────────────────────────
builder = StateGraph(PipelineState)

builder.add_node("vcs_setup", vcs_setup)
builder.add_node("vcs_finalize", vcs_finalize)

builder.add_node("orchestrator", orchestrator_node)
builder.add_node("context_agent", context_node)
builder.add_node("summarizer_agent", summarizer_node)
builder.add_node("remediation_agent", remediation_node)
builder.add_node("validation_agent", validation_node)
builder.add_node("documentation_agent", documentation_node)

# ── Edges ────────────────────────────────────────────────────
# Orchestrator is the sole entry point and routing hub
builder.add_edge(START, "orchestrator")
builder.add_edge("vcs_setup", "orchestrator")

builder.add_conditional_edges(
    "orchestrator",
    route_orchestrator,
    {
        "vcs_setup":     "vcs_setup",
        "context_agent": "context_agent",
        "summarizer_agent": "summarizer_agent",
        "remediation_agent": "remediation_agent",
        "documentation_agent": "documentation_agent",
        "vcs_finalize": "vcs_finalize",
    },
)

builder.add_edge("context_agent", "orchestrator")
builder.add_edge("summarizer_agent", "remediation_agent")
builder.add_edge("remediation_agent", "validation_agent")
builder.add_edge(
    "validation_agent", "orchestrator"
)  # unconditional: orchestrator decides next step
builder.add_edge(
    "documentation_agent", "orchestrator"
)  # unconditional: orchestrator advances chunk

builder.add_edge("vcs_finalize", END)

graph = builder.compile()
