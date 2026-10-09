from hypothesis import settings

# Determinism (PLAN.md §0): the same code must produce the same test cases on every run.
settings.register_profile("deterministic", derandomize=True, deadline=None)
settings.load_profile("deterministic")
