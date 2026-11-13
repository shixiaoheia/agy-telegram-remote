# Historical CLI output used only as an offline test fixture.
OFFICIAL_MODELS: tuple[tuple[str, str, str], ...] = (
    ("Gemini 4 Argon", "gemini-4-argon-high", "High 深度推理"),
    ("Gemini 4 Argon", "gemini-4-argon-medium", "Medium 推理"),
    *((f"Claude {family.title()} 5.5", f"claude-{family}-5-5-{level}", f"{family.title()} 5.5 ({level})")
      for family in ("sonnet", "opus") for level in ("low", "medium", "high", "max")),
    ("Gemini 3.8 Flash (最新极速)", "gemini-3.8-flash-high", "High 思考 (推荐)"),
    ("Gemini 3.8 Flash (最新极速)", "gemini-3.8-flash-medium", "Medium 思考"),
    ("Gemini 3.8 Flash (最新极速)", "gemini-3.8-flash-low", "Low 思考"),
    ("Gemini 3.1 Pro (深度推理)", "gemini-3.1-pro-high", "High 深度推理 (推荐)"),
    ("Gemini 3.1 Pro (深度推理)", "gemini-3.1-pro-low", "Low 推理"),
)

