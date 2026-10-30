"""localai-steer: Claude-CLI-style mid-run steering for LocalAI agents.

M0 delivers the one-shot turn: POST the message, subscribe to the agent's
SSE broadcast, and print the final agent result correlated by ``message_id``.

M1 adds live SSE progress rendering (rich panel / plain stderr lines),
event-to-turn correlation (R1–R5), and a per-turn ``TurnProgress`` capture
buffer with M2-facing views.
"""

__version__ = "0.1.0"
