"""Tests never spend money: every LLM call is replayed from the committed cassettes."""

import os

os.environ["LLM_MODE"] = "replay"
