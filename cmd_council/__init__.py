"""cmd-council — LLM Council on a single Command Code subscription.

Three-stage flow (karpathy/llm-council):
  Stage 1: advisors answer in parallel (first opinions)
  Stage 2: anonymized peer review + ranking
  Stage 3: chairman synthesizes the final answer

Hardened with openfusion-style patterns: graceful degradation, budget
guard with rolling windows, OpenAI-compatible facade, dual-protocol
provider adapter (OpenAI + Anthropic formats on Command Code).
"""

__version__ = "0.1.0"
