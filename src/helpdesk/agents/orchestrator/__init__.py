"""The LangGraph orchestrator agent (Phase 4).

``classify -> route -> resolve | escalate_low_confidence -> finalize`` (README §2).
``graph.py`` is the pure builder + run helper; ``host.py`` wraps the compiled
graph as a MAF agent for the Foundry hosted-agent runtime.
"""
