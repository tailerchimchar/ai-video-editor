"""Editor suggestions for a story round (docs: api/docs/story-suggestions.md).

Candidates (pure code, one builder per game) -> panel questions asked through
`decisions.ask_many` (Jev, rules fallback; never Anthropic) -> ranked
`SuggestionItem`s in `story/<round>/suggestions.json` -> accepted ones become
`effect` requests in `requests.json`.
"""
